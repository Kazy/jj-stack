from __future__ import annotations

import json

from ..support.integration_helpers import commit_file, init_fake_github_repo
from ..support.json_schema import parse_jsonl_output
from .submit_command_helpers import configure_submit_environment, run_main


def test_jsonl_streams_every_message_and_activity_as_valid_records(
    tmp_path, monkeypatch, capsys
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "one.txt")
    commit_file(repo, "feature 2", "two.txt")

    assert run_main(repo, config_path, "submit", "--output=jsonl") == 0
    captured = capsys.readouterr()
    assert not captured.err
    records = parse_jsonl_output(captured.out)
    statuses = [record for record in records if record["type"] == "status"]
    assert {
        "type": "status",
        "text": "Syncing pull requests",
        "completed": 2,
        "total": 2,
    } in statuses
    # No activity outlives its work, so a GUI never shows a stale spinner.
    assert statuses[-1]["text"] is None
    assert any(record["type"] == "output" for record in records)
    assert tuple(fake_repo.prs) == (1, 2)

    assert run_main(repo, config_path, "view", "--json") == 0
    document = json.loads(capsys.readouterr().out)
    assert run_main(repo, config_path, "view", "--json", "--output=jsonl") == 0
    records = parse_jsonl_output(capsys.readouterr().out)
    assert [record["data"] for record in records if record["type"] == "result"] == [document]
