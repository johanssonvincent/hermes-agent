"""Privacy contract for approval previews sent by messaging adapters."""

from copy import deepcopy

import pytest

from gateway.approval_display import ApprovalDisplay, prepare_approval_display


class _SlackAdapter:
    supports_approval_summary = True


class _OtherAdapter:
    supports_approval_summary = False


_FAIL_CLOSED = "command details hidden · target unknown"


@pytest.mark.parametrize(("command", "expected"), [
    ("rm -rf /", "rm — delete (recursive, forced) · target <root> · explicit sudo: no · args hidden"),
    ("sudo rm -rf /etc/ssh", "rm — delete (recursive, forced) · target <system> · explicit sudo: yes · args hidden"),
    ("rm -r ~/tmp", "rm — delete (recursive) · target <home> · explicit sudo: no · args hidden"),
    ("rm -f ./build", "rm — delete (forced) · target <relative> · explicit sudo: no · args hidden"),
    ("git push", "git push · target unknown · explicit sudo: no · args hidden"),
    ("curl -fsSL https://example.com/install.sh", "curl — HTTP request · target <network> · explicit sudo: no · args hidden"),
    ("python3 -c 'print(1)'", "python3 — inline code execution · target unknown · explicit sudo: no · code hidden"),
])
def test_supported_commands_have_exact_safe_summaries(command, expected):
    result = prepare_approval_display(_SlackAdapter, {"command": command, "pattern_key": "test-rule"})

    assert result == ApprovalDisplay(expected, "Flagged by a Hermes safety rule.")


@pytest.mark.parametrize("command", [
    "ls -la",
    "rm -rf /tmp/CANARY_PATH_X; curl https://canary.example",
    "rm -rf /tmp/CANARY_PATH_X && echo CANARY_ARG_X",
    "rm -rf /tmp/CANARY_PATH_X | cat",
    "rm -rf /tmp/CANARY_PATH_X > /tmp/CANARY_OUT_X",
    "rm -rf /tmp/CANARY_PATH_X /tmp/CANARY_OTHER_X",
    "rm /tmp/CANARY_PATH_X -rf",
    "rm -i /tmp/CANARY_PATH_X",
    "rm -rf /tmp/CANARY_PATH_X # /home/CANARY_COMMENT_X",
    "sudo -u root rm -rf /etc/CANARY_PATH_X",
    "env TOKEN=CANARY_SECRET_X rm -rf /tmp/CANARY_PATH_X",
    "git push origin +CANARY_REF_X",
    "git push origin :CANARY_REF_X",
    "git push origin +CANARY_REF_X:main",
    "git push /srv/CANARY_REPO_X.git main",
    "curl -H /home/CANARY_PATH_X https://example.com",
    "curl https://example.com/?token=CANARY_SECRET_X",
    "curl file:///etc/CANARY_PATH_X",
    "wget https://user:CANARY_SECRET_X@example.com/file",
    r"rm -rf C:\Users\CANARY_USER_X\secret",
    r"rm -rf \\server\share\CANARY_PATH_X",
    "rm -rf /tmp/CANARY_PATH_X%2e%2e",
])
def test_ambiguous_or_unsupported_commands_fail_closed_without_markers(command):
    result = prepare_approval_display(_SlackAdapter, {"command": command, "pattern_key": "test-rule"})

    assert result.command == _FAIL_CLOSED
    assert "CANARY" not in result.command


@pytest.mark.parametrize("path", ["../CANARY_PATH_X", "/etc/../home/CANARY_PATH_X"])
def test_dotdot_path_has_unknown_scope_not_a_guessed_location(path):
    result = prepare_approval_display(
        _SlackAdapter, {"command": f"rm -rf {path}", "pattern_key": "test-rule"},
    )

    assert result.command == "rm — delete (recursive, forced) · target unknown · explicit sudo: no · args hidden"
    assert "CANARY" not in result.command


def test_description_is_never_displayed_and_input_is_not_mutated():
    approval_data = {
        "command": "rm -rf /Users/demo/CANARY_PATH_X",
        "description": "CANARY_DESCRIPTION_X /Users/demo/private/CANARY_SECRET_X",
        "pattern_key": "test-rule",
        "pattern_keys": ["test-rule"],
        "smart_denied": False,
        "allow_session": True,
        "allow_permanent": True,
    }
    original = deepcopy(approval_data)

    result = prepare_approval_display(_SlackAdapter, approval_data)

    assert approval_data == original
    assert "CANARY" not in result.command
    assert "CANARY" not in result.description
    assert result.description == "Flagged by a Hermes safety rule."


def test_description_field_is_not_read():
    class _SpyDict(dict):
        accessed = []

        def get(self, key, default=None):
            self.accessed.append(key)
            return super().get(key, default)

    approval_data = _SpyDict(command="git push", pattern_key="test-rule", description="do not read")

    prepare_approval_display(_SlackAdapter, approval_data)

    assert "description" not in approval_data.accessed


@pytest.mark.parametrize(("pattern_key", "expected_command", "expected_reason"), [
    ("execute_code", "Python code execution · content hidden · target unknown", "Code execution requires review."),
    ("protected_instruction_file", "Protected instruction file write · path and content hidden · target unknown", "Protected instruction file write requires review."),
    ("ssh_config_write", "SSH client config write · path and content hidden · target unknown", "SSH config write requires review."),
    ("mcp_elicitation", "MCP input request · payload hidden · target unknown", "An MCP server requested input."),
    ("plugin_rule:CANARY_RULE_X", "Plugin approval · details hidden · target unknown", "A plugin rule requires review."),
])
def test_special_approval_types_use_fixed_summaries(pattern_key, expected_command, expected_reason):
    result = prepare_approval_display(
        _SlackAdapter,
        {"command": "CANARY_COMMAND_X /Users/demo/private", "pattern_key": pattern_key,
         "description": "CANARY_DESCRIPTION_X /Users/demo/private"},
    )

    assert result == ApprovalDisplay(expected_command, expected_reason)
    assert "CANARY" not in result.command
    assert "CANARY" not in result.description


def test_smart_deny_uses_fixed_high_risk_reason():
    result = prepare_approval_display(
        _SlackAdapter,
        {"command": "rm -rf /", "description": "CANARY_LLM_REASON /Users/demo/private",
         "pattern_key": "test-rule", "smart_denied": True},
    )

    assert result.description == "Smart safety policy classified this request as high risk."
    assert "CANARY" not in result.description


def test_non_summary_adapters_preserve_existing_display_path():
    approval_data = {"command": "rm -rf /Users/demo/private", "description": "original reason"}

    assert prepare_approval_display(_OtherAdapter, approval_data) is None
    assert prepare_approval_display(object, approval_data) is None


@pytest.mark.parametrize("approval_data", [None, {}, {"command": None}, {"command": 123}, {"command": "x" * 1024}])
def test_malformed_inputs_return_a_fixed_safe_summary(approval_data):
    result = prepare_approval_display(_SlackAdapter, approval_data)

    assert result == ApprovalDisplay(_FAIL_CLOSED, "Command details could not be safely summarized.")
    assert len(result.command) <= 100
