#!/usr/bin/env python3
"""Find unread names, unused defaults, and unneeded `| None` by editing a copy and type-checking.

Each probe changes one spot in a scratch copy of the repo and runs pyrefly:
- rename a module-level name or class member: no new errors outside constructor calls means
  nothing reads it;
- delete a one-line parameter default: no new errors means every caller passes the argument;
- delete a trailing `| None`: no new errors means None never reaches it.
Errors only under tests/ mean only tests need it. Parameters are probed only for functions called
by name, and fields only outside pydantic models, because the type checker cannot see values from
argparse or JSON. Check each result by hand: a name read dynamically still looks unread.
"""

from __future__ import annotations

import ast
import fnmatch
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from queue import Queue

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin")
COPIED = ("src", "tests", "tools", "check.py", "pyproject.toml")
# Errors a renamed field or method causes where it is written rather than read.
WRITE_ERRORS = {"unexpected-keyword", "missing-argument", "bad-argument-count"}
VERDICTS = {
    "unread": "nothing reads it",
    "default": "no caller relies on it",
    "none": "None never reaches it",
}

type ErrorKey = tuple[str, int, str, str]


@dataclass(frozen=True)
class Probe:
    path: str
    line: int
    kind: str
    name: str
    start: int
    end: int
    replacement: str


@dataclass(frozen=True)
class _Class:
    bases: tuple[str, ...]
    members: frozenset[str]
    imports: dict[str, str]


@dataclass(frozen=True)
class _Index:
    classes: dict[str, _Class]
    called: frozenset[str]
    ignored_names: tuple[str, ...]
    ignored_decorators: tuple[str, ...]


def _member_name(node: ast.stmt) -> str | None:
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return node.name
    if isinstance(node, ast.TypeAlias):
        return node.name.id
    target = node.target if isinstance(node, ast.AnnAssign) else None
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
    return target.id if isinstance(target, ast.Name) else None


