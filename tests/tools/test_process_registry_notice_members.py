from tools.process_registry_notifications import (
    format_process_notification,
    live_notice_text,
    validate_notice_members,
)


def _member(path, line):
    return {"job_id": f"job-{line}", "marker_path": str(path), "line": line}


def test_validate_notice_members_accepts_more_than_fifty(tmp_path):
    members = [
        _member(tmp_path / f"job-{index}" / "returned.json", f"result {index}")
        for index in range(51)
    ]

    assert validate_notice_members(members) == members


def test_partial_notice_adds_hint_only_when_returned_line_was_removed(tmp_path):
    returned = tmp_path / "returned.json"
    returned.touch()
    members = [
        _member(returned, "already returned"),
        _member(tmp_path / "pending" / "returned.json", "still pending"),
    ]
    hint = "Some results were already returned; fetch details with the job IDs above."

    filtered = live_notice_text("already returned\nstill pending", members)
    assert filtered == f"still pending\n{hint}"

    original = "count: 2\nstill pending\n"
    assert live_notice_text(original, members) == original


def test_terminal_process_notification_without_notice_members_is_unchanged():
    event = {"type": "completion", "session_id": "process-1", "command": "echo done",
             "exit_code": 0, "output": "done"}

    assert format_process_notification(event) == (
        "[IMPORTANT: Background process process-1 completed normally (exit code 0).\n"
        "Command: echo done\nOutput:\ndone]"
    )


def test_empty_notice_members_remain_invalid():
    import pytest

    with pytest.raises(ValueError, match="invalid notice_members count"):
        validate_notice_members([])
