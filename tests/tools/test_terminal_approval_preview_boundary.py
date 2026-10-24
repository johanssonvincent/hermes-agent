"""The Slack preview boundary never changes terminal risk or execution inputs."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tools import terminal_tool


_RAW_COMMAND = "git push /tmp/CANARY_PATH_X"


class _FakeEnv:
    host_cwd = None

    def __init__(self):
        self.commands = []

    def execute(self, command, **_kwargs):
        self.commands.append(command)
        return {"output": "ok", "returncode": 0}


def _install_runtime(monkeypatch, decision_factory):
    plan = terminal_tool._ExecPlan(
        config={}, env_type="local", effective_task_id="task-1", image="", cwd="/workspace",
        host_cwd=None, effective_timeout=10,
    )
    env = _FakeEnv()
    risk_commands = []

    def check(command, env_type, has_host_access):
        risk_commands.append(command)
        return decision_factory(command)

    monkeypatch.setattr(terminal_tool, "_plan_execution", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(terminal_tool, "_acquire_env", lambda *_args, **_kwargs: env)
    monkeypatch.setattr(terminal_tool, "_pre_exec_block", lambda _command, **_kwargs: None)
    monkeypatch.setattr(terminal_tool, "_check_all_guards", check)
    monkeypatch.setattr(terminal_tool, "_docker_has_host_access", lambda _config: False)
    monkeypatch.setattr(terminal_tool, "_resolve_command_cwd", lambda **_kwargs: "/workspace")
    monkeypatch.setattr(terminal_tool, "_yield_kwargs", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(terminal_tool, "finalize_foreground_result", lambda **_kwargs: "executed")

    from agent import deadline
    from hermes_cli.observability import shared_metrics_harness, shared_metrics_loop
    from tools import interrupt

    monkeypatch.setattr(deadline, "run_bounded_sync", lambda callback, *_args, **_kwargs: _run_guard(callback))
    monkeypatch.setattr(shared_metrics_loop, "record_terminal_backend", lambda _plan, result, **_kwargs: result)
    monkeypatch.setattr(shared_metrics_harness, "record_terminal_outcome", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(interrupt, "clear_current_thread_interrupt", lambda: None)
    return env, risk_commands


def _run_guard(callback):
    callback()
    return SimpleNamespace(timed_out=False)


def test_approved_terminal_command_reaches_risk_and_execution_unchanged(monkeypatch):
    env, risk_commands = _install_runtime(
        monkeypatch,
        lambda _command: {"approved": True, "user_approved": True, "description": "approved"},
    )

    result = terminal_tool.terminal_tool(_RAW_COMMAND, _host_local=True)

    assert result == "executed"
    assert risk_commands == [_RAW_COMMAND]
    assert env.commands == [_RAW_COMMAND]


@pytest.mark.parametrize("status", ["pending_approval", "blocked"])
def test_denied_or_unresolved_approval_never_executes(monkeypatch, status):
    env, risk_commands = _install_runtime(
        monkeypatch,
        lambda command: {
            "approved": False, "status": status, "command": command,
            "description": "approval unresolved", "pattern_key": "test-rule",
        },
    )

    result = terminal_tool.terminal_tool(_RAW_COMMAND, _host_local=True)

    assert json.loads(result)["status"] == status
    assert risk_commands == [_RAW_COMMAND]
    assert env.commands == []
