#!/usr/bin/env python3
"""Report wide function signatures and parameters threaded through calls unchanged.

The report lists the widest signatures, parameters passed down a chain of functions that only
forward them, parameter groups that recur across signatures, and keyword arguments that receive
the same expression at every call site. Callees are matched by name, so read it as leads.
"""

from __future__ import annotations

import ast
import itertools
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOP = 15

type Function = ast.FunctionDef | ast.AsyncFunctionDef


def _parameters(function: Function) -> list[str]:
    arguments = function.args
    names = arguments.posonlyargs + arguments.args + arguments.kwonlyargs
    return [a.arg for a in names if a.arg not in ("self", "cls")]


def _callee(call: ast.Call) -> str | None:
    function = call.func
    if isinstance(function, ast.Attribute):
        return function.attr
    return function.id if isinstance(function, ast.Name) else None


def _forwarded_to(function: Function, parameter: str) -> set[str] | None:
    """Callees that receive `parameter` under its own name, or None if the body uses it."""
    callees: set[str] = set()
    forwarded: set[int] = set()
    for call in ast.walk(function):
        if not isinstance(call, ast.Call):
            continue
        for keyword in call.keywords:
            value = keyword.value
            if keyword.arg == parameter and isinstance(value, ast.Name) and value.id == parameter:
                callees.add(_callee(call) or "?")
                forwarded.add(id(value))
    for node in ast.walk(function):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id == parameter and id(node) not in forwarded:
                return None
    return callees or None


def _chain(forwards: dict[tuple[str, str], set[str]], name: str, parameter: str) -> list[str]:
    longest: list[str] = []
    for callee in forwards.get((name, parameter), ()):
        if (callee, parameter) in forwards and callee != name:
            longest = max(longest, _chain(forwards, callee, parameter), key=len)
    return [name, *longest]


def main(paths: list[str]) -> int:
    trees = [
        ast.parse(path.read_text())
        for root in paths or ["src"]
        for path in sorted((REPO_ROOT / root).rglob("*.py"))
    ]
    functions = [
        node
        for tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    signatures = {function: _parameters(function) for function in functions}
    total = sum(len(parameters) for parameters in signatures.values())
    print(f"{len(functions)} functions, {total} parameters")
    widths = Counter(min(len(parameters), 8) for parameters in signatures.values())
    print("Functions by width (8 means 8+):", dict(sorted(widths.items())))

    print("\nWidest signatures:")
    for function, parameters in sorted(signatures.items(), key=lambda i: -len(i[1]))[:TOP]:
        print(f"  {len(parameters):2} {function.name}({', '.join(parameters)})")

    forwards = {
        (function.name, parameter): callees
        for function, parameters in signatures.items()
        for parameter in parameters
        if (callees := _forwarded_to(function, parameter))
    }
    print(f"\n{len(forwards)} parameters are only forwarded to callees under the same name.")
    chains = {tuple(_chain(forwards, *key)) + (key[1],) for key in forwards}
    print("Longest forwarding chains:")
    for chain in sorted(chains, key=len, reverse=True)[:TOP]:
        if len(chain) > 3:
            print(f"  {chain[-1]}: {' -> '.join(chain[:-1])}")

    for size in (3, 4):
        groups = Counter(
            group
            for parameters in signatures.values()
            for group in itertools.combinations(sorted(parameters), size)
        )
        print(f"\nParameter groups of {size} shared by the most signatures:")
        for group, count in groups.most_common(TOP // 3):
            print(f"  {count:2} ({', '.join(group)})")

    names = {function.name for function in functions}
    values: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    for tree in trees:
        for call in ast.walk(tree):
            callee = _callee(call) if isinstance(call, ast.Call) else None
            if isinstance(call, ast.Call) and callee is not None and callee in names:
                for keyword in call.keywords:
                    if keyword.arg is not None:
                        values[(callee, keyword.arg)].append(ast.unparse(keyword.value))
    constant = [(len(v), k, v[0]) for k, v in values.items() if len(v) > 1 and len(set(v)) == 1]
    print("\nKeyword arguments given the same expression at every call site:")
    for count, (callee, keyword), value in sorted(constant, reverse=True)[:TOP]:
        print(f"  {count:2}x {callee}({keyword}={value})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
