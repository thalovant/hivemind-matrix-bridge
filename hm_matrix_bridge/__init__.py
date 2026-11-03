"""HiveMind <-> Matrix bridge.

A HiveMind bridge is a satellite whose input and output are a chat platform
instead of a microphone. This one talks to the hub through
:class:`~hivemind_bus_client.HiveMessageBusClient` directly (composition, the
same shape as ``hivemind-telegram-bridge``) and to Matrix through the small
client-server API client in :mod:`hm_matrix_bridge.matrix`.

What it answers, in order of the checks in :meth:`HiveMindMatrixBridge.handle_message`:

- only ``m.text`` messages from someone other than the bot; edits, notices
  (other bots) and messages from before the bot joined are not;
- never in an end-to-end encrypted room: the bridge has no device keys, so it
  logs the room once and stays silent there;
- a room on the allow-list is always answered; with no allow-list every room
  the bot is in is. A direct message (a room with exactly two joined members,
  the bot and one person) that is not on the allow-list follows the DM
  policy: ``auto`` refuses it when an allow-list is set and accepts it
  otherwise;
- in a room with more people, with ``require_mention``, only a message that
  mentions the bot (an ``m.mentions`` pill, or its user id or display name in
  the text) or replies to one of its messages. The mention is removed before
  the text goes to the hub.

Invites are accepted only to rooms on the allow-list (to any room when there
is none); every other invite is left alone.

Connection lifecycle, spelled out because getting it wrong is the recurring
bug across HiveMind bridges:

- ``HiveMessageBusClient.connect()`` starts and owns the reconnect worker in a
  background thread and blocks until the handshake completes or fails. Call
  it exactly once, from :meth:`HiveMindMatrixBridge.start`; never also call
  ``run_forever()``.
- Nothing is forwarded before ``connect()`` returns, and nothing from the
  first (initial) sync is answered: it is history.
- A freshly registered HiveMind client is denied every message type until a
  hub admin runs ``hivemind-core allow-msg recognizer_loop:utterance
  <client_id>`` (and usually ``speak``); this bridge cannot do that itself.
"""
import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.parse import quote

import requests
from hivemind_bus_client import (
    HiveMessage,
    HiveMessageBusClient,
    HiveMessageType,
)
from ovos_bus_client.message import Message
from ovos_utils.log import LOG

from hm_matrix_bridge.health import HealthServer
from hm_matrix_bridge.matrix import (
    AccountMismatch,
    MatrixClient,
    MatrixError,
    TokenRejected,
)

platform = "HiveMindMatrixBridgeV0.3"

#: hivemind-bus-client's own default for ``handshake_max_retries`` is None,
#: retry forever. Bound it so an unreachable hub or a wrong password fails
#: fast instead of hanging the bridge.
DEFAULT_HANDSHAKE_MAX_RETRIES = 10

#: How conversations are kept apart on the hub. ``person``: one session per
#: person in each room, so in a shared room each person's follow-up questions
#: continue their own conversation. ``chat``: one session per room.
SESSION_SCOPES = ("person", "chat")

#: Direct messages that are not on the allow-list. ``auto`` refuses them when
#: an allow-list is set (a limit anyone could step around by opening a DM
#: with the bot is no limit) and accepts them otherwise.
DM_POLICIES = ("auto", "allow", "deny")

#: Joined members in a direct message: the bot and one person.
DM_MEMBER_COUNT = 2

#: Long-poll length of one ``/sync``, in milliseconds.
SYNC_TIMEOUT_MS = 30000

#: Longest pause between two failed syncs, in seconds.
MAX_SYNC_BACKOFF = 60.0

#: Remembered event ids (the bot's own messages, and looked-up reply targets).
EVENT_MEMORY = 1024


def sync_filter(timeline_limit: int) -> str:
    """The ``/sync`` filter: only what the bridge reads, members lazy-loaded.

    Lazy loading also makes the homeserver send each room's summary, whose
    ``m.joined_member_count`` says whether a room is a direct message.
    """
    nothing = {"not_types": ["*"]}
    return json.dumps({
        "presence": nothing,
        "account_data": nothing,
        "room": {
            "state": {"types": ["m.room.encryption", "m.room.member"],
                      "lazy_load_members": True},
            "timeline": {"types": ["m.room.message", "m.room.encrypted",
                                   "m.room.encryption", "m.room.member"],
                         "limit": timeline_limit,
                         "lazy_load_members": True},
            "ephemeral": nothing,
            "account_data": nothing,
        },
    }, separators=(",", ":"))


