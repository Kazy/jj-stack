from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

JsonOutputKind = Literal["list", "view"]

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "docs" / "json-output.schema.json"


def assert_json_output_matches_schema(payload: object, kind: JsonOutputKind) -> None:
    _assert_matches(payload, f"{kind}Output")


def parse_jsonl_output(text: str) -> list[dict[str, Any]]:
    """Parse `--output=jsonl` stdout, checking that every line is one valid record."""

    records = [json.loads(line) for line in text.splitlines()]
    for record in records:
        _assert_matches(record, "jsonlRecord")
    return records


def _assert_matches(payload: object, definition: str) -> None:
    validator = _validator(definition)
    errors = sorted(validator.iter_errors(payload), key=_error_sort_key)
    assert not errors, _format_errors(errors)


@cache
def _validator(definition: str) -> Draft202012Validator:
    root_schema = _schema()
    schema = {
        "$schema": root_schema["$schema"],
        "$defs": root_schema["$defs"],
        "$ref": f"#/$defs/{definition}",
    }
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


@cache
def _schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _error_sort_key(error: ValidationError) -> tuple[str, str]:
    path = ".".join(str(part) for part in error.absolute_path)
    return path, error.message


def _format_errors(errors: list[ValidationError]) -> str:
    lines = []
    for error in errors:
        path = ".".join(str(part) for part in error.absolute_path) or "<root>"
        lines.append(f"{path}: {error.message}")
    return "\n".join(lines)
