"""Pure classification of whether a tracked pull request's work is on trunk.

Check whether the submitted commit or GitHub's rewritten merge result is an ancestor of trunk.
An open PR must still point to the submitted commit. A merged PR's head may have moved past it,
for example through GitHub's update-branch merge, so a merged PR instead needs the submitted
commit inside the head that landed. A PR's merged state alone does not show that its work
reached this repo's trunk.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

import jj_stack.ui as ui
from jj_stack.formatting import format_pr_label
from jj_stack.identifiers import CommitId
from jj_stack.models.github import GithubPR
from jj_stack.models.tracking import TrackedPR
from jj_stack.ui import Message

# `in_landed_head`: not on trunk, but GitHub reports it in the head of a PR that landed.
# `outside_landed_head`: GitHub does not report it in that head, so the head replaced it.
CommitAncestry = Literal[
    "not_on_trunk", "on_trunk", "unresolved", "in_landed_head", "outside_landed_head"
]
# `replaced`: the PR landed from a head without the submitted commit. Only the local change's
# contents, compared with trunk, can show whether all of it landed.
TrunkEvidenceKind = Literal["exact", "rewritten", "replaced"]


def classify_trunk_evidence(
    *,
    ancestries: Mapping[CommitId, CommitAncestry],
    candidate: TrackedPR,
    pr: GithubPR,
) -> tuple[TrunkEvidenceKind | None, Message]:
    """Return the kind of evidence that the work reached trunk, or why neither check passed."""

    pr_label = format_pr_label(pr.number, url=pr.html_url)
    submitted = candidate.submitted_baseline.commit_id
    ancestry = ancestries[submitted]
    merged = pr.state == "merged"
    if ancestry == "on_trunk" and (merged or pr.head.sha == submitted):
        return "exact", ""
    if merged and ancestry == "in_landed_head":
        return "rewritten", ""
    if merged and ancestry == "outside_landed_head":
        return "replaced", ""
    if pr.head.sha != submitted:
        return None, t"{pr_label} no longer points to the last submitted commit"
    if not merged:
        return None, t"{pr_label} is {pr.state} without a result on trunk"
    merge_commit_id = pr.merge_commit_sha
    if merge_commit_id is None:
        return None, t"GitHub did not report the commit produced by merging {pr_label}"
    merge_ancestry = ancestries.get(merge_commit_id)
    if merge_ancestry == "on_trunk":
        return "rewritten", ""
    if merge_ancestry == "unresolved":
        return None, (
            t"commit {ui.commit_id(merge_commit_id)} from GitHub's merge is unavailable locally"
        )
    return None, t"commit {ui.commit_id(merge_commit_id)} from GitHub's merge is not on trunk"


def landed_head_checks(
    prs: Sequence[tuple[TrackedPR, GithubPR]],
    ancestries: Mapping[CommitId, CommitAncestry],
) -> dict[CommitId, CommitId]:
    """Map each merged PR's submitted commit to the landed head GitHub must show it in.

    A PR lands when its merge result is on trunk. A PR merged into another tracked PR's branch
    lands with that PR, as when the top PR of a stack is merged on GitHub first. Pairs the merge
    result check already covers, and submitted commits already on trunk, need no lookup.
    """

    merged = {tracked.pr_identity.head_ref: pr for tracked, pr in prs if pr.state == "merged"}
    checks: dict[CommitId, CommitId] = {}
    for tracked, pr in prs:
        submitted = tracked.submitted_baseline.commit_id
        if pr.state != "merged" or ancestries[submitted] == "on_trunk":
            continue
        landing = pr
        # Bounded by the merged PRs because a cycle of base branches never lands.
        for _ in merged:
            if _landed(landing, ancestries) or (parent := merged.get(landing.base.ref)) is None:
                break
            landing = parent
        if _landed(landing, ancestries) and (landing is not pr or pr.head.sha != submitted):
            checks[submitted] = landing.head.sha
    return checks


def _landed(pr: GithubPR, ancestries: Mapping[CommitId, CommitAncestry]) -> bool:
    return pr.merge_commit_sha is not None and ancestries.get(pr.merge_commit_sha) == "on_trunk"