#: The first sync only learns the rooms; its messages are history.
INITIAL_SYNC_FILTER = sync_filter(1)
SYNC_FILTER = sync_filter(50)


@dataclass
class RoomState:
    """What the bridge knows about one joined room."""

    #: the room has ``m.room.encryption``; nothing there is answered
    encrypted: bool = False
    #: joined members, from the room summary or ``/joined_members``
    joined_members: Optional[int] = None
    #: the bot's display name in this room, when it has its own
    bot_display_name: Optional[str] = None


def _describe(error: Exception) -> str:
    """A failure, as it may be logged: never a URL, a body or a secret."""
    if isinstance(error, MatrixError):
        return error.describe()
    return type(error).__name__


def strip_reply_fallback(body: str) -> str:
    """``body`` without the quoted reply fallback older clients put first.

    A reply's plain body may start with the replied-to message quoted
    (``> <@author:server> text`` lines, then an empty line). That quote is
    not part of the question.
    """
    lines = body.split("\n")
    if not lines or not lines[0].startswith(">"):
        return body
    index = 0
    while index < len(lines) and lines[index].startswith(">"):
        index += 1
    if index < len(lines) and lines[index].strip() == "":
        index += 1
    return "\n".join(lines[index:])


class HiveMindMatrixBridge:
    """Bridge a Matrix bot account to a HiveMind node."""

    def __init__(self,
                 homeserver: Optional[str] = None,
                 access_token: Optional[str] = None,
                 user_id: Optional[str] = None,
                 key: Optional[str] = None,
                 password: Optional[str] = None,
                 host: Optional[str] = None,
                 port: int = 5678,
                 self_signed: bool = False,
                 lang: str = "en-us",
                 site_id: str = "matrix",
                 allowed_rooms: Optional[Iterable[str]] = None,
                 handshake_max_retries: int = DEFAULT_HANDSHAKE_MAX_RETRIES,
                 require_mention: bool = True,
                 session_scope: str = "person",
                 dm_policy: str = "auto",
                 health_port: Optional[int] = None,
                 *,
                 client: Optional[HiveMessageBusClient] = None,
                 matrix: Optional[MatrixClient] = None):
        """
        Parameters
        ----------
        homeserver: client API base URL, e.g. ``https://matrix.example.org``.
        access_token: the bot account's access token. ``homeserver`` and
            ``access_token`` are required unless ``matrix`` is injected.
        user_id: the bot's ``@user:server``. When given, the token must
            belong to it; when not, the token's own account is used.
        key, password, host, port, self_signed: HiveMind hub connection.
        lang: utterance language tag.
        site_id: this bridge's HiveMind site id.
        allowed_rooms: room ids (``!opaque:server``). When given, only these
            rooms are answered and only their invites accepted; direct
            messages outside them follow ``dm_policy``.
        handshake_max_retries: bound on the HiveMind handshake retries.
        require_mention: in a room with more than two members, answer only
            messages that mention the bot or reply to it.
        session_scope: ``"person"`` (one hub session per person per room) or
            ``"chat"`` (one per room).
        dm_policy: ``"deny"``, ``"allow"`` or ``"auto"`` for two-member rooms
            not on the allow-list; ``auto`` is ``deny`` when ``allowed_rooms``
            is given and ``allow`` otherwise.
        health_port: serve ``/healthz`` and ``/readyz`` on this port.
        client: pre-built HiveMessageBusClient (tests). It is not connected
            until :meth:`start` (or :meth:`connect_hivemind`).
        matrix: pre-built :class:`~hm_matrix_bridge.matrix.MatrixClient` (tests).
        """
        if matrix is None and not (homeserver and access_token):
            raise ValueError("homeserver and access_token are required unless a "
                             "Matrix client is injected")
        if session_scope not in SESSION_SCOPES:
            raise ValueError(f"session_scope must be one of {SESSION_SCOPES}, "
                             f"not {session_scope!r}")
        if dm_policy not in DM_POLICIES:
            raise ValueError(f"dm_policy must be one of {DM_POLICIES}, not {dm_policy!r}")

        self.lang = lang
        self.site_id = site_id
        self.allowed_rooms = set(allowed_rooms) if allowed_rooms else None
        self.handshake_max_retries = handshake_max_retries
        self.require_mention = require_mention
        self.session_scope = session_scope
        if dm_policy == "auto":
            dm_policy = "deny" if self.allowed_rooms is not None else "allow"
        self.dm_policy = dm_policy
        self.expected_user_id = user_id
        self.user_id: Optional[str] = None
        self.display_name: Optional[str] = None

        self.matrix = matrix or MatrixClient(homeserver, access_token)
        self.client = client or HiveMessageBusClient(
            key=key,
            password=password,
            host=host,
            port=port,
            useragent=platform,
            self_signed=self_signed,
        )

        self.rooms: Dict[str, RoomState] = {}
        self._since: Optional[str] = None
        self._events: "OrderedDict[Tuple[str, str], bool]" = OrderedDict()
        self._events_lock = threading.Lock()

        self.health_port = health_port
        self._health: Optional[HealthServer] = None
        self._started = False
        self._connected = threading.Event()
        self._synced = threading.Event()
        self._sync_ok = threading.Event()
        self._stop = threading.Event()
        self._token_rejected: Optional[TokenRejected] = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def identify(self) -> bool:
        """Ask the homeserver whose access token this is; ``False`` if stopped first.

        Raises :class:`~hm_matrix_bridge.matrix.TokenRejected` when it is
        refused, or when it belongs to another account than ``user_id``.
        A network failure or a server error is retried with a backoff.
        """
        failures = 0
        answer: Optional[Dict[str, Any]] = None
        while answer is None:
            if self._stop.is_set():
                return False
            try:
                answer = self.matrix.whoami()
            except TokenRejected:
                raise
            except (MatrixError, requests.RequestException) as error:
                failures += 1
                wait = self._backoff(error, failures)
                LOG.warning(f"cannot reach the Matrix homeserver ({_describe(error)}); "
                            f"retrying in {wait:.0f}s")
                self._stop.wait(wait)
        user_id = answer.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise MatrixError(200, "NO_USER_ID")
        if self.expected_user_id and user_id != self.expected_user_id:
            raise AccountMismatch(self.expected_user_id, user_id)
        self.user_id = user_id
        try:
            self.display_name = self.matrix.display_name(user_id)
        except TokenRejected:
            raise
        except (MatrixError, requests.RequestException) as error:
            LOG.warning(f"could not read the bot's display name ({_describe(error)}); "
                        "mentions by display name will not be recognised")
        LOG.info(f"== Matrix account {user_id}")
        return True

    def connect_hivemind(self) -> None:
        """Connect to the HiveMind hub and wait for the handshake.

        Calls ``HiveMessageBusClient.connect()`` exactly once, with a finite
        ``handshake_max_retries``; it raises ``ConnectionRefusedError`` when
        the hub refuses the credentials.
        """
        self.client.connect(site_id=self.site_id,
                            handshake_max_retries=self.handshake_max_retries)
        self.client.on_mycroft("speak", self.handle_speak)
        self.client.on_mycroft("hive.complete_intent_failure",
                               self.handle_intent_failure)
        self._connected.set()
        LOG.info("== connected to HiveMind")

    def start(self) -> None:
        """Check the Matrix token, connect to HiveMind, then sync until stopped.

        Blocks the calling thread. Raises
        :class:`~hm_matrix_bridge.matrix.TokenRejected` when the homeserver
        refuses the access token, at start or later.
        """
        if self._started:
            return
        self._started = True
        if self.health_port is not None:
            self._health = HealthServer(self.health_port, self.ready)
            self._health.start()
        try:
            if not self.identify():
                return
            self.connect_hivemind()
            LOG.warning(
                "a freshly registered HiveMind client is denied every message "
                "type until an admin runs `hivemind-core allow-msg "
                "recognizer_loop:utterance <client_id>` on the hub (and "
                "usually `speak` too). If messages seem to vanish silently, "
                "check that first."
            )
            self.run_matrix()
        finally:
            self.stop()

    def run_matrix(self) -> None:
        """The initial sync, then long-poll ``/sync`` until :meth:`stop`."""
        self._sync_until_done(initial=True)
        if self._stop.is_set():
            return
        self._synced.set()
        LOG.info(f"== listening to Matrix ({len(self.rooms)} rooms joined)")
        while not self._stop.is_set():
            self._sync_until_done(initial=False)
        if self._token_rejected is not None:
            raise self._token_rejected

    def _sync_until_done(self, initial: bool) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                answer = self.matrix.sync(
                    since=None if initial else self._since,
                    timeout_ms=0 if initial else SYNC_TIMEOUT_MS,
                    filter_json=INITIAL_SYNC_FILTER if initial else SYNC_FILTER,
                )
            except TokenRejected:
                self._sync_ok.clear()
                raise
            except (MatrixError, requests.RequestException) as error:
                self._sync_ok.clear()
                failures += 1
                wait = self._backoff(error, failures)
                LOG.warning(f"Matrix sync failed ({_describe(error)}); retrying in {wait:.0f}s")
                self._stop.wait(wait)
                continue
            self._sync_ok.set()
            next_batch = answer.get("next_batch")
            if isinstance(next_batch, str) and next_batch:
                self._since = next_batch
            self.process_sync(answer, initial=initial)
            return

    @staticmethod
    def _backoff(error: Exception, failures: int) -> float:
        if isinstance(error, MatrixError) and error.rate_limited and error.retry_after_ms:
            return min(error.retry_after_ms / 1000.0, MAX_SYNC_BACKOFF)
        return min(2.0 ** min(failures, 6), MAX_SYNC_BACKOFF)

    def stop(self) -> None:
        """Stop syncing and close the HiveMind connection."""
        self._stop.set()
        try:
            self.client.close()
        except Exception:
            LOG.exception("error closing HiveMind client")
        self._started = False
        self._connected.clear()
        self._sync_ok.clear()
        if self._health is not None:
            self._health.stop()
            self._health = None

    def ready(self) -> bool:
        """Whether a message can go end to end right now.

        The HiveMind session must be live (the client clears its handshake
        event when the connection drops), the initial sync must be done and
        the last sync must have succeeded.
        """
        return bool(self.hub_live() and self._synced.is_set() and self._sync_ok.is_set())

    def hub_live(self) -> bool:
        """Whether the HiveMind session is up right now, not only once established.

        The client clears its ``handshake_event`` when the connection drops
        and sets it again once the reconnect worker has a new session.
        """
        handshake = getattr(self.client, "handshake_event", None)
        return self._connected.is_set() and (handshake is None or handshake.is_set())

    # ------------------------------------------------------------------
    # sync processing
    # ------------------------------------------------------------------
    def process_sync(self, answer: Dict[str, Any], initial: bool = False) -> None:
        """Apply one ``/sync`` answer: rooms left, rooms joined, invites.

        With ``initial`` the rooms are learned and nothing is answered.
        """
        rooms = _mapping(answer.get("rooms"))
        for room_id in _mapping(rooms.get("leave")):
            self.rooms.pop(room_id, None)
        for room_id, room in _mapping(rooms.get("join")).items():
            if isinstance(room, dict):
                self._process_joined_room(room_id, room, initial)
        for room_id in _mapping(rooms.get("invite")):
            self.handle_invite(room_id)

    def _process_joined_room(self, room_id: str, room: Dict[str, Any], initial: bool) -> None:
        # A room first seen after the initial sync was just joined: its
        # timeline can hold messages from before the bot was there.
        new_room = room_id not in self.rooms and not initial
        state = self.rooms.setdefault(room_id, RoomState())
        member_changed = False
        for event in _section_events(room.get("state")):
            member_changed |= self._apply_state(room_id, state, event)
        answering = not initial and not new_room
        for event in _section_events(room.get("timeline")):
            event_type = event.get("type")
            if "state_key" in event:
                member_changed |= self._apply_state(room_id, state, event)
                if (new_room and event_type == "m.room.member"
                        and event.get("state_key") == self.user_id
                        and _mapping(event.get("content")).get("membership") == "join"):
                    answering = True
                continue
            if event_type == "m.room.encrypted":
                self._mark_encrypted(room_id, state)
            elif event_type == "m.room.message":
                if event.get("sender") == self.user_id:
                    self._remember(room_id, event.get("event_id"), True)
                elif answering:
                    self._handle_one(room_id, event)
        summary = _mapping(room.get("summary"))
        count = summary.get("m.joined_member_count")
        if isinstance(count, int) and not isinstance(count, bool):
            state.joined_members = count
        elif member_changed:
            state.joined_members = None  # recount on the next message

    def _handle_one(self, room_id: str, event: Dict[str, Any]) -> None:
        """:meth:`handle_message`, so that one event it cannot handle drops only itself.

        Every room member decides what an event's ``content`` holds; an
        exception from one must not end the sync loop (and with it the
        bridge). A refused token still does.
        """
        try:
            self.handle_message(room_id, event)
        except TokenRejected:
            raise
        except Exception as error:
            LOG.warning(f"dropped a message in room {room_id} that could not be handled "
                        f"({type(error).__name__})")

    def _apply_state(self, room_id: str, state: RoomState, event: Dict[str, Any]) -> bool:
        """Apply one state event; whether it changed someone's membership."""
        event_type = event.get("type")
        content = event.get("content") if isinstance(event.get("content"), dict) else {}
        if event_type == "m.room.encryption" and content:
            self._mark_encrypted(room_id, state)
        elif event_type == "m.room.member":
            if event.get("state_key") == self.user_id:
                name = content.get("displayname")
                state.bot_display_name = name if isinstance(name, str) and name.strip() else None
            return True
        return False

    def _mark_encrypted(self, room_id: str, state: RoomState) -> None:
        if state.encrypted:
            return
        state.encrypted = True
        # Once per room. Encrypted rooms need device keys this bridge does not keep.
        LOG.warning(f"room {room_id} is end-to-end encrypted; encrypted rooms are "
                    "not supported, so the bridge will not answer there")

    def handle_invite(self, room_id: str) -> None:
        """Accept an invite to an allowed room; leave every other one alone."""
        if self.allowed_rooms is not None and room_id not in self.allowed_rooms:
            LOG.debug(f"ignoring an invite to room {room_id}: not on the allow-list")
            return
        try:
            self.matrix.join(room_id)
        except TokenRejected:
            raise
        except (MatrixError, requests.RequestException) as error:
            LOG.warning(f"could not accept the invite to room {room_id} ({_describe(error)})")
            return
        LOG.info(f"joined room {room_id} on invite")

    # ------------------------------------------------------------------
    # Matrix -> HiveMind
    # ------------------------------------------------------------------
    def handle_message(self, room_id: str, event: Dict[str, Any]) -> None:
        """Forward one ``m.room.message`` to the hub, or drop it.

        Dropped when: it is not ``m.text`` or has no text, it came from the
        bot, it edits an earlier message, the room is encrypted or not one
        the bridge answers in, it is not addressed to the bot, or HiveMind
        has not completed its handshake yet.
        """
        sender = event.get("sender")
        if not isinstance(sender, str) or sender == self.user_id:
            return
        content = event.get("content") if isinstance(event.get("content"), dict) else {}
        if content.get("msgtype") != "m.text":
            return  # m.notice is what other bots send; never answer one
        body = content.get("body")
        if not isinstance(body, str) or not body.strip():
            return
        relates = content.get("m.relates_to") if isinstance(content.get("m.relates_to"), dict) else {}
        if relates.get("rel_type") == "m.replace" or "m.new_content" in content:
            return  # an edit: the original was already seen
        state = self.rooms.get(room_id)
        if state is None or state.encrypted:
            return
        allowed, is_dm = self.answer_policy(room_id)
        if not allowed:
            LOG.debug(f"ignoring a message in room {room_id}: not a room the bridge answers in")
            return

        text = self.addressed_text(room_id, content, is_dm)
        if text is None:
            return

        if not self.hub_live():
            LOG.warning("dropping a Matrix message, the HiveMind session is not up")
            return

        thread_id = _event_id(relates.get("event_id")) if relates.get("rel_type") == "m.thread" else None
        self.forward_to_hivemind(text, sender, room_id, thread_id)

    def answer_policy(self, room_id: str) -> Tuple[bool, bool]:
        """``(answered, is_direct_message)`` for ``room_id``.

        A room on the allow-list is always answered. A direct message that is
        not follows ``dm_policy``. Any other room is answered only when there
        is no allow-list.
        """
        if self.allowed_rooms is not None and room_id in self.allowed_rooms:
            return True, self.is_direct(room_id)
        if self.allowed_rooms is not None and self.dm_policy == "deny":
            return False, False  # nothing outside the list can be answered
        is_dm = self.is_direct(room_id)
        if is_dm:
            return self.dm_policy == "allow", True
        return self.allowed_rooms is None, False

    def is_direct(self, room_id: str) -> bool:
        """Whether ``room_id`` has exactly two joined members, the bot and one person."""
        state = self.rooms.get(room_id)
        if state is None:
            return False
        if state.joined_members is None:
            try:
                joined = self.matrix.joined_members(room_id).get("joined")
            except TokenRejected:
                raise
            except (MatrixError, requests.RequestException) as error:
                LOG.warning(f"could not count the members of room {room_id} ({_describe(error)})")
                return False
            if isinstance(joined, dict):
                state.joined_members = len(joined)
        return state.joined_members == DM_MEMBER_COUNT

    def addressed_text(self, room_id: str, content: Dict[str, Any], is_dm: bool) -> Optional[str]:
        """The text to forward, or ``None`` when the message is not for the bot.

        The bot's user id and display name are removed wherever they appear,
        with a following ``:`` or ``,``: "HiveBot: what time is it" asks "what
        time is it". A reply's quoted fallback is removed too. In a room with
        more than two members, with ``require_mention``, a message that
        neither mentions the bot nor replies to one of its messages is not
        addressed to it.
        """
        body = content.get("body") or ""
        relates = content.get("m.relates_to") if isinstance(content.get("m.relates_to"), dict) else {}
        reply_to = _event_id(_mapping(relates.get("m.in_reply_to")).get("event_id"))
        if relates.get("is_falling_back") is True:
            reply_to = None  # a thread's fallback reply, not a real one
        if reply_to:
            body = strip_reply_fallback(body)

        mentions = content.get("m.mentions") if isinstance(content.get("m.mentions"), dict) else {}
        user_ids = mentions.get("user_ids") if isinstance(mentions.get("user_ids"), list) else []
        pill = self.user_id is not None and self.user_id in user_ids

        text, found = self._strip_mentions(room_id, body)
        text = " ".join(text.split())
        if not text:
            return None
        if self.require_mention and not is_dm:
            if not (pill or found or (reply_to and self.is_bot_event(room_id, reply_to))):
                return None
        return text

    def _strip_mentions(self, room_id: str, body: str) -> Tuple[str, bool]:
        """``body`` without the bot's user id and display names; whether any was there."""
        if not self.user_id:
            return body, False
        found = False
        encoded = quote(self.user_id, safe="")
        targets = [rf"(?:{re.escape(self.user_id)}|{re.escape(encoded)})"]
        # A markdown pill: [Name](https://matrix.to/#/@bot:server)
        link = re.compile(rf"\[[^\]\n]*\]\(https?://matrix\.to/#/{targets[0]}\)[:,]?",
                          re.IGNORECASE)
        body, count = link.subn(" ", body)
        found |= count > 0
        names = {self.user_id}
        state = self.rooms.get(room_id)
        for name in (self.display_name, state.bot_display_name if state else None):
            if name and name.strip():
                names.add(name.strip())
        for name in sorted(names, key=len, reverse=True):
            pattern = re.compile(rf"(?<![\w@]){re.escape(name)}(?![\w])[:,]?", re.IGNORECASE)
            body, count = pattern.subn(" ", body)
            found |= count > 0
        return body, found

    def is_bot_event(self, room_id: str, event_id: str) -> bool:
        """Whether ``event_id`` in ``room_id`` was sent by the bot.

        Answered from memory for the bot's own recent messages, else by
        looking the event up once.
        """
        with self._events_lock:
            known = self._events.get((room_id, event_id))
        if known is not None:
            return known
        try:
            event = self.matrix.event(room_id, event_id)
        except TokenRejected:
            raise
        except (MatrixError, requests.RequestException) as error:
            LOG.debug(f"could not look up a replied-to event in room {room_id} "
                      f"({_describe(error)})")
            return False
        mine = event.get("sender") == self.user_id
        self._remember(room_id, event_id, mine)
        return mine

    def _remember(self, room_id: str, event_id: Optional[str], mine: bool) -> None:
        if not isinstance(event_id, str) or not event_id:
            return
        with self._events_lock:
            self._events[(room_id, event_id)] = mine
            self._events.move_to_end((room_id, event_id))
            while len(self._events) > EVENT_MEMORY:
                self._events.popitem(last=False)

    def forward_to_hivemind(self, text: str, sender: str, room_id: str,
                            thread_id: Optional[str] = None) -> None:
        """Send ``text`` to the hub as an utterance from ``sender`` in ``room_id``."""
        context = {
            "source": platform,
            "destination": "HiveMind",
            "platform": platform,
            "room_id": room_id,
            "user": {"matrix_user_id": sender},
            "session": {"session_id": self.session_id(room_id, sender)},
        }
        if thread_id:
            context["thread_id"] = thread_id
        msg = Message("recognizer_loop:utterance",
                      {"utterances": [text], "lang": self.lang},
                      context)
        try:
            self.client.emit(HiveMessage(HiveMessageType.BUS, msg))
        except Exception as error:
            # While the hub is unreachable hivemind-bus-client raises a
            # RuntimeError whose text is the whole serialized message, the
            # question included: log the type only, and keep syncing.
            LOG.warning(f"could not forward a message to HiveMind "
                        f"({type(error).__name__}); dropped")

    def session_id(self, room_id: str, sender: Optional[str]) -> str:
        """The hub session a message from ``sender`` in ``room_id`` belongs to."""
        if self.session_scope == "person" and sender:
            return f"matrix-{room_id}-{sender}"
        return f"matrix-{room_id}"

    # ------------------------------------------------------------------
    # HiveMind -> Matrix
    # ------------------------------------------------------------------
    def handle_speak(self, message: Message) -> None:
        room_id = message.context.get("room_id")
        if not room_id:
            return
        utterance = message.data.get("utterance")
        if not utterance:
            return
        self.speak(utterance, room_id, message.context.get("thread_id"))

    def handle_intent_failure(self, message: Message) -> None:
        room_id = message.context.get("room_id")
        if not room_id:
            return
        LOG.error("complete intent failure")
        self.speak("I don't know how to answer that", room_id,
                   message.context.get("thread_id"))

    def speak(self, text: str, room_id: str, thread_id: Optional[str] = None) -> None:
        """Post ``text`` to ``room_id`` (in ``thread_id`` when the question was in a thread).

        Runs on the HiveMind bus thread. Only a room the bridge answers in
        gets a reply, whatever the hub's context names.
        """
        state = self.rooms.get(room_id)
        if state is None or state.encrypted or not self.answer_policy(room_id)[0]:
            LOG.warning(f"not replying in room {room_id}: not a room the bridge answers in")
            return
        content: Dict[str, Any] = {"msgtype": "m.text", "body": text,
                                   # mentions nobody: a reply never pings anyone
                                   "m.mentions": {}}
        if thread_id:
            content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id,
                                       "is_falling_back": True,
                                       "m.in_reply_to": {"event_id": thread_id}}
        # The length, never the text: replies carry what people asked about.
        LOG.debug(f"sending to Matrix room {room_id}: {len(text)} characters")
        try:
            event_id = self.matrix.send_message(room_id, content)
        except TokenRejected as error:
            LOG.error(f"the homeserver refused the access token while sending ({_describe(error)})")
            self._token_rejected = error
            self._stop.set()
            return
        except (MatrixError, requests.RequestException) as error:
            LOG.error(f"failed to send to Matrix room {room_id} ({_describe(error)})")
            return
        self._remember(room_id, event_id, True)


def _section_events(section: Any) -> Iterable[Dict[str, Any]]:
    events = section.get("events") if isinstance(section, dict) else None
    return [event for event in events if isinstance(event, dict)] if isinstance(events, list) else []


def _mapping(value: Any) -> Dict[str, Any]:
    """``value`` when it is a JSON object, else an empty one."""
    return value if isinstance(value, dict) else {}


def _event_id(value: Any) -> Optional[str]:
    """``value`` when it can be an event id (a non-empty string), else ``None``.

    Anything in an event's ``content`` is whatever the sender put there: the
    homeserver does not check the shape of ``m.relates_to``.
    """
    return value if isinstance(value, str) and value else None


__all__ = ["HiveMindMatrixBridge", "platform"]
