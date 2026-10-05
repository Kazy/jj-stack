"""Inputs that stay fixed for one command run against GitHub."""

from __future__ import annotations

from dataclasses import dataclass

from jj_stack.bootstrap import CommandContext
from jj_stack.github.client import GithubClient
from jj_stack.github.resolution import GithubTarget
from jj_stack.models.github import GithubRepo


@dataclass(frozen=True, slots=True)
class ObservedTrunk:
    """The GitHub repo and the base branch at trunk, observed once per run."""

    github_repo: GithubRepo
    branch: str


@dataclass(frozen=True, slots=True)
class GithubRun:
    context: CommandContext
    dry_run: bool
    github: GithubClient
    target: GithubTarget
    trunk: ObservedTrunk | None = None
