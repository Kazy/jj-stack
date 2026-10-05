"""Remove a GitHub stack without closing its pull requests.

The PRs keep their base branches and dependencies. Local changes and saved pull request links
stay in place. Submitting the same local stack again recreates the GitHub stack.

With a revset or pull request, `unstack` uses the matching local stack. Use
`--stack <number>` when the GitHub stack no longer corresponds to a single local stack.

`--local` only forgets `jj-stack`'s saved pull request links. It does not change GitHub, close
pull requests, delete PR branches, or modify local changes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, GlobalOptions, bootstrap_context
from jj_stack.commands.cleanup.actions import UNTRUSTED_PR_STATES
from jj_stack.errors import CliError, UsageError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.error_messages import require_github_target
from jj_stack.github.resolution import resolve_github_target
from jj_stack.identifiers import ChangeId, short_change_id
from jj_stack.models.github import GithubStack
from jj_stack.models.tracking import TrackingState
from jj_stack.stack.change_state import classify, stop_error
from jj_stack.stack.github_stack_safety import (
    dissolve_github_stack,
    selected_github_stack,
)
from jj_stack.stack.pr_facts import observe_github_stacks, observe_prs
from jj_stack.stack.preparation import PreparedLocalStack, prepare_local_stack
from jj_stack.stack.selection import (
    resolve_linked_change_for_pr,
)
from jj_stack.state.operation_lock import operation_lock

HELP = "Remove a GitHub stack without closing its pull requests"


@dataclass(frozen=True, slots=True)
class LocalUnstackAction:
    """One saved pull request link forgotten by `unstack --local`."""

    branch: str
    change_id: ChangeId
    subject: str


@dataclass(frozen=True, slots=True)
class LocalUnstackResult:
    """Result of forgetting saved pull request links."""

    actions: tuple[LocalUnstackAction, ...]
    dry_run: bool


def unstack(
    *,
    global_options: GlobalOptions,
    dry_run: bool,
    local: bool,
    pr: str | None,
    revset: str | None,
    stack: int | None,
) -> int:
    """CLI entrypoint for `unstack`."""

    if stack is not None and (local or pr is not None or revset is not None):
        raise UsageError(
            "jj-stack unstack --stack cannot be combined with --local, --pull-request, "
            "or a revset."
        )
    if stack is not None and stack < 1:
        raise UsageError("jj-stack unstack --stack requires a positive GitHub stack number.")

    context = bootstrap_context(global_options)
    command = "unstack --local" if local else "unstack"
    with operation_lock(
        context.state_store,
        command=command,
        mutating=not dry_run,
    ):
        if stack is not None:
            return asyncio.run(_run_github_unstack(context, dry_run=dry_run, target=stack))
        selected = _resolve_local_stack(context=context, pr=pr, revset=revset)
        if local:
            _print_local_unstack_result(_run_local_unstack(context, dry_run, selected))
            return 0
        return asyncio.run(_run_github_unstack(context, dry_run=dry_run, target=selected))


async def _run_github_unstack(
    context: CommandContext,
    *,
    dry_run: bool,
    target: int | PreparedLocalStack,
) -> int:
    """Remove GitHub stack number `target`, or the one holding the selected local stack."""

    github_target = require_github_target(
        resolve_github_target(context.jj_client.list_git_remotes())
    )

    async with context.open_github_client(repo=github_target.repo) as github_client:
        if isinstance(target, int):
            stack_number = target
            github_stack = await _get_github_stack(
                github_client=github_client,
                stack_number=stack_number,
            )
            if github_stack is None:
                console.output(t"No GitHub stack #{stack_number} was found.")
                return 0
            if not github_stack.active_pr_numbers:
                console.output(
                    t"GitHub stack #{stack_number} contains only merged PRs. "
                    t"GitHub keeps them as history; there is nothing to remove."
                )
                return 0
        else:
            state = target.state
            changes = target.stack.changes
            change_ids = tuple(c.change_id for c in changes if c.change_id in state.prs)
            pr_numbers = tuple(state.prs[c].pr_identity.pr_number for c in change_ids)
            if not pr_numbers:
                console.output("No saved pull request links were found for the selected stack.")
                return 0
            selected = set(pr_numbers)
            observed = tuple(
                stack
                for stack in await observe_github_stacks(github=github_client)
                if not selected.isdisjoint(stack.active_pr_numbers)
            )
            github_stack = selected_github_stack(github_target.repo, pr_numbers, observed)
            await _check_selected_prs(
                change_ids=change_ids,
                context=context,
                github_client=github_client,
                remote_name=github_target.remote.name,
                state=state,
            )

        if github_stack is not None and not dry_run:
            await dissolve_github_stack(github_client=github_client, stack=github_stack)

    if github_stack is None:
        console.output("No GitHub stack was found for the selected pull requests.")
        return 0
    action = "Would remove" if dry_run else "Removed"
    console.output(t"{action} GitHub stack #{github_stack.number}.")
    return 0


async def _get_github_stack(
    *,
    github_client: GithubClient,
    stack_number: int,
) -> GithubStack | None:
    try:
        return await github_client.get_stack(stack_number=stack_number)
    except GithubClientError as error:
        if error.status_code == 404:
            return None
        raise CliError(t"Could not inspect GitHub stack #{stack_number}.") from error


async def _check_selected_prs(
    *,
    change_ids: tuple[ChangeId, ...],
    context: CommandContext,
    github_client: GithubClient,
    remote_name: str,
    state: TrackingState,
) -> None:
    try:
        observation = await observe_prs(
            change_ids=change_ids,
            context=context,
            github_client=github_client,
            remote_name=remote_name,
            state=state,
        )
    except GithubClientError as error:
        raise CliError("Could not inspect the selected pull requests.") from error

    rerun = f"jj-stack unstack {short_change_id(change_ids[-1])}"
    for change_id in change_ids:
        pr_state = classify(observation.prs[change_id])
        if isinstance(pr_state, UNTRUSTED_PR_STATES):
            raise stop_error(pr_state, rerun=rerun)


def _run_local_unstack(
    context: CommandContext,
    dry_run: bool,
    selected: PreparedLocalStack,
) -> LocalUnstackResult:
    actions: list[LocalUnstackAction] = []
    for change in selected.stack.changes:
        tracked_pr = selected.state.prs.get(change.change_id)
        if tracked_pr is None:
            continue
        actions.append(
            LocalUnstackAction(
                branch=tracked_pr.pr_identity.head_ref,
                change_id=change.change_id,
                subject=change.subject,
            )
        )
    if not dry_run:
        for action in actions:
            context.state_store.remove_pr(action.change_id)
    return LocalUnstackResult(actions=tuple(actions), dry_run=dry_run)


def _resolve_local_stack(
    *,
    context: CommandContext,
    pr: str | None,
    revset: str | None,
) -> PreparedLocalStack:
    if pr is not None:
        revset, note = resolve_linked_change_for_pr(
            context=context,
            pr_reference=pr,
            revset=revset,
        )
        console.note(note)
    with console.spinner(description="Inspecting jj stack"):
        return prepare_local_stack(
            context=context, fetch_remote_state=False, revset=revset, containing_change_id=None
        )


def _print_local_unstack_result(result: LocalUnstackResult) -> None:
    if not result.actions:
        console.output("No saved pull request links were found for the selected stack.")
        return
    heading = (
        "Would forget saved pull request links:"
        if result.dry_run
        else ("Forgot saved pull request links:")
    )
    console.output(heading)
    icon = "~" if result.dry_run else "✓"
    for action in result.actions:
        change_label = t"{action.subject} ({ui.change_id(action.change_id)})"
        console.output(
            t"  {icon} forget {change_label}; leave {ui.bookmark(action.branch)} unchanged"
        )
