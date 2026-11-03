"""The Slack approval display uses a safe summary, not the raw command."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


def _ensure_slack_mock():
    if "slack_bolt" in sys.modules:
        return
    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    sys.modules["slack_bolt"] = slack_bolt
    sys.modules["slack_bolt.async_app"] = slack_bolt.async_app
    handler_mod = MagicMock()
    handler_mod.AsyncSocketModeHandler = MagicMock
    sys.modules["slack_bolt.adapter"] = MagicMock()
    sys.modules["slack_bolt.adapter.socket_mode"] = MagicMock()
    sys.modules["slack_bolt.adapter.socket_mode.async_handler"] = handler_mod
    sdk_mod = MagicMock()
    sdk_mod.web = MagicMock()
    sdk_mod.web.async_client = MagicMock()
    sdk_mod.web.async_client.AsyncWebClient = MagicMock
    sys.modules["slack_sdk"] = sdk_mod
    sys.modules["slack_sdk.web"] = sdk_mod.web
    sys.modules["slack_sdk.web.async_client"] = sdk_mod.web.async_client


_ensure_slack_mock()

from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter
from tools import approval as _approval
from tools.approval_gateway_wait import _ApprovalEntry


SESSION = "agent:main:slack:dm:C1"
_RAW_COMMAND = "rm -rf /Users/demo/customer/CANARY_PATH_X.csv"
_RAW_DESCRIPTION = "CANARY_DESCRIPTION_X /Users/demo/private"
_EXPECTED_SUMMARY = "rm — delete (recursive, forced) · target <home> · explicit sudo: no · args hidden"


class _TextSlackAdapter(SlackAdapter):
    @classmethod
    def supports_exec_approval_buttons(cls):
        return False


class _LegacyAdapter:
    typed_command_prefix = "/"

    def __init__(self):
        self.messages = []

    def pause_typing_for_chat(self, _chat_id):
        return None

    async def send(self, _chat_id, message, **_kwargs):
        self.messages.append(message)
        return None


def _make_adapter(adapter_class=SlackAdapter):
    adapter = adapter_class(PlatformConfig(enabled=True, token="«redacted:xox…»"))
    adapter._app = MagicMock()
    adapter._bot_user_id = "U_BOT"
    adapter._team_clients = {"T1": AsyncMock()}
    adapter._team_bot_user_ids = {"T1": "U_BOT"}
    adapter._channel_team = {"C1": "T1"}
    adapter._team_clients["T1"].chat_postMessage = AsyncMock(return_value={"ts": "1234.5678"})
    adapter._team_clients["T1"].chat_update = AsyncMock(return_value={"ok": True, "ts": "1234.5678"})
    return adapter


def _runner(adapter, monkeypatch):
    from gateway.run_turn_runner import TurnRunner
    from pm import environments

    monkeypatch.setattr(environments, "activate_dependencies", lambda _root: None)
    runner = object.__new__(TurnRunner)
    runner._ctx = MagicMock()
    runner._ctx._status_adapter = adapter
    runner._ctx._status_chat_id = "C1"
    runner._ctx._status_thread_metadata = {"team_id": "T1", "thread_id": "t1"}
    runner._ctx.session_key = SESSION
    runner._ctx.source = MagicMock()
    runner._ctx.source.chat_id = "C1"
    runner._ctx.source.platform = "slack"
    runner._ctx.source.session_key = SESSION

    class _Fut:
        def __init__(self, result):
            self._result = result

        def result(self, timeout=None):
            return self._result

    def _schedule(coro, _label):
        return _Fut(asyncio.run(coro))

    setattr(runner, "_schedule", lambda coro, log_message, loop=None: _schedule(coro, log_message))
    setattr(runner, "_close_native_stream_boundary", lambda _why, placeholder=None, reopen=False: True)
    return runner


def _approval_data(**extra):
    return {
        "command": _RAW_COMMAND,
        "description": _RAW_DESCRIPTION,
        "pattern_key": "test-rule",
        "pattern_keys": ["test-rule"],
        "request_id": None,
        **extra,
    }


def _assert_no_private_markers(text):
    for marker in ("CANARY_PATH_X", "CANARY_DESCRIPTION_X", "/Users/demo", "/private"):
        assert marker not in text


def test_runner_posts_safe_slack_card_and_keeps_approval_actions(monkeypatch):
    adapter = _make_adapter()
    approval_data = _approval_data(allow_session=True, allow_permanent=True)
    original = dict(approval_data)

    _runner(adapter, monkeypatch)._approval_notify_sync(approval_data)

    payload = adapter._team_clients["T1"].chat_postMessage.call_args.kwargs
    rendered = json.dumps(payload, ensure_ascii=False)
    assert _EXPECTED_SUMMARY in rendered
    _assert_no_private_markers(rendered)
    action_ids = [element["action_id"] for element in payload["blocks"][1]["elements"]]
    assert action_ids == ["hermes_approve_once", "hermes_approve_session", "hermes_approve_always", "hermes_deny"]
    assert approval_data == original


def test_runner_posts_safe_plain_text_fallback(monkeypatch):
    adapter = _make_adapter(_TextSlackAdapter)
    approval_data = _approval_data(allow_session=True, allow_permanent=True)

    _runner(adapter, monkeypatch)._approval_notify_sync(approval_data)

    payload = adapter._team_clients["T1"].chat_postMessage.call_args.kwargs
    rendered = json.dumps(payload, ensure_ascii=False)
    assert _EXPECTED_SUMMARY in rendered
    _assert_no_private_markers(rendered)
    assert "!approve" in payload["text"]


def test_timeout_card_edit_reuses_safe_slack_summary(monkeypatch):
    monkeypatch.setattr("gateway.platforms.base_exec_approval.approval_timeout_seconds", lambda: 300)
    adapter = _make_adapter()
    approval_data = _approval_data(request_id="request-1")
    entry = _ApprovalEntry(dict(approval_data))
    with _approval._lock:
        _approval._gateway_queues[SESSION] = [entry]
    try:
        _runner(adapter, monkeypatch)._approval_notify_sync(dict(entry.data))
        assert entry.settle is not None
        entry.settle("timeout")
        payload = adapter._team_clients["T1"].chat_update.call_args.kwargs
        rendered = json.dumps(payload, ensure_ascii=False)
        assert _EXPECTED_SUMMARY in rendered
        _assert_no_private_markers(rendered)
        assert "NOT run" in rendered
    finally:
        with _approval._lock:
            _approval._gateway_queues.pop(SESSION, None)


def test_summary_capability_is_slack_only():
    assert SlackAdapter.supports_approval_summary is True
    assert _TextSlackAdapter.supports_approval_summary is True


def test_non_slack_approval_display_keeps_existing_command_text(monkeypatch):
    adapter = _LegacyAdapter()
    approval_data = {"command": "git push /tmp/CANARY_PATH_X", "description": "legacy reason"}

    _runner(adapter, monkeypatch)._approval_notify_sync(approval_data)

    assert "/tmp/CANARY_PATH_X" in adapter.messages[0]
    assert "legacy reason" in adapter.messages[0]
    assert approval_data["command"] == "git push /tmp/CANARY_PATH_X"
