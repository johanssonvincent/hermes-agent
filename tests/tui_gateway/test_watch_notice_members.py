from pathlib import Path
import queue
import threading
from types import SimpleNamespace

from tools.process_registry_notifications import live_notice_text
from tui_gateway import server


def test_tui_notice_member_filter_uses_current_marker_state(tmp_path):
    marker = tmp_path / "returned.json"
    members = [{"job_id": "job-1", "marker_path": str(marker), "line": "result"}]
    assert live_notice_text("original", members) == "original"
    marker.touch()
    assert live_notice_text("original", members) == ""


def test_tui_notice_uses_formatted_header_and_rechecks_after_claim(monkeypatch, tmp_path):
    marker = tmp_path / "returned.json"
    event = {"type": "watch_match", "session_id": "p", "notice_members": [
        {"job_id": "job-1", "marker_path": str(marker), "line": "result"}]}
    session = {"history_lock": threading.RLock(), "running": False}
    registry = SimpleNamespace(completion_queue=queue.Queue(), is_completion_consumed=lambda _: False)
    dispatched, released = [], []
    monkeypatch.setattr(server, "_notification_event_belongs_elsewhere", lambda *a: False)
    monkeypatch.setattr(server, "_notification_event_requires_owner", lambda _: False)
    monkeypatch.setattr(server, "_notif_claim_turn", lambda _session: marker.touch() or True)
    monkeypatch.setattr(server, "_notif_release_turn", lambda _session: released.append(True))
    monkeypatch.setattr(server, "_notif_dispatch_event", lambda *args: dispatched.append(args))
    monkeypatch.setattr(server, "_emit", lambda *args: None)
    assert server._notif_handle_event("sid", session, event, set(), registry,
                                      lambda _: "[IMPORTANT: watch header]\nresult", None) is True
    assert not dispatched and released


def test_tui_notice_marker_read_error_requeues_with_backoff(monkeypatch, tmp_path):
    from tui_gateway import session_notifications

    marker = tmp_path / "returned.json"
    event = {"type": "watch_match", "session_id": "p", "notice_members": [
        {"job_id": "job-1", "marker_path": str(marker), "line": "result"}]}
    registry = SimpleNamespace(completion_queue=queue.Queue(), is_completion_consumed=lambda _: False)
    sleeps = []
    monkeypatch.setattr(server, "_notification_event_belongs_elsewhere", lambda *a: False)
    monkeypatch.setattr(server, "_notification_event_requires_owner", lambda _: False)
    monkeypatch.setattr(server, "_notif_log_failure", lambda *a: None)
    monkeypatch.setattr(session_notifications.time, "sleep", sleeps.append)
    monkeypatch.setattr(Path, "stat", lambda _self: (_ for _ in ()).throw(OSError("denied")))
    assert server._notif_handle_event("sid", {"running": False}, event, set(), registry,
                                      lambda _: "formatted", None) is True
    assert registry.completion_queue.get_nowait() is event
    assert sleeps == [0.25]
