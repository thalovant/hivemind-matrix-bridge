"""Test doubles: a scripted Matrix client and builders for ``/sync`` answers.

The bridge only calls the handful of methods of
:class:`hm_matrix_bridge.matrix.MatrixClient` that :class:`FakeMatrix`
implements, so the bridge's room logic can be driven event by event without
a homeserver. ``tests/homeserver.py`` is the real-HTTP counterpart.
"""
import itertools
import threading
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock

from hm_matrix_bridge import HiveMindMatrixBridge
from hm_matrix_bridge.matrix import MatrixError

BOT = "@hive:example.org"
BOT_NAME = "HiveBot"
ALICE = "@alice:example.org"
BOB = "@bob:example.org"
ROOM = "!group:example.org"
DM = "!dm:example.org"
OTHER = "!other:example.org"

_ids = itertools.count(1)


class FakeMatrix:
    """Scripted stand-in for :class:`~hm_matrix_bridge.matrix.MatrixClient`."""

    def __init__(self, user_id: str = BOT, display_name: Optional[str] = BOT_NAME):
        self.user_id = user_id
        self._display_name = display_name
        self.whoami_results: List[Any] = []
        self.syncs: List[Any] = []
        self.sync_calls: List[Dict[str, Any]] = []
        self.on_exhausted: Optional[Callable[[], None]] = None
        self.joins: List[str] = []
        self.join_error: Optional[Exception] = None
        self.sent: List[tuple] = []
        self.send_error: Optional[Exception] = None
        self.members: Dict[str, List[str]] = {}
        self.member_calls: List[str] = []
        self.events: Dict[tuple, Dict[str, Any]] = {}
        self.event_calls: List[tuple] = []

    def whoami(self) -> Dict[str, Any]:
        if self.whoami_results:
            result = self.whoami_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return {"user_id": self.user_id, "device_id": "DEVICE"}

    def display_name(self, user_id: str) -> Optional[str]:
        return self._display_name

    def sync(self, since=None, timeout_ms=0, filter_json=None) -> Dict[str, Any]:
        self.sync_calls.append({"since": since, "timeout_ms": timeout_ms,
                                "filter": filter_json})
        if not self.syncs:
            if self.on_exhausted is not None:
                self.on_exhausted()
            return {"next_batch": f"s{len(self.sync_calls)}"}
        result = self.syncs.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def join(self, room_id: str) -> Dict[str, Any]:
        self.joins.append(room_id)
        if self.join_error is not None:
            raise self.join_error
        return {"room_id": room_id}

    def joined_members(self, room_id: str) -> Dict[str, Any]:
        self.member_calls.append(room_id)
        if room_id not in self.members:
            raise MatrixError(403, "M_FORBIDDEN")
        return {"joined": {user: {} for user in self.members[room_id]}}

    def event(self, room_id: str, event_id: str) -> Dict[str, Any]:
        self.event_calls.append((room_id, event_id))
        if (room_id, event_id) not in self.events:
            raise MatrixError(404, "M_NOT_FOUND")
        return self.events[(room_id, event_id)]

    def send_message(self, room_id: str, content: Dict[str, Any]) -> Optional[str]:
        self.sent.append((room_id, content))
        if self.send_error is not None:
            raise self.send_error
        return f"$sent{next(_ids)}"


def text(sender: str, body: str, event_id: Optional[str] = None, **content) -> Dict[str, Any]:
    """An ``m.text`` message event."""
    return {"type": "m.room.message", "sender": sender,
            "event_id": event_id or f"$e{next(_ids)}",
            "content": {"msgtype": "m.text", "body": body, **content}}


def member(user_id: str, membership: str = "join", displayname: Optional[str] = None) -> Dict[str, Any]:
    content: Dict[str, Any] = {"membership": membership}
    if displayname:
        content["displayname"] = displayname
    return {"type": "m.room.member", "state_key": user_id, "sender": user_id,
            "event_id": f"$m{next(_ids)}", "content": content}


def encryption() -> Dict[str, Any]:
    return {"type": "m.room.encryption", "state_key": "", "sender": ALICE,
            "event_id": f"$c{next(_ids)}", "content": {"algorithm": "m.megolm.v1.aes-sha2"}}


def joined(room_id: str, *timeline, state=(), members: Optional[int] = None,
           next_batch: str = "next") -> Dict[str, Any]:
    """A ``/sync`` answer with one joined room."""
    room: Dict[str, Any] = {"timeline": {"events": list(timeline)},
                            "state": {"events": list(state)}}
    if members is not None:
        room["summary"] = {"m.joined_member_count": members}
    return {"next_batch": next_batch, "rooms": {"join": {room_id: room}}}


def invited(room_id: str) -> Dict[str, Any]:
    return {"next_batch": "next",
            "rooms": {"invite": {room_id: {"invite_state": {"events": [
                {"type": "m.room.member", "state_key": BOT, "sender": ALICE,
                 "content": {"membership": "invite"}}]}}}}}


def merge(*answers: Dict[str, Any]) -> Dict[str, Any]:
    """Several ``/sync`` answers as one."""
    merged: Dict[str, Any] = {"next_batch": "next", "rooms": {}}
    for answer in answers:
        for section, rooms in answer.get("rooms", {}).items():
            merged["rooms"].setdefault(section, {}).update(rooms)
    return merged


def make_bridge(rooms: Optional[Dict[str, int]] = None, connect: bool = True,
                matrix: Optional[FakeMatrix] = None, **options):
    """A bridge on a :class:`FakeMatrix` and a mocked hub, after its initial sync.

    ``rooms`` maps each room the bot is in to its joined member count.
    """
    matrix = matrix or FakeMatrix()
    client = MagicMock()
    client.handshake_event = threading.Event()
    client.handshake_event.set()
    bridge = HiveMindMatrixBridge(client=client, matrix=matrix, **options)
    assert bridge.identify()
    if connect:
        bridge.connect_hivemind()
    initial = merge(*(joined(room_id, members=count)
                      for room_id, count in (rooms if rooms is not None
                                             else {ROOM: 3}).items()))
    bridge.process_sync(initial, initial=True)
    return bridge, matrix, client


def utterances(client) -> List[Any]:
    """``(text, context)`` for every utterance the bridge sent the hub."""
    sent = []
    for call in client.emit.call_args_list:
        hive_message = call.args[0]
        message = hive_message.payload
        sent.append((message.data["utterances"][0], message.context))
    return sent
