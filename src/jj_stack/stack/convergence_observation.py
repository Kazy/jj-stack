"""Read the additional repo state needed to plan sync for a local stack."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace

from jj_stack.bootstrap import CommandContext
from jj_stack.concurrency import wait_for_read_tasks
from jj_stack.github.client import GithubClient
from jj_stack.identifiers import ChangeId, CommitId
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.models.github import GithubStack
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import TrackingState
from jj_stack.stack.change_state import TrackedPRObservation
from jj_stack.stack.pr_facts import (
    RepoFacts,
    observe_prs,
)
from jj_stack.stack.repo import observe_repo_paths
from jj_stack.stack.trunk_evidence import CommitAncestry


def submitted_changes_below(
    *, context: CommandContext, selected: tuple[LocalCommit, ...], state: TrackingState
) -> tuple[ChangeId, ...]:
    """Tracked changes under the bottom selected change in submitted history, nearest first.

    Rebasing a stack onto trunk with `jj` after a merge removes the merged change from the local
    path, but the submitted commits still record which tracked change was below.
    """

    selected_ids = {change.change_id for change in selected}
    by_submitted = {
        tracked.submitted_baseline.commit_id: change_id
        for change_id, tracked in state.prs.items()
        if change_id not in selected_ids
    }
    if not by_submitted or (bottom := state.prs.get(selected[0].change_id)) is None:
        return ()
    start = bottom.submitted_baseline.commit_id
    commits = {
        item.commit_id: item
        for item in context.jj_client.query_commits_by_ids((start, *by_submitted))
    }
    below: list[ChangeId] = []
    commit = commits.get(start)
    # Submitted history is acyclic, so each step moves to a new tracked change.
    while commit is not None and len(commit.parents) == 1 and commit.parents[0] in by_submitted:
        below.append(by_submitted[commit.parents[0]])
        commit = commits.get(commit.parents[0])
    return tuple(below)


def changes_emptied_on_trunk(
    *,
    ancestries: Mapping[CommitId, CommitAncestry],
    context: CommandContext,
    observation: RepoFacts,
    trunk_commit_id: CommitId,
) -> frozenset[ChangeId]:
    """Changes whose PR landed from a replaced head and whose local copy adds nothing to trunk.

    A speculative rebase onto trunk answers without changing the repo. An overlapping edit
    conflicts rather than reverting what landed, so a conflicted copy is never empty.
    """

    copies = {
        change_id: mutable[0]
        for change_id, item in observation.prs.items()
        if ancestries.get(item.tracked.submitted_baseline.commit_id) == "outside_landed_head"
        and len(mutable := tuple(copy for copy in item.local if not copy.immutable)) == 1
    }
    if not copies:
        return frozenset()
    operation_id = context.jj_client.prepare_rebase_commits(
        commit_ids=tuple(copy.commit_id for copy in copies.values()),
        destination=trunk_commit_id,
    )
    rebased = context.jj_client.query_commits_by_change_ids(
        tuple(copies), cli_args=JjCliArgs((f"--at-op={operation_id}",))
    )
    return frozenset(
        change_id
        for change_id, commits in rebased.items()
        if (moved := tuple(commit for commit in commits if not commit.immutable))
        and all(commit.empty for commit in moved)
    )


async def complete_sync_observation(
    *,
    below: tuple[ChangeId, ...],
    context: CommandContext,
    github: GithubClient,
    initial: RepoFacts,
    remote_name: str,
    selected: tuple[LocalCommit, ...],
    stacks: tuple[GithubStack, ...],
    state: TrackingState,
) -> tuple[RepoFacts, bool]:
    selected_prs = {
        tracked.pr_identity.pr_number
        for change in selected
        if (tracked := state.prs.get(change.change_id)) is not None
    }
    affected = tuple(stack for stack in stacks if not selected_prs.isdisjoint(stack.pr_numbers))
    resource_prs = {number for stack in affected for number in stack.pr_numbers}
    tracked_prs = {tracked.pr_identity.pr_number for tracked in state.prs.values()}
    if not any(
        _pr_changed(observed, include_remote_target=False)
        for change_id in (*below, *(change.change_id for change in selected))
        if (observed := initial.prs.get(change_id)) is not None
    ) and (resource_prs & tracked_prs).issubset(selected_prs):
        return initial, False
    change_ids = tuple(
        change_id
        for change_id, tracked in state.prs.items()
        if tracked.pr_identity.pr_number in resource_prs
    )
    missing_ids = tuple(change_id for change_id in change_ids if change_id not in initial.prs)
    missing = (
        await observe_prs(
            branch_reads="none",
            change_ids=missing_ids,
            context=context,
            github_client=github,
            github_repo_snapshot=initial.github_repo,
            remote_name=remote_name,
            state=state,
        )
        if missing_ids
        else None
    )
    prs = {
        **initial.prs,
        **(missing.prs if missing is not None else {}),
    }
    identities = tuple(item.tracked.pr_identity for item in prs.values())
    heads = tuple(identity.head_ref for identity in identities)
    targets_task = asyncio.create_task(github.get_branch_targets(branches=heads))
    open_prs_task = asyncio.create_task(github.get_open_prs_by_head_refs(head_refs=heads))
    await wait_for_read_tasks(targets_task, open_prs_task)
    targets, open_prs = targets_task.result(), open_prs_task.result()
    observation = replace(
        initial,
        prs={
            change_id: replace(
                item,
                remote_target=targets.get(item.tracked.pr_identity.head_ref),
                open_prs_on_branch=open_prs.get(item.tracked.pr_identity.head_ref, ()),
            )
            for change_id, item in prs.items()
        },
    )
    changed = any(_pr_changed(item) for item in observation.prs.values())
    return observation, changed


def queued_pr_numbers(
    observation: RepoFacts,
    selected: tuple[LocalCommit, ...],
) -> tuple[int, ...]:
    return tuple(
        pr.number
        for change in selected
        if (observed := observation.prs.get(change.change_id)) is not None
        and (pr := observed.pr) is not None
        and pr.state == "open"
        and pr.is_queued
    )


def dependent_path_heads(
    *,
    ancestor_commit_ids: tuple[CommitId, ...],
    context: CommandContext,
    excluded_change_ids: frozenset[ChangeId],
) -> dict[CommitId, tuple[LocalCommit, ...]]:
    if not ancestor_commit_ids:
        return {}
    paths = observe_repo_paths(
        jj_client=context.jj_client,
        descendant_of=ancestor_commit_ids,
        state=context.state_store.load(),
    ).paths
    result: dict[CommitId, tuple[LocalCommit, ...]] = {}
    for ancestor in ancestor_commit_ids:
        heads: dict[CommitId, LocalCommit] = {}
        for path in paths:
            if not any(item.commit_id == ancestor for item in path.stack.changes):
                continue
            head = next(
                (
                    change
                    for change in reversed(path.stack.changes)
                    if change.change_id not in excluded_change_ids
                ),
                None,
            )
            if head is not None:
                heads[head.commit_id] = head
        result[ancestor] = tuple(heads.values())
    return result


def _pr_changed(
    observed: TrackedPRObservation,
    *,
    include_remote_target: bool = True,
) -> bool:
    pr = observed.pr
    if pr is None:
        return True
    baseline = observed.tracked.submitted_baseline.commit_id
    return (
        pr.state == "merged"
        or pr.head.sha != baseline
        or (include_remote_target and observed.remote_target != baseline)
        or any(commit.immutable for commit in observed.local)
    )
