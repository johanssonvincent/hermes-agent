"""Safe summaries for approval prompts sent to messaging adapters."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ApprovalDisplay:
    command: str
    description: str


_FAIL_CLOSED = "command details hidden · target unknown"
_DEFAULT_REASON = "Command details could not be safely summarized."
_GENERIC_REASON = "Flagged by a Hermes safety rule."
_HIGH_RISK_REASON = "Smart safety policy classified this request as high risk."
_MAX_COMMAND_LENGTH = 512
_ALLOWED_RM_OPTIONS = {
    "-r": (True, False), "-R": (True, False), "-f": (False, True),
    "-rf": (True, True), "-fr": (True, True), "-Rf": (True, True), "-fR": (True, True),
    "--recursive": (True, False), "--force": (False, True),
}
_ALLOWED_CURL_OPTIONS = {"-f", "-s", "-S", "-L", "-sS", "-fsS", "-fsSL", "--fail", "--silent",
                        "--show-error", "--location"}
_ALLOWED_WGET_OPTIONS = {"-q", "--quiet", "-nv", "--no-verbose"}
_SIMPLE_CHARS = re.compile(r"^[A-Za-z0-9 ._/:~+\-]+$")
_PATH_CHARS = re.compile(r"^(?:/|~(?:/[A-Za-z0-9._+\-]+)*|(?:\./)?[A-Za-z0-9._+\-/]+)$")
_URL = re.compile(r"^https?://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._/\-]*)?$")
_INLINE_PYTHON = re.compile(r"^\s*(sudo )?(python3?) -c '([^'\r\n]*)'\s*$")
_SPECIAL = {
    "execute_code": (
        "Python code execution · content hidden · target unknown", "Code execution requires review."),
    "protected_instruction_file": (
        "Protected instruction file write · path and content hidden · target unknown",
        "Protected instruction file write requires review."),
    "ssh_config_write": (
        "SSH client config write · path and content hidden · target unknown", "SSH config write requires review."),
    "mcp_elicitation": (
        "MCP input request · payload hidden · target unknown", "An MCP server requested input."),
}


def prepare_approval_display(adapter_class: type, approval_data: object) -> ApprovalDisplay | None:
    """Prepare an adapter-specific display copy without changing approval data."""
    if getattr(adapter_class, "supports_approval_summary", False) is not True:
        return None
    try:
        if not isinstance(approval_data, dict):
            return ApprovalDisplay(_FAIL_CLOSED, _DEFAULT_REASON)
        pattern_key = approval_data.get("pattern_key")
        smart_denied = bool(approval_data.get("smart_denied", False))
        if pattern_key in _SPECIAL:
            command, reason = _SPECIAL[pattern_key]
            return ApprovalDisplay(command, _HIGH_RISK_REASON if smart_denied else reason)
        if isinstance(pattern_key, str) and pattern_key.startswith("plugin_rule:"):
            return ApprovalDisplay(
                "Plugin approval · details hidden · target unknown",
                _HIGH_RISK_REASON if smart_denied else "A plugin rule requires review.",
            )
        command = approval_data.get("command")
        if not isinstance(command, str) or not command or len(command) > _MAX_COMMAND_LENGTH:
            return ApprovalDisplay(_FAIL_CLOSED, _DEFAULT_REASON)
        summary = _summarize_command(command)
        return ApprovalDisplay(summary, _HIGH_RISK_REASON if smart_denied else _GENERIC_REASON)
    except Exception:
        return ApprovalDisplay(_FAIL_CLOSED, _DEFAULT_REASON)


def _summarize_command(command: str) -> str:
    if "\n" in command or "\r" in command or "\t" in command or any(ord(char) > 127 for char in command):
        return _FAIL_CLOSED
    command = command.strip()
    if not command:
        return _FAIL_CLOSED
    inline = _INLINE_PYTHON.fullmatch(command)
    if inline:
        return _format("python3" if inline.group(2) == "python3" else "python", "inline code execution",
                       "unknown", sudo=bool(inline.group(1)), args="code hidden")
    if not _SIMPLE_CHARS.fullmatch(command):
        return _FAIL_CLOSED
    tokens = command.split()
    if not tokens or len(tokens) > 32:
        return _FAIL_CLOSED
    sudo = tokens[0] == "sudo"
    if sudo:
        tokens = tokens[1:]
    if not tokens:
        return _FAIL_CLOSED
    if tokens[0] == "rm":
        return _summarize_rm(tokens, sudo)
    if tokens == ["git", "push"]:
        return f"git push · target unknown · explicit sudo: {'yes' if sudo else 'no'} · args hidden"
    if tokens[0] in {"curl", "wget"}:
        return _summarize_http(tokens, sudo)
    return _FAIL_CLOSED


def _summarize_rm(tokens: list[str], sudo: bool) -> str:
    recursive = False
    forced = False
    index = 1
    while index < len(tokens) - 1 and tokens[index] in _ALLOWED_RM_OPTIONS:
        option_recursive, option_forced = _ALLOWED_RM_OPTIONS[tokens[index]]
        recursive = recursive or option_recursive
        forced = forced or option_forced
        index += 1
    if index != len(tokens) - 1:
        return _FAIL_CLOSED
    path = tokens[index]
    if path.startswith("-") or not _PATH_CHARS.fullmatch(path):
        return _FAIL_CLOSED
    scope = _path_scope(path)
    if scope == "unknown":
        return _format("rm", _delete_operation(recursive, forced), "unknown", sudo=sudo)
    return _format("rm", _delete_operation(recursive, forced), scope, sudo=sudo)


def _summarize_http(tokens: list[str], sudo: bool) -> str:
    program = tokens[0]
    options = _ALLOWED_CURL_OPTIONS if program == "curl" else _ALLOWED_WGET_OPTIONS
    operands = [token for token in tokens[1:] if token not in options]
    if any(token.startswith("-") for token in operands) or len(operands) != 1 or not _URL.fullmatch(operands[0]):
        return _FAIL_CLOSED
    parsed = urlsplit(operands[0])
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return _FAIL_CLOSED
    if parsed.query or parsed.fragment:
        return _FAIL_CLOSED
    try:
        parsed.port
    except ValueError:
        return _FAIL_CLOSED
    operation = "HTTP request" if program == "curl" else "HTTP download"
    return _format(program, operation, "<network>", sudo=sudo)


def _path_scope(path: str) -> str:
    if ".." in path.split("/") or "//" in path:
        return "unknown"
    if path == "/":
        return "<root>"
    if path == "~" or path.startswith("~/"):
        return "<home>"
    if not path.startswith("/"):
        return "<relative>"
    home = str(Path.home()).rstrip("/")
    if path == home or path.startswith(home + "/") or path == "/Users" or path.startswith("/Users/") \
            or path == "/home" or path.startswith("/home/"):
        return "<home>"
    if path == "/tmp" or path.startswith("/tmp/") or path == "/private/tmp" or path.startswith("/private/tmp/"):
        return "<temporary>"
    if any(path == prefix or path.startswith(prefix + "/") for prefix in (
            "/bin", "/boot", "/dev", "/etc", "/lib", "/opt", "/private", "/proc", "/sbin",
            "/System", "/usr", "/var", "/Library")):
        return "<system>"
    return "unknown"


def _delete_operation(recursive: bool, forced: bool) -> str:
    qualifiers = [label for enabled, label in ((recursive, "recursive"), (forced, "forced")) if enabled]
    suffix = f" ({', '.join(qualifiers)})" if qualifiers else ""
    return f"delete{suffix}"


def _format(program: str, operation: str, scope: str, *, sudo: bool, args: str = "args hidden") -> str:
    sudo_text = "yes" if sudo else "no"
    summary = f"{program} — {operation} · target {scope} · explicit sudo: {sudo_text} · {args}"
    return summary if len(summary) <= 100 else _FAIL_CLOSED
