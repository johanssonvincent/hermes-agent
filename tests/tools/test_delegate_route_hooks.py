import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tools.delegate_tool_config import _resolve_child_runtime, _resolve_plugin_delegate_route
from tools.delegate_tool import _build_children


class ResolvePluginDelegateRouteTests(unittest.TestCase):
    def test_forced_worktree_setup_failure_refuses_shared_workspace(self):
        from tools.delegate_tool_child_run import _ChildRun
        run = _ChildRun(child=SimpleNamespace(_jev_worktree_isolation=True), parent_agent=SimpleNamespace(),
                        task_index=0, goal="implement", subagent_id="test-child", child_progress_cb=None)
        with patch("tools.delegate_tool_child_run._create_isolated_worktree", return_value=None):
            with self.assertRaisesRegex(ValueError, "refusing shared-workspace"):
                run.seed_workspace()

    def test_missing_session_home_does_not_use_another_profiles_manager(self):
        with patch.dict(os.environ, {"HERMES_HOME": ""}), \
             patch("hermes_cli.plugins.get_loaded_plugin_manager", return_value=None) as get_manager:
            self.assertIsNone(_resolve_plugin_delegate_route(
                {"goal": "task"}, default_provider="openai-codex", default_model="gpt-6-luna-900k",
                default_effort="medium", parent_agent=SimpleNamespace()))
        get_manager.assert_not_called()

    def test_no_plugin_route_keeps_global_pin(self):
        with patch("hermes_cli.plugins.get_loaded_plugin_manager", return_value=None):
            self.assertIsNone(_resolve_plugin_delegate_route(
                {"goal": "task"}, default_provider="openai-codex", default_model="gpt-6-luna-900k",
                default_effort="medium"))

    def test_valid_provider_model_effort_review_route_is_preserved(self):
        decision = {"provider": "anthropic", "model": "claude-opus-5-5", "reasoning_effort": "high",
                    "reason": "T3 plan", "runner": "routed_worker",
                    "review": {"provider": "openai-codex", "model": "gpt-6-astra", "reasoning_effort": "high", "runner": "routed_worker"},
                    "allowed_tools": ["read_file", "web_search"],
                    "classification": {"work_type": "planning", "complexity": 4, "risk": "high", "ambiguity": "medium"}}
        manager = SimpleNamespace(has_hook=lambda name: True, invoke_hook=lambda *a, **kw: [decision])
        with patch("hermes_cli.plugins.get_loaded_plugin_manager", return_value=manager):
            route = _resolve_plugin_delegate_route({"goal": "plan"}, default_provider="openai-codex",
                                                   default_model="gpt-6-luna-900k", default_effort="medium",
                                                   parent_agent=SimpleNamespace(_session_db=SimpleNamespace(db_path="/tmp/hermes-test/state.db")))
        self.assertEqual(route["model"], "claude-opus-5-5")
        self.assertEqual(route["reasoning_effort"], "high")
        self.assertEqual(route["review"]["model"], "gpt-6-astra")
        self.assertEqual(route["classification"]["complexity"], 4)

    def test_hook_error_or_invalid_route_refuses_global_fallback(self):
        for value in ({"error": "Jev quota unavailable"}, {"provider": "", "model": "x", "reasoning_effort": "medium"},
                      {"provider": "anthropic", "model": "x", "reasoning_effort": "max"}):
            manager = SimpleNamespace(has_hook=lambda name: True, invoke_hook=lambda *a, **kw: [value])
            with self.subTest(value=value), patch("hermes_cli.plugins.get_loaded_plugin_manager", return_value=manager):
                with self.assertRaises(ValueError):
                    _resolve_plugin_delegate_route({"goal": "task"}, default_provider="openai-codex",
                                                   default_model="gpt-6-luna-900k", default_effort="medium",
                                                   parent_agent=SimpleNamespace(_session_db=SimpleNamespace(db_path="/tmp/hermes-test/state.db")))

    def test_child_builder_applies_trusted_route_and_effort_per_task(self):
        route = {"provider": "anthropic", "model": "claude-opus-5-5", "reasoning_effort": "high",
                 "reason": "plan", "runner": "routed_worker", "review": None,
                 "allowed_tools": ["read_file", "web_search"],
                 "classification": {"work_type": "planning", "complexity": 3, "risk": "medium", "ambiguity": "high"}}
        creds = {"provider": "openai-codex", "model": "gpt-6-luna-900k", "base_url": None, "api_key": None,
                 "api_mode": None, "request_overrides": None, "command": None, "args": []}
        resolved = {"provider": "anthropic", "model": "claude-opus-5-5", "base_url": None, "api_key": "oauth",
                    "api_mode": "anthropic_messages", "request_overrides": None, "command": None, "args": []}
        built = SimpleNamespace(tool_progress_callback=None,
                                valid_tool_names=["read_file", "web_search", "write_file", "terminal"])
        with patch("tools.delegate_tool._resolve_plugin_delegate_route", return_value=route), \
             patch("tools.delegate_tool._resolve_delegation_credentials", return_value=resolved) as resolve_creds, \
             patch("tools.delegate_tool._build_child_preserving_parent_tools", return_value=built) as build:
            children, error = _build_children(
                [{"goal": "plan migration", "context": "context"}], [], creds, top_role="leaf", max_iterations=10,
                parent_agent=SimpleNamespace(), routing_cfg={"provider": "openai-codex", "model": "gpt-6-luna-900k",
                                                               "reasoning_effort": "medium"},
                live_deleg_id=None, live_writers=[])
        self.assertIsNone(error)
        self.assertEqual(len(children), 1)
        self.assertEqual(resolve_creds.call_args.args[0]["provider"], "anthropic")
        self.assertEqual(resolve_creds.call_args.args[0]["reasoning_effort"], "high")
        self.assertEqual(build.call_args.kwargs["model"], "claude-opus-5-5")
        self.assertEqual(build.call_args.kwargs["override_provider"], "anthropic")
        self.assertEqual(build.call_args.kwargs["routing_cfg"]["reasoning_effort"], "high")
        self.assertEqual(built._jev_route["model"], "claude-opus-5-5")
        self.assertEqual(built.valid_tool_names, ["read_file", "web_search"])

    def test_policy_routed_implementation_forces_private_worktree(self):
        route = {"provider": "openai-codex", "model": "gpt-6-luna-900k", "reasoning_effort": "high",
                 "reason": "complex implementation", "runner": "routed_worker", "review": None,
                 "classification": {"work_type": "implementation", "complexity": 3, "risk": "medium", "ambiguity": "medium"},
                 "allowed_tools": ["read_file", "write_file", "patch", "terminal"]}
        creds = {"provider": "openai-codex", "model": "gpt-6-luna-900k", "base_url": None, "api_key": None,
                 "api_mode": None, "request_overrides": None, "command": None, "args": []}
        child = SimpleNamespace(tool_progress_callback=None, valid_tool_names=["read_file", "write_file", "patch", "terminal"])
        resolved = {**creds, "provider": "openai-codex"}
        with patch("tools.delegate_tool._resolve_plugin_delegate_route", return_value=route), \
             patch("tools.delegate_tool._resolve_delegation_credentials", return_value=resolved), \
             patch("tools.delegate_tool._build_child_preserving_parent_tools", return_value=child):
            children, error = _build_children(
                [{"goal": "implement", "context": ""}], [], creds, top_role="leaf", max_iterations=10,
                parent_agent=SimpleNamespace(), routing_cfg={"provider": "openai-codex", "model": "gpt-6-luna-900k",
                                                               "reasoning_effort": "medium"},
                live_deleg_id=None, live_writers=[])
        self.assertIsNone(error)
        self.assertEqual(len(children), 1)
        self.assertTrue(child._jev_worktree_isolation)

    def test_jev_worktree_override_works_when_global_isolation_is_off(self):
        from tools.delegate_tool_child_run import _create_isolated_worktree
        info = {"path": "/tmp/jev-worktree", "branch": "jev/child", "repo_root": "/repo", "base_commit": "abc"}
        with patch("tools.delegate_tool._get_worktree_isolation", return_value=False), \
             patch("tools.subagent_worktree.local_backend_active", return_value=True), \
             patch("tools.terminal_tool.get_session_cwd", return_value="/repo"), \
             patch("tools.subagent_worktree.create_subagent_worktree", return_value=info) as create:
            result = _create_isolated_worktree(SimpleNamespace(), "parent-task", "child-id", force=True)
        self.assertEqual(result, info)
        create.assert_called_once_with("/repo", subagent_id="child-id")

    def test_per_child_effort_overrides_global_delegate_effort(self):
        parent = SimpleNamespace(model="gpt-6-luna-900k", provider="openai-codex", base_url=None,
                                 api_mode="responses", acp_args=[], reasoning_config={"enabled": True, "effort": "medium"},
                                 _fallback_chain=None)
        result = _resolve_child_runtime(
            parent, {"reasoning_effort": "medium"}, None, model="gpt-6-sol", override_provider="openai-codex",
            override_base_url=None, override_api_key=None, override_api_mode=None, override_acp_command=None,
            override_acp_args=[], routing_cfg={"provider": "openai-codex", "model": "gpt-6-sol", "reasoning_effort": "high"})
        self.assertEqual(result["reasoning_config"], {"enabled": True, "effort": "high"})


if __name__ == "__main__":
    unittest.main()
