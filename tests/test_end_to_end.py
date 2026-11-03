"""The whole bridge over real HTTP: a scripted homeserver, a mocked hub.

``main()`` is run the way the image runs it, settings from the environment,
to check what a container platform sees: readiness, the reply in the room,
and ``token_rejected`` in the termination log when the token is refused.
"""
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import MagicMock

import pytest
from ovos_bus_client.message import Message

import hm_matrix_bridge as bridge_module
import hm_matrix_bridge.__main__ as cli
import hm_matrix_bridge.matrix as matrix_module
from homeserver import Homeserver
from hm_matrix_bridge import HiveMindMatrixBridge
from hm_matrix_bridge.matrix import MatrixClient

BOT = "@hive:example.org"
ALICE = "@alice:example.org"
ROOM = "!group:example.org"
TOKEN = "syt_aGl2ZQ_secretvalue"
QUESTION = "what is on my calendar"
ANSWER = "Dentist at four"


def scripted_sync(*answers, then_wait=0.05):
    """A /sync handler answering ``answers`` in order, then empty long-polls."""
    queue = list(answers)

    def handler(request):
        if queue:
            answer = queue.pop(0)
            return answer if isinstance(answer, tuple) else (200, answer, {})
        time.sleep(then_wait)
        return 200, {"next_batch": "idle"}, {}

    return handler


def room(*timeline, members=None, next_batch="b"):
    entry = {"timeline": {"events": list(timeline)}, "state": {"events": []}}
    if members:
        entry["summary"] = {"m.joined_member_count": members}
    return {"next_batch": next_batch, "rooms": {"join": {ROOM: entry}}}


def message(sender, body, event_id):
    return {"type": "m.room.message", "sender": sender, "event_id": event_id,
            "content": {"msgtype": "m.text", "body": body}}


def a_hub(on_utterance=None):
    hub = MagicMock()
    hub.handshake_event = threading.Event()
    hub.connect.side_effect = lambda **kwargs: hub.handshake_event.set()
    if on_utterance is not None:
        hub.emit.side_effect = lambda hive_message: on_utterance(hive_message.payload)
    return hub


def wait_for(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def status(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as answer:
            return answer.status
    except urllib.error.HTTPError as error:
        return error.code


@pytest.fixture
def hs():
    with Homeserver() as server:
        server.route("GET", "/account/whoami", (200, {"user_id": BOT, "device_id": "D"}))
        server.route("GET", r"/profile/.+/displayname", (200, {"displayname": "HiveBot"}))
        server.route("PUT", r"/rooms/.+/send/m\.room\.message/.+", (200, {"event_id": "$reply"}))
        yield server


def test_a_question_goes_to_the_hub_and_the_answer_comes_back_to_the_room(hs):
    hs.route("GET", "/sync", scripted_sync(
        room(message(ALICE, "HiveBot: an old question", "$old"), members=3, next_batch="b1"),
        room(message(ALICE, f"HiveBot: {QUESTION}", "$new"), next_batch="b2"),
    ))
    asked = []
    bridge = None

    def hub_answers(utterance):
        asked.append((utterance.data["utterances"], utterance.context["session"]["session_id"]))
        reply = Message("speak", {"utterance": ANSWER}, utterance.context)
        threading.Thread(target=bridge.handle_speak, args=(reply,), daemon=True).start()

    bridge = HiveMindMatrixBridge(
        matrix=MatrixClient(hs.url, TOKEN, min_send_interval=0),
        client=a_hub(hub_answers), allowed_rooms=[ROOM], health_port=0)
    runner = threading.Thread(target=bridge.start, daemon=True)
    runner.start()
    try:
        assert wait_for(lambda: hs.requests_to("PUT", r"/rooms/.+"))
        assert asked == [([QUESTION], f"matrix-{ROOM}-{ALICE}")], "history is not answered"
        [sent] = hs.requests_to("PUT", r"/rooms/.+")
        assert sent.body["body"] == ANSWER
        assert sent.raw_path.startswith("/_matrix/client/v3/rooms/%21group%3Aexample.org/send/")
        assert status(bridge._health.port, "/readyz") == 200
        assert status(bridge._health.port, "/healthz") == 200
    finally:
        bridge.stop()
        runner.join(timeout=10)
    assert not runner.is_alive()


def run_main(monkeypatch, tmp_path, hs, hub=None):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    termination = tmp_path / "termination-log"
    monkeypatch.setattr(cli, "TERMINATION_LOG", str(termination))
    hub = hub or a_hub()
    monkeypatch.setattr(bridge_module, "HiveMessageBusClient", lambda **kwargs: hub)
    for variable in list(cli.ENVIRONMENT.values()):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("MATRIX_HOMESERVER", hs.url)
    monkeypatch.setenv("MATRIX_USER_ID", BOT)
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("MATRIX_ALLOWED_ROOMS", ROOM)
    with pytest.raises(SystemExit) as stopped:
        cli.main([])
    written = termination.read_text(encoding="utf-8") if termination.exists() else None
    return stopped.value.code, written, hub


def test_a_refused_token_at_start_is_token_rejected_and_the_hub_is_never_dialled(monkeypatch, tmp_path, hs):
    hs.route("GET", "/account/whoami", (401, {"errcode": "M_UNKNOWN_TOKEN"}))
    code, written, hub = run_main(monkeypatch, tmp_path, hs)
    assert code == cli.EXIT_TOKEN_REJECTED != 0
    assert written == "token_rejected"
    hub.connect.assert_not_called()


def test_a_token_revoked_while_running_is_token_rejected(monkeypatch, tmp_path, hs):
    hs.route("GET", "/sync", scripted_sync(room(members=3), (401, {"errcode": "M_UNKNOWN_TOKEN"}, {})))
    code, written, hub = run_main(monkeypatch, tmp_path, hs)
    assert (code, written) == (cli.EXIT_TOKEN_REJECTED, "token_rejected")
    hub.connect.assert_called_once()
    hub.close.assert_called()


def test_a_token_of_another_account_is_token_rejected(monkeypatch, tmp_path, hs):
    hs.route("GET", "/account/whoami", (200, {"user_id": "@someone-else:example.org"}))
    code, written, _ = run_main(monkeypatch, tmp_path, hs)
    assert (code, written) == (cli.EXIT_TOKEN_REJECTED, "token_rejected")


def test_a_refusing_hub_exits_non_zero_without_blaming_the_token(monkeypatch, tmp_path, hs):
    hub = a_hub()
    hub.connect.side_effect = ConnectionRefusedError("HiveMind refused this identity: bad key")
    code, written, _ = run_main(monkeypatch, tmp_path, hs, hub=hub)
    assert code == 1
    assert written is None


def test_the_token_is_never_logged(monkeypatch, tmp_path, hs):
    lines = []
    log = MagicMock()
    for level in ("debug", "info", "warning", "error", "exception"):
        getattr(log, level).side_effect = lambda *a, **k: lines.append(" ".join(map(str, a)))
    for module in (cli, bridge_module, matrix_module):
        monkeypatch.setattr(module, "LOG", log)
    hs.route("GET", "/sync", scripted_sync(room(members=3), (401, {"errcode": "M_UNKNOWN_TOKEN"}, {})))
    run_main(monkeypatch, tmp_path, hs)
    assert lines
    assert TOKEN not in "\n".join(lines)
