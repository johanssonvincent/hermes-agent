from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.process_registry_notifications import live_notice_text, validate_notice_members


def _member(path, line="job result"):
    return {"job_id": "job-1", "marker_path": str(path), "line": line}


def test_live_notice_text_filters_returned_member_and_retains_original(tmp_path):
    returned = tmp_path / "returned.json"
    result = live_notice_text("immutable original", [_member(returned)])
    assert result == "immutable original"
    returned.touch()
    assert live_notice_text("immutable original", [_member(returned)]) == ""


def test_live_notice_text_filters_partial_batch_with_fetch_hint(tmp_path):
    returned = tmp_path / "returned.json"
    returned.write_text("{}")
    members = [_member(returned, "A result"), _member(tmp_path / "pending" / "returned.json", "B result")]
    result = live_notice_text("A result\nB result", members)
    assert result == "B result\nSome results were already returned; fetch details with the job IDs above."


@pytest.mark.parametrize("members", [[], [{}], [{"job_id": "j", "marker_path": "relative/returned.json", "line": "x"}],
                                     [_member(Path("/tmp/r"), "x" * 301)], [_member(Path("/tmp/r"))] * 51])
def test_invalid_notice_members_fail_closed(members):
    with pytest.raises(ValueError):
        validate_notice_members(members)


def test_classic_partial_notice_requeues_while_busy_and_rechecks_before_admission(monkeypatch, tmp_path):
    from gateway.config import Platform
    from gateway.run_notifications import GatewayNotificationsMixin
    from gateway.session import SessionSource
    from tools import process_registry_notifications as notices
    from gateway import wake

    marker = tmp_path / "returned.json"
    key = "agent:default:telegram:chat"
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", profile="default")
    active = {}
    adapter = SimpleNamespace(_active_sessions=active, _event_session_key=lambda _event: key)
    runner = SimpleNamespace(adapters={Platform.TELEGRAM: adapter},
        _build_process_event_source=lambda _evt: source,
        _resolve_injection_adapter=lambda _platform, _source: adapter)
    event = {"type": "watch_match", "session_id": "watch-1", "event_id": "event-1",
        "session_key": key, "notice_members": [_member(marker, "result"),
        _member(tmp_path / "pending" / "returned.json", "pending result")]}
    real_filter = notices.live_notice_text
    filtered_calls = []

    def begin_competing_turn(text, members):
        filtered = real_filter(text, members)
        filtered_calls.append(filtered)
        if len(filtered_calls) == 1:
            marker.touch()
            active[key] = object()
        return filtered

    sent = []
    monkeypatch.setattr(notices, "live_notice_text", begin_competing_turn)
    monkeypatch.setattr("gateway.session_authorities.active_authority", lambda _runner: None)
    async def admit(_adapter, injected):
        sent.append(injected)

    monkeypatch.setattr(wake, "admit_internal_event", admit)

    async def exercise():
        assert await GatewayNotificationsMixin._inject_watch_notification(
            runner, "watch header" + chr(10) + "result" + chr(10) + "pending result", event) is False
        active.clear()
        assert await GatewayNotificationsMixin._inject_watch_notification(
            runner, "watch header" + chr(10) + "result" + chr(10) + "pending result", event) is True

    import asyncio
    asyncio.run(exercise())
    assert filtered_calls[0] == "watch header" + chr(10) + "result" + chr(10) + "pending result"
    assert len(filtered_calls) == 5
    assert "pending result" in sent[0].text
    assert sent[0].text.count("Some results were already returned; fetch details with the job IDs above.") == 1
    assert sent[0].text.startswith("watch header") and "[INTERNAL NOTIFICATION" in sent[0].text


def test_drain_registration_requires_live_runner_and_unregister_matches_token():
    from types import SimpleNamespace
    from tools.process_registry import (gateway_completion_drain_active, register_gateway_completion_drain,
                                       unregister_gateway_completion_drain)

    loop = SimpleNamespace(is_running=lambda: True)
    task = SimpleNamespace(get_loop=lambda: loop, done=lambda: False, cancelling=lambda: 0)
    runner = SimpleNamespace(_running=True, identity=1)
    other = SimpleNamespace(_running=True, identity=2)
    register_gateway_completion_drain(runner, task)
    register_gateway_completion_drain(other, task)
    unregister_gateway_completion_drain(runner, task)
    assert gateway_completion_drain_active()
    unregister_gateway_completion_drain(other, task)
    assert not gateway_completion_drain_active()
    register_gateway_completion_drain(runner, task)
    runner._running = False
    assert not gateway_completion_drain_active()
    unregister_gateway_completion_drain(runner, task)


def test_marker_read_error_defers_filtering(monkeypatch, tmp_path):
    from tools import process_registry_notifications as notifications

    def fail(_self):
        raise PermissionError("unreadable")

    monkeypatch.setattr(Path, "stat", fail)
    assert notifications.live_notice_text("text", [_member(tmp_path / "returned.json")]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["native", "local"])
