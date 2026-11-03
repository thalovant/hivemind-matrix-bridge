"""How replies leave the bridge: off the hub's thread, in order, bounded, and in sizes a homeserver takes."""
import threading
import time

import pytest
from ovos_bus_client.message import Message

import hm_matrix_bridge as bridge_module
from fakes import ROOM, FakeMatrix, make_bridge
from hm_matrix_bridge import MAX_BODY_BYTES, MAX_REPLY_PARTS, TRUNCATION_MARK, split_reply


class HeldMatrix(FakeMatrix):
    """A homeserver that holds every send until released."""

    def __init__(self):
        super().__init__()
        self.release = threading.Event()
        self.entered = threading.Event()

    def send_message(self, room_id, content):
        self.entered.set()
        assert self.release.wait(10)
        return super().send_message(room_id, content)


def hub_says(bridge, utterance, room_id=ROOM):
    bridge.handle_speak(Message("speak", {"utterance": utterance}, {"room_id": room_id}))


def test_the_hub_thread_does_not_wait_for_the_homeserver():
    matrix = HeldMatrix()
    bridge, _, _ = make_bridge({ROOM: 3}, matrix=matrix)
    started = time.monotonic()
    hub_says(bridge, "one")
    hub_says(bridge, "two")
    assert time.monotonic() - started < 1.0
    assert matrix.entered.wait(5)
    assert matrix.sent == []
    matrix.release.set()
    assert bridge.wait_for_replies(5)
    assert [content["body"] for _, content in matrix.sent] == ["one", "two"]


def test_replies_beyond_the_queue_are_dropped_not_piled_up(monkeypatch):
    monkeypatch.setattr(bridge_module, "MAX_PENDING_REPLIES", 3)
    matrix = HeldMatrix()
    bridge, _, _ = make_bridge({ROOM: 3}, matrix=matrix)
    hub_says(bridge, "in flight")
    assert matrix.entered.wait(5)
    for number in range(10):
        hub_says(bridge, f"queued {number}")
    matrix.release.set()
    assert bridge.wait_for_replies(5)
    assert [content["body"] for _, content in matrix.sent] == \
        ["in flight", "queued 0", "queued 1", "queued 2"]


def test_a_reply_that_raises_does_not_end_the_sender(monkeypatch):
    bridge, matrix, _ = make_bridge({ROOM: 3})
    calls = []
    original = bridge.answer_policy

    def flaky(room_id):
        calls.append(room_id)
        if len(calls) == 1:
            raise KeyError("odd")
        return original(room_id)

    monkeypatch.setattr(bridge, "answer_policy", flaky)
    hub_says(bridge, "first")
    hub_says(bridge, "second")
    assert bridge.wait_for_replies(5)
    assert [content["body"] for _, content in matrix.sent] == ["second"]


def test_stop_posts_what_is_already_queued():
    matrix = HeldMatrix()
    bridge, _, _ = make_bridge({ROOM: 3}, matrix=matrix)
    hub_says(bridge, "one")
    hub_says(bridge, "two")
    assert matrix.entered.wait(5)
    threading.Timer(0.3, matrix.release.set).start()
    bridge.stop()
    assert [content["body"] for _, content in matrix.sent] == ["one", "two"]
    hub_says(bridge, "after stop")
    assert bridge.wait_for_replies(5)
    assert len(matrix.sent) == 2


def test_a_long_reply_is_split_into_messages_a_homeserver_accepts():
    bridge, matrix, _ = make_bridge({ROOM: 3})
    words = " ".join(f"word{number}" for number in range(5000))  # ~ 44 kB
    hub_says(bridge, words)
    assert bridge.wait_for_replies(5)
    bodies = [content["body"] for _, content in matrix.sent]
    assert 1 < len(bodies) <= MAX_REPLY_PARTS
    assert all(len(body.encode("utf-8")) <= MAX_BODY_BYTES for body in bodies)
    assert " ".join(bodies) == words
    assert all(content["msgtype"] == "m.notice" and content["m.mentions"] == {}
               for _, content in matrix.sent)


@pytest.mark.parametrize("text", [
    "😀" * 20000,                       # 4 bytes each, no spaces
    "日本語のテキスト" * 3000,           # 3 bytes each
    ("x" * 15999) + "é" + ("y" * 100),  # a 2-byte character across the boundary
])
def test_splitting_never_breaks_a_character(text):
    parts = split_reply(text)
    assert all(len(part.encode("utf-8")) <= MAX_BODY_BYTES for part in parts)
    if len(parts) < MAX_REPLY_PARTS or not parts[-1].endswith(TRUNCATION_MARK):
        assert "".join(parts) == text


def test_a_reply_beyond_the_parts_is_cut_and_marked():
    text = "a " * (MAX_BODY_BYTES * MAX_REPLY_PARTS)
    parts = split_reply(text)
    assert len(parts) == MAX_REPLY_PARTS
    assert parts[-1].endswith(TRUNCATION_MARK)
    assert all(len(part.encode("utf-8")) <= MAX_BODY_BYTES for part in parts)


def test_a_short_reply_is_one_message_unchanged():
    assert split_reply("It is noon.\n\nAnything else?") == ["It is noon.\n\nAnything else?"]
