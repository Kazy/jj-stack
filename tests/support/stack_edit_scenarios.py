"""Shared stack-edit vocabulary and pure order transition model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

StackEditOperationKind = Literal[
    "abandon",
    "insert_after",
    "insert_before",
    "move_after",
    "move_before",
    "move_to_top",
    "rewrite",
    "squash_into_previous",
]


@dataclass(frozen=True, slots=True)
class StackEditOperation:
    """One user-reachable local edit applied to a linear stack."""

    kind: StackEditOperationKind
    label: str
    new_label: str | None = None
    target_label: str | None = None


@dataclass(frozen=True, slots=True)
class StackEditEffect:
    """Modeled order and rewrite consequences of one stack edit."""

    live_labels: tuple[str, ...]
    removed_label: str | None
    rewritten_labels: frozenset[str]


def move_after_candidates(live_labels: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """Return moves that change the order instead of naming the current parent."""

    return tuple(
        (label, target_label)
        for index, label in enumerate(live_labels)
        for target_index, target_label in enumerate(live_labels)
        if target_label != label and index != target_index + 1
    )


def move_before_candidates(live_labels: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """Return moves that change the order instead of naming the current child."""

    return tuple(
        (label, target_label)
        for index, label in enumerate(live_labels)
        for target_index, target_label in enumerate(live_labels)
        if target_label != label and index + 1 != target_index
    )


def apply_stack_edit(
    live_labels: tuple[str, ...],
    operation: StackEditOperation,
) -> StackEditEffect:
    """Apply one edit to label order and report its semantic effects."""

    live = list(live_labels)
    index = live.index(operation.label)
    rewritten: set[str] = set()
    removed_label: str | None = None

    if operation.kind == "abandon":
        rewritten.update(live[index + 1 :])
        removed_label = live.pop(index)
    elif operation.kind == "rewrite":
        rewritten.update(live[index:])
    elif operation.kind in {"insert_after", "insert_before"}:
        new_label = operation.new_label
        assert new_label is not None
        insert_at = index + 1 if operation.kind == "insert_after" else index
        rewritten.update(live[insert_at:])
        live.insert(insert_at, new_label)
    elif operation.kind == "move_to_top":
        rewritten.update(live[index:])
        live.pop(index)
        live.append(operation.label)
    elif operation.kind in {"move_after", "move_before"}:
        target = operation.target_label
        assert target is not None
        target_index = live.index(target)
        rewritten.update(live[min(index, target_index) :])
        live.pop(index)
        target_index = live.index(target)
        insert_at = target_index + 1 if operation.kind == "move_after" else target_index
        live.insert(insert_at, operation.label)
    elif operation.kind == "squash_into_previous":
        rewritten.update(live[index - 1 :])
        removed_label = live.pop(index)

    return StackEditEffect(
        live_labels=tuple(live),
        removed_label=removed_label,
        rewritten_labels=frozenset(rewritten),
    )
