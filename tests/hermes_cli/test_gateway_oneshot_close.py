import asyncio

from hermes_cli.gateway_chat_view import GatewayChatView


class Client:
    def __init__(self, detach_error=None, slow=False, resume_snapshot=None):
        self.calls = []
        self.detach_error = detach_error
        self.slow = slow
        self.resume_snapshot = resume_snapshot
        self.events = asyncio.Queue()

    async def rpc(self, method, **params):
        self.calls.append((method, params))
        if method == "session.detach":
            if self.slow:
                await asyncio.sleep(3)
            if self.detach_error:
                raise self.detach_error
        if method == "session.resume":
            return self.resume_snapshot
        return {}


def _view(*, created=True, outcome="completed", failure=None, client=None):
    client = client or Client()
    view = GatewayChatView(client, {
        "stored_session_id": "session-1", "subscription_id": "subscription-1",
    }, quiet=True)
    view.created_session = created
    view.unattended = True
    view.failure = failure

    async def render():
        return

    async def submit(text):
        view.completions["admission-1"] = {"outcome": outcome, "text": "answer"}
        return {"admission_id": "admission-1"}

    async def settled_result(admission):
        return {}

    view.render = render
    view.submit = submit
    view._settled_result = settled_result
    return view, client


def _run(view):
    return asyncio.run(view.run("question", oneshot=True))


def test_created_oneshot_detaches_after_terminal_completion():
    view, client = _view()

    assert _run(view) == 0
    assert client.calls == [("session.detach", {
        "session_id": "session-1", "subscription_id": "subscription-1", "end_reason": "cli_close",
    })]


def test_created_oneshot_detaches_after_receipt_recovery():
    view, client = _view()

    async def submit(text):
        view.completions["admission-1"] = {
            "outcome": "completed", "text": "recovered answer",
            "_receipt": {"result": {"completed": True}},
        }
        return {"admission_id": "admission-1"}

    view.submit = submit
    assert _run(view) == 0
    assert client.calls[0][0] == "session.detach"
    assert client.calls[0][1]["subscription_id"] == "subscription-1"


def test_replay_gap_resume_replaces_detach_subscription_id():
    client = Client(resume_snapshot={
        "stored_session_id": "session-1", "subscription_id": "subscription-2",
        "execution_generation": 2,
    })
    view, _ = _view(client=client)

    asyncio.run(view._reattach())
    asyncio.run(view._close_created_oneshot())

    assert client.calls[-1] == ("session.detach", {
        "session_id": "session-1", "subscription_id": "subscription-2", "end_reason": "cli_close",
    })


def test_resumed_oneshot_does_not_detach():
    view, client = _view(created=False)

    assert _run(view) == 0
    assert not client.calls


def test_cancelled_completion_and_failure_do_not_detach():
    cancelled, client = _view(outcome="cancelled")
    assert _run(cancelled) == 130
    assert not client.calls

    failed, client = _view(failure=RuntimeError("stream failed"))
    assert _run(failed) == 0
    assert not client.calls


def test_detach_oserror_does_not_hide_answer_or_change_exit_code(capsys):
    view, client = _view(client=Client(detach_error=OSError("connection closed")))

    assert _run(view) == 0
    assert "answer" in capsys.readouterr().out
    assert len(client.calls) == 1


def test_detach_timeout_and_invalid_params_do_not_change_result():
    slow, client = _view(client=Client(slow=True))
    assert _run(slow) == 0
    assert len(client.calls) == 1

    from hermes_cli.gateway_client import GatewayClientError
    old_gateway, client = _view(client=Client(detach_error=GatewayClientError("invalid_params")))
    assert _run(old_gateway) == 0
    assert len(client.calls) == 1


def test_input_required_and_ctrl_c_do_not_detach():
    blocked, client = _view()
    async def submit_blocked(text):
        return {"admission_id": "admission-1"}
    blocked.submit = submit_blocked
    blocked.prompts = {"prompt-1": {"prompt_id": "prompt-1", "kind": "approval"}}
    assert _run(blocked) == 3
    assert not client.calls

    interrupted, client = _view()
    async def submit_interrupted(text):
        raise KeyboardInterrupt
    interrupted.submit = submit_interrupted
    try:
        _run(interrupted)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("KeyboardInterrupt should propagate")
    assert not client.calls


def test_interactive_view_is_not_detached_by_oneshot_cleanup():
    view, client = _view()
    view.unattended = False

    asyncio.run(view._close_created_oneshot())

    assert not client.calls