def _imports(tree: ast.Module) -> dict[str, str]:
    imports: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports |= {(a.asname or a.name): a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports |= {(a.asname or a.name): f"{node.module}.{a.name}" for a in node.names}
    return imports


def _index(trees: list[ast.Module]) -> _Index:
    classes: dict[str, _Class] = {}
    called: set[str] = set()
    for tree in trees:
        imports = _imports(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                members = frozenset(
                    name for member in node.body if (name := _member_name(member))
                )
                bases = tuple(ast.unparse(base).partition("[")[0] for base in node.bases)
                classes[node.name] = _Class(bases, members, imports)
            elif isinstance(node, ast.Call):
                function = node.func
                if isinstance(function, ast.Name):
                    called.add(function.id)
                elif isinstance(function, ast.Attribute):
                    called.add(function.attr)
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["tool"]["vulture"]
    decorators = tuple(d.removeprefix("@") for d in config["ignore_decorators"])
    return _Index(classes, frozenset(called), tuple(config["ignore_names"]), decorators)


def _inherits(index: _Index, owner: str, name: str) -> bool:
    """Whether a base of `owner` defines `name`, so reads may go through the base type."""
    for base in index.classes[owner].bases:
        local = base.rpartition(".")[2]
        if local in index.classes:
            if name in index.classes[local].members or _inherits(index, local, name):
                return True
            continue
        head, _, rest = base.partition(".")
        qualified = index.classes[owner].imports.get(head, f"builtins.{head}")
        module, _, attribute = (f"{qualified}.{rest}" if rest else qualified).rpartition(".")
        try:
            if hasattr(getattr(importlib.import_module(module), attribute), name):
                return True
        except ImportError, AttributeError:
            continue
    return False


def _is_typed_dict(index: _Index, owner: str) -> bool:
    bases = index.classes[owner].bases
    return "TypedDict" in bases or any(
        base in index.classes and _is_typed_dict(index, base) for base in bases
    )


def _ignored(index: _Index, node: ast.stmt, name: str) -> bool:
    if name.startswith("__") or any(fnmatch.fnmatch(name, p) for p in index.ignored_names):
        return True
    for decorator in getattr(node, "decorator_list", ()):
        target = ast.unparse(decorator.func if isinstance(decorator, ast.Call) else decorator)
        if any(fnmatch.fnmatch(target, pattern) for pattern in index.ignored_decorators):
            return True
    return False


def _offset(lines: list[str], line: int, col: int) -> int:
    return sum(len(text) for text in lines[: line - 1]) + col


def _unread_probe(path: str, lines: list[str], node: ast.stmt, name: str, label: str) -> Probe:
    line = node.lineno
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        col = lines[line - 1].find(f" {name}") + 1
    else:
        col = lines[line - 1].find(name, node.col_offset)
    start = _offset(lines, line, col)
    return Probe(path, line, "unread", label, start, start + len(name), f"{name}_x")


def _none_probe(
    path: str, lines: list[str], annotation: ast.expr | None, label: str
) -> Iterator[Probe]:
    if (
        isinstance(annotation, ast.BinOp)
        and isinstance(annotation.op, ast.BitOr)
        and isinstance(annotation.right, ast.Constant)
        and annotation.right.value is None
        and annotation.lineno == annotation.end_lineno
    ):
        line = annotation.lineno
        start = _offset(lines, line, annotation.left.end_col_offset or 0)
        end = _offset(lines, line, annotation.end_col_offset or 0)
        yield Probe(path, line, "none", f"{label}: {ast.unparse(annotation)}", start, end, "")


def _parameter_probes(
    path: str, lines: list[str], function: ast.FunctionDef | ast.AsyncFunctionDef
) -> Iterator[Probe]:
    arguments = function.args
    positional = arguments.posonlyargs + arguments.args
    defaults: list[tuple[ast.arg, ast.expr | None]] = [(a, None) for a in positional]
    defaults[len(positional) - len(arguments.defaults) :] = zip(
        positional[len(positional) - len(arguments.defaults) :], arguments.defaults, strict=True
    )
    defaults += zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True)
    for argument, default in defaults:
        label = f"{function.name}({argument.arg})"
        if default is not None and default.end_lineno == argument.lineno:
            start = _offset(lines, argument.lineno, argument.end_col_offset or 0)
            end = _offset(lines, argument.lineno, default.end_col_offset or 0)
            yield Probe(path, argument.lineno, "default", label, start, end, "")
        yield from _none_probe(path, lines, argument.annotation, label)
    yield from _none_probe(path, lines, function.returns, f"{function.name}()")


def _probes(path: str, tree: ast.Module, source: str, index: _Index) -> Iterator[Probe]:
    lines = source.splitlines(keepends=True)
    # Module-level annotations without values only declare names that functions assign.
    scopes: list[tuple[str | None, list[ast.stmt]]] = [
        (None, [n for n in tree.body if not (isinstance(n, ast.AnnAssign) and n.value is None)])
    ]
    owners: dict[ast.AST, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and not _is_typed_dict(index, node.name):
            scopes.append((node.name, node.body))
            owners |= dict.fromkeys(node.body, node.name)
    for owner, body in scopes:
        pydantic = owner is not None and _inherits(index, owner, "model_validate")
        for node in body:
            name = _member_name(node)
            if name is None or _ignored(index, node, name):
                continue
            if owner is None or not _inherits(index, owner, name):
                label = f"{owner}.{name}" if owner else name
                yield _unread_probe(path, lines, node, name, label)
                if isinstance(node, ast.AnnAssign) and owner and not pydantic:
                    yield from _none_probe(path, lines, node.annotation, label)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            caller_name = owners.get(node) if node.name == "__init__" else node.name
            if caller_name in index.called:
                yield from _parameter_probes(path, lines, node)


def _site_packages() -> str:
    # Naming site-packages directly skips the editable install's path back to this checkout.
    script = "import sysconfig; print(sysconfig.get_path('purelib'))"
    return subprocess.run(
        [BIN / "python", "-c", script], capture_output=True, text=True, check=True
    ).stdout.strip()


def _errors(root: Path, site_packages: str) -> set[ErrorKey]:
    command = [BIN / "pyrefly", "check", "--output-format", "json", "--summary=none"]
    completed = subprocess.run(
        [*command, "--site-package-path", site_packages],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    errors = json.loads(completed.stdout)["errors"]
    return {(e["path"], e["line"], e["name"], e["description"]) for e in errors}


def _copy(destination: Path) -> Path:
    for name in COPIED:
        source = REPO_ROOT / name
        if source.is_dir():
            ignore = shutil.ignore_patterns("__pycache__")
            shutil.copytree(source, destination / name, ignore=ignore)
        else:
            shutil.copy2(source, destination / name)
    return destination


def _verdict(probe: Probe, new: set[ErrorKey]) -> str | None:
    if probe.kind == "unread":
        new = {error for error in new if error[2] not in WRITE_ERRORS}
    if not new:
        return VERDICTS[probe.kind]
    if probe.path.startswith("src/") and all(error[0].startswith("tests/") for error in new):
        return "only tests need it"
    return None


def main(paths: list[str]) -> int:
    sources = {
        path.relative_to(REPO_ROOT).as_posix(): path.read_text()
        for root in ("src", "tests", "tools")
        for path in sorted((REPO_ROOT / root).rglob("*.py"))
    }
    trees = {path: ast.parse(source) for path, source in sources.items()}
    index = _index(list(trees.values()))
    prefixes = tuple(f"{path.rstrip('/')}/" for path in paths or ["src"])
    probes = [
        probe
        for path in sources
        if path.startswith(prefixes) or path in paths
        for probe in _probes(path, trees[path], sources[path], index)
    ]
    workers = os.cpu_count() or 4
    with tempfile.TemporaryDirectory() as scratch:
        roots: Queue[Path] = Queue()
        for number in range(workers):
            roots.put(_copy(Path(scratch) / str(number)))
        site_packages = _site_packages()
        baseline = _errors(roots.queue[0], site_packages)

        def run(probe: Probe) -> str | None:
            root = roots.get()
            file = root / probe.path
            original = file.read_text()
            try:
                file.write_text(
                    original[: probe.start] + probe.replacement + original[probe.end :]
                )
                verdict = _verdict(probe, _errors(root, site_packages) - baseline)
            finally:
                file.write_text(original)
                roots.put(root)
            return verdict and f"{probe.path}:{probe.line}: {probe.kind} {probe.name}: {verdict}"

        with ThreadPoolExecutor(workers) as pool:
            findings = [finding for finding in pool.map(run, probes) if finding]
    print("\n".join(findings) or f"No findings in {len(probes)} probes.")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
