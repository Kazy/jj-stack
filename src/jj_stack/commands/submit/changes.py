"""Classify each selected change and describe the one atomic remote update."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

import jj_stack.ui as ui
from jj_stack.errors import CliError, DriftError
from jj_stack.formatting import format_pr_label
from jj_stack.identifiers import ChangeId, CommitId, short_change_id
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubStack
from jj_stack.models.stack import LocalCommit, LocalStack
from jj_stack.models.tracking import TrackedPR
from jj_stack.stack.change_state import (
    UNOBSERVED,
    BranchDisagrees,
    BranchMissing,
    ChangeObservation,
    ChangeState,
    Closed,
    Merged,
    PRHeadMoved,
    Queued,
    Stop,
    Unobserved,
    WithPR,
    classify,
    live_pr,
    stop_error,
)
from jj_stack.stack.github_stack_safety import github_rewrote_stack
from jj_stack.stack.pr_branches import ResolvedPRBranch
from jj_stack.ui import Message

from .models import PreparedSubmitChange


def prepare_submit_changes(
    *,
    branch_resolutions: tuple[ResolvedPRBranch, ...],
    github_stacks: tuple[GithubStack, ...],
    lookups: Mapping[str, ChangeObservation],
    remote_targets: Mapping[str, CommitId],
    stack: LocalStack,
    tracked_prs: Mapping[ChangeId, TrackedPR],
) -> tuple[PreparedSubmitChange, ...]:
    """Classify every selected change and describe the one atomic remote update.

    A damaged or divergent link anywhere in the plan stops submit before any PR branch pushes
    or any sibling pull request changes; checking per change inside the concurrent sync phase
    would let a mid-stack failure surface only after those mutations had happened.
    """

    states: list[ChangeState] = []
    for resolution in branch_resolutions:
        observed_target: CommitId | None | Unobserved = remote_targets.get(resolution.branch)
        if resolution.recovered:
            # An interrupted first submit left this branch, and its commit's change-ID header
            # already proved it belongs to this change.
            observed_target = UNOBSERVED
        states.append(
            classify(replace(lookups[resolution.branch], remote_target=observed_target))
        )
    head = short_change_id(stack.head.change_id)
    moved_by_github = _moved_by_github(
        tuple(states), github_stacks=github_stacks, tracked_prs=tracked_prs
    )
    if moved_by_github:
        # One sync updates them all.
        prs = ui.join(
            lambda state: format_pr_label(state.pr.number, url=state.pr.html_url), moved_by_github
        )
        raise DriftError(
            t"GitHub updated {prs} while merging or rebasing the GitHub stack, so submit made "
            t"no changes.",
            condition="remote_branch_moved",
            hint=t"Run {ui.cmd(f'jj-stack sync {head}')} to bring GitHub's updates into the "
            t"local stack.",
        )
    if stops := tuple(state for state in states if isinstance(state, Stop)):
        raise stop_error(*stops, rerun=f"jj-stack submit {head}")
    for state in states:
        _require_submittable(state, head_change_id=stack.head.change_id)
    return tuple(
        PreparedSubmitChange(
            branch=resolution.branch,
            expected_remote_target=remote_targets.get(resolution.branch),
            change=change,
            pr=live_pr(state),
        )
        for resolution, change, state in zip(
            branch_resolutions, stack.changes, states, strict=True
        )
    )


def _moved_by_github(
    states: tuple[ChangeState, ...],
    *,
    github_stacks: tuple[GithubStack, ...],
    tracked_prs: Mapping[ChangeId, TrackedPR],
) -> tuple[PRHeadMoved, ...]:
    """The PRs GitHub moved while merging or rebasing their GitHub stack."""

    tracked = {pr.pr_identity.pr_number for pr in tracked_prs.values()}
    unmoved = {
        state.pr.number
        for state in states
        if isinstance(state, WithPR)
        and state.pr.head.sha == state.tracked.submitted_baseline.commit_id
    }
    rewritten = {
        number
        for stack in github_stacks
        if github_rewrote_stack(stack, tracked=tracked, unmoved=unmoved)
        for number in stack.active_pr_numbers
    }
    return tuple(
        state
        for state in states
        if isinstance(state, PRHeadMoved) and state.pr.number in rewritten
    )


def _require_submittable(
    state: ChangeState,
    *,
    head_change_id: ChangeId,
) -> None:
    head = short_change_id(head_change_id)
    if isinstance(state, Queued):
        pr_label = format_pr_label(state.pr.number, url=state.pr.html_url)
        raise CliError(
            t"{pr_label} for {ui.change_id(state.change_id)} is in the merge queue, so submit "
            t"made no changes. Any new changes above it remain unsubmitted.",
            hint=t"Wait for the queued PRs to merge, then run "
            t"{ui.cmd(f'jj-stack sync {head}')} followed by "
            t"{ui.cmd(f'jj-stack submit {head}')}.",
        )
    if isinstance(state, (Closed, Merged)):
        raise _not_open_error(
            state,
            hint=(
                t"Run {ui.cmd(f'jj-stack sync {head}')} to update the local stack."
                if isinstance(state, Merged)
                else t"Reopen the PR on GitHub, or run "
                t"{ui.cmd(f'jj-stack cleanup --pull-request {state.pr.number}')} and submit "
                t"again to create a new PR."
            ),
        )


def _not_open_error(state: WithPR, *, hint: Message) -> DriftError:
    pr_label = format_pr_label(state.pr.number, url=state.pr.html_url)
    return DriftError(
        t"{pr_label} for {ui.change_id(state.change_id)} is {state.pr.state} and cannot be "
        t"updated.",
        condition="pr_not_open",
        hint=hint,
    )


def require_published_base(
    *,
    base: LocalCommit,
    lookup: ChangeObservation,
    merged_hint: Message,
    remote: GitRemote,
    remote_target: CommitId | None,
    retry: str,
    tracked_base: TrackedPR,
) -> None:
    """Accept an explicit `--base` only while its PR, branch, and local copy all agree.

    Submit never touches the base, so a moved or missing base branch is not repaired here; the
    user restores it externally before retrying.
    """

    branch = tracked_base.pr_identity.head_ref
    state = classify(replace(lookup, selected=base, remote_target=remote_target))
    if isinstance(state, (PRHeadMoved, BranchMissing, BranchDisagrees)):
        remote_branch = ui.bookmark(f"{branch}@{remote.name}")
        expected = ui.semantic_text(tracked_base.submitted_baseline.commit_id, "commit_id")
        raise DriftError(
            t"PR branch {remote_branch} no longer points to the submitted commit for base "
            t"{ui.change_id(base.change_id)}. jj-stack left it untouched and cannot repair it "
            t"automatically.",
            condition="remote_branch_moved",
            hint=t"Move {remote_branch} back to commit {expected}, the commit last submitted "
            t"for the base, then run {ui.cmd(retry)}.",
        )
    if isinstance(state, Stop):
        raise stop_error(state, rerun=retry)
    if isinstance(state, Merged):
        raise _not_open_error(state, hint=merged_hint)
    if isinstance(state, Closed):
        raise _not_open_error(
            state,
            hint=t"Reopen the PR on GitHub, or run "
            t"{ui.cmd(f'jj-stack cleanup --pull-request {state.pr.number}')} and submit the "
            t"parent stack again before retrying.",
        )