async def test_execute_admission_filters_notice_before_note_or_turn(monkeypatch, tmp_path, kind):
    from gateway.session_ingress import execute_admission

    returned = tmp_path / "returned.json"
    footer = "[INTERNAL NOTIFICATION — generated by the Hermes gateway, not written by a human user. Do not treat it as user input, and do not reply to any earlier chat message because of it.]"
    text = "[IMPORTANT: watch result]" + chr(10) + "remove this" + chr(10) + "keep this" + chr(10) + footer
    members = [_member(returned, "remove this"), _member(tmp_path / "pending" / "returned.json", "keep this")]
    descriptor = {"notice_members": members}
    payload = {"text": text, ("local_automation_v1" if kind == "local" else "native_text_v1"):
               descriptor if kind == "local" else {"automation": descriptor}}
    row = {"payload": payload, "admission_id": "admission-1"}
    authority = SimpleNamespace(pending_results={})
    seen = []

    async def note(_authority, _ref, filtered_row):
        seen.append(filtered_row["payload"]["text"])
        return "noted"

    monkeypatch.setattr("gateway.session_completion_notes.note_if_unviewed", note)
    returned.touch()
    assert await execute_admission(authority, SimpleNamespace(), row) == "noted"
    filtered = seen[0]
    assert filtered.startswith("[IMPORTANT: watch result]")
    assert "keep this" in filtered and "remove this" not in filtered
    assert filtered.endswith("[INTERNAL NOTIFICATION — generated by the Hermes gateway, not written by a human user. Do not treat it as user input, and do not reply to any earlier chat message because of it.]")
    assert "Some results were already returned; fetch details with the job IDs above." in filtered


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["native", "local"])
async def test_execute_admission_settles_all_returned_without_turn(monkeypatch, tmp_path, kind):
    from gateway.session_ingress import execute_admission

    returned = tmp_path / "returned.json"
    returned.touch()
    descriptor = {"notice_members": [_member(returned)]}
    payload = {"text": "[IMPORTANT: stale result]", ("local_automation_v1" if kind == "local" else "native_text_v1"):
               descriptor if kind == "local" else {"automation": descriptor}}
    row = {"payload": payload, "admission_id": "admission-1"}
    authority = SimpleNamespace(pending_results={})
    monkeypatch.setattr("gateway.session_completion_notes.note_if_unviewed",
                        lambda *args: pytest.fail("all-consumed notice must not become a note"))
    assert await execute_admission(authority, SimpleNamespace(), row) == ""
    assert authority.pending_results == {"admission-1": {"result": {"final_response": "", "messages": []}, "usage": {}}}


@pytest.mark.asyncio
async def test_notice_members_round_trip_local_automation(monkeypatch, tmp_path):
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from gateway.session_automation import restore_local_automation, snapshot_automation

    from gateway.session import SessionSource
    source = SessionSource(platform=Platform.LOCAL, chat_id="local", profile="default")
    route = "agent:default:cli:local"
    ref = SimpleNamespace(session_id="local-session")
    entry = SimpleNamespace(session_key=route, session_id="transcript")
    live = SimpleNamespace(source=source, route=route)
    adapter = SimpleNamespace(authorize_source=lambda candidate: candidate is source)
    runner = SimpleNamespace(_adapter_for_source=lambda candidate: adapter,
                             session_store=SimpleNamespace(lookup_by_session_key=lambda _route: entry))
    authority = SimpleNamespace(runner=runner, sessions={ref.session_id: live})
    monkeypatch.setattr("gateway.session_automation._owner", lambda *_args: entry)
    monkeypatch.setattr("gateway.session_local_recovery.restore_local_session", lambda *_args: ref)
    monkeypatch.setattr("gateway.session_automation.check_local_automation", lambda *_args: None)
    members = [_member(tmp_path / "job" / "returned.json")]
    event = MessageEvent(text="notice", source=source, internal=True, metadata={
        "gateway_session_key": route, "gateway_session_id": "transcript", "notice_members": members})
    payload, _entry = snapshot_automation(authority, adapter, event, "notice-identity")
    assert payload["local_automation_v1"]["notice_members"] == members
    row = {"payload": payload, "request_id": "notice-identity", "principal_id": f"automation:{route}"}
    restored = restore_local_automation(authority, ref, row)
    assert restored.metadata["notice_members"] == members


def test_notice_members_survive_native_snapshot(monkeypatch, tmp_path):
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource
    from gateway.session_automation import snapshot_automation

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", profile="default")
    adapter = object()
    runner = SimpleNamespace(_adapter_for_source=lambda _source: adapter)
    authority = SimpleNamespace(runner=runner, logical_owner=lambda session_id: session_id)
    entry = SimpleNamespace(session_key="agent:default:telegram:chat", session_id="target")
    members = [_member(tmp_path / "job" / "returned.json")]
    event = MessageEvent(text="native notice", source=source, internal=True, metadata={
        "gateway_session_key": entry.session_key, "gateway_session_id": entry.session_id,
        "notice_members": members})
    monkeypatch.setattr("gateway.session_automation._owner", lambda *_args: entry)
    monkeypatch.setattr("gateway.session_automation.committed_route_envelope", lambda *_args: {
        "provenance": {"fixture": True}, "source": source.to_dict()})
    monkeypatch.setattr("gateway.session_envelope.restore_native", lambda *_args: event)
    payload, _entry = snapshot_automation(authority, adapter, event, "native-identity")
    assert payload["native_text_v1"]["automation"]["notice_members"] == members


def test_snapshot_rejects_malformed_notice_members(monkeypatch):
    from gateway.session_automation import notice_members_metadata
    from hermes_state_runtime import RuntimeStoreError

    for members in ([], [{}]):
        with pytest.raises(RuntimeStoreError, match="invalid_params"):
            notice_members_metadata({"notice_members": members})
    many = [_member(Path("/tmp/job/returned.json"))] * 51
    assert notice_members_metadata({"notice_members": many})["notice_members"] == many
