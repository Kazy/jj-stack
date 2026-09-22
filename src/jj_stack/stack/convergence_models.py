"""Plans for updating a local stack after GitHub merges or rebases its PRs."""

from __future__ import annotations

from dataclasses import dataclass

from jj_stack.identifiers import ChangeId, CommitId
from jj_stack.models.github import GithubPR
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import TrackedPR
from jj_stack.stack.trunk_evidence import TrunkEvidenceKind


@dataclass(frozen=True, slots=True)
class OnTrunkChange:
    change_id: ChangeId
    candidate: TrackedPR
    evidence_kind: TrunkEvidenceKind
    # The still-open PR to close, or None when GitHub already finished it or rewrote it.
    close_pr: GithubPR | None
    change: LocalCommit | None


@dataclass(frozen=True, slots=True)
class ConvergenceActions:
    on_trunk: tuple[OnTrunkChange, ...]
    remaining_prs: dict[ChangeId, GithubPR]
    remaining_changes: tuple[LocalCommit, ...]
    working_copy_children: tuple[LocalCommit, ...]


@dataclass(frozen=True, slots=True)
class RewrittenPRChange:
    change_id: ChangeId
    candidate: TrackedPR
    local_change: LocalCommit
    pr: GithubPR


@dataclass(frozen=True, slots=True)
class OrdinaryConvergencePlan:
    actions: ConvergenceActions


@dataclass(frozen=True, slots=True)
class GithubStackMergePlan:
    actions: ConvergenceActions
    rewritten_changes: tuple[RewrittenPRChange, ...]
    # GitHub's reported merge result for the last merged member, when it reported one.
    merge_result_commit_id: CommitId | None


@dataclass(frozen=True, slots=True)
class GithubStackRebasePlan:
    actions: ConvergenceActions
    rewritten_changes: tuple[RewrittenPRChange, ...]


type SelectedConvergencePlan = (
    OrdinaryConvergencePlan | GithubStackMergePlan | GithubStackRebasePlan
)
