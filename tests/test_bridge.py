"""Which Matrix messages reach the hub, and where the hub's replies go.

Matrix is a :class:`fakes.FakeMatrix` and the hub a mock, so this checks the
bridge's own decisions; ``tests/test_end_to_end.py`` runs the same path over
real HTTP.
"""
import threading
from unittest.mock import MagicMock

import pytest
from ovos_bus_client.message import Message

import hm_matrix_bridge as bridge_module
from fakes import (ALICE, BOB, BOT, BOT_NAME, DM, OTHER, ROOM, FakeMatrix, encryption,
                   invited, joined, make_bridge, member, text, utterances)
from hm_matrix_bridge import HiveMindMatrixBridge
from hm_matrix_bridge.matrix import AccountMismatch, MatrixError, TokenRejected


def say(bridge, room_id, event, members=None):
    bridge.process_sync(joined(room_id, event, members=members))


# ----------------------------------------------------------------------
# construction and the hub connection
# ----------------------------------------------------------------------
def test_homeserver_and_token_are_required_without_an_injected_client():
    with pytest.raises(ValueError):
        HiveMindMatrixBridge(client=MagicMock())


def test_an_unknown_session_scope_or_dm_policy_is_refused():
    with pytest.raises(ValueError):
        HiveMindMatrixBridge(client=MagicMock(), matrix=FakeMatrix(), session_scope="room")
    with pytest.raises(ValueError):
        HiveMindMatrixBridge(client=MagicMock(), matrix=FakeMatrix(), dm_policy="sometimes")


def test_connect_bounds_the_handshake_and_names_the_site():
    bridge, _, client = make_bridge(site_id="matrix-kitchen")
    client.connect.assert_called_once_with(
        site_id="matrix-kitchen",
        handshake_max_retries=bridge_module.DEFAULT_HANDSHAKE_MAX_RETRIES)
    assert bridge_module.DEFAULT_HANDSHAKE_MAX_RETRIES is not None
    handlers = {call.args[0] for call in client.on_mycroft.call_args_list}
    assert handlers == {"speak", "hive.complete_intent_failure"}


def test_a_refused_hub_identity_is_not_swallowed():
    client = MagicMock()
    client.connect.side_effect = ConnectionRefusedError("HiveMind refused this identity")
    bridge = HiveMindMatrixBridge(client=client, matrix=FakeMatrix())
    with pytest.raises(ConnectionRefusedError):
        bridge.connect_hivemind()


# ----------------------------------------------------------------------
# who the bot is
# ----------------------------------------------------------------------
def test_identify_learns_the_account_and_its_display_name():
    bridge = HiveMindMatrixBridge(client=MagicMock(), matrix=FakeMatrix())
    assert bridge.identify()
    assert bridge.user_id == BOT
    assert bridge.display_name == BOT_NAME


def test_a_refused_token_stops_identify():
    matrix = FakeMatrix()
    matrix.whoami_results = [TokenRejected(401, "M_UNKNOWN_TOKEN")]
    bridge = HiveMindMatrixBridge(client=MagicMock(), matrix=matrix)
    with pytest.raises(TokenRejected):
        bridge.identify()


def test_a_token_for_another_account_is_refused():
    bridge = HiveMindMatrixBridge(client=MagicMock(), matrix=FakeMatrix(), user_id="@other:example.org")
    with pytest.raises(AccountMismatch) as refused:
        bridge.identify()
    assert isinstance(refused.value, TokenRejected)


def test_an_unreachable_homeserver_is_retried(monkeypatch):
    monkeypatch.setattr(HiveMindMatrixBridge, "_backoff", staticmethod(lambda error, failures: 0))
    matrix = FakeMatrix()
    matrix.whoami_results = [MatrixError(502), MatrixError(503)]
    bridge = HiveMindMatrixBridge(client=MagicMock(), matrix=matrix)
    assert bridge.identify()
    assert bridge.user_id == BOT


def test_start_checks_the_token_before_touching_the_hub():
    matrix = FakeMatrix()
    matrix.whoami_results = [TokenRejected(401, "M_UNKNOWN_TOKEN")]
    client = MagicMock()
    bridge = HiveMindMatrixBridge(client=client, matrix=matrix)
    with pytest.raises(TokenRejected):
        bridge.start()
    client.connect.assert_not_called()
    client.close.assert_called_once()


# ----------------------------------------------------------------------
# history, own messages, notices, edits
# ----------------------------------------------------------------------
def test_the_initial_sync_is_history_and_is_not_answered():
    matrix = FakeMatrix()
    client = MagicMock()
    bridge = HiveMindMatrixBridge(client=client, matrix=matrix, require_mention=False)
    bridge.identify()
    bridge.connect_hivemind()
    bridge.process_sync(joined(ROOM, text(ALICE, "an old question"), members=3), initial=True)
    client.emit.assert_not_called()
    assert ROOM in bridge.rooms
    say(bridge, ROOM, text(ALICE, "a new question"))
    assert [t for t, _ in utterances(client)] == ["a new question"]


def test_a_message_reaches_the_hub_with_its_room_and_session():
    bridge, _, client = make_bridge({ROOM: 3}, allowed_rooms=[ROOM], lang="fr-fr")
    say(bridge, ROOM, text(ALICE, f"{BOT_NAME}: turn on the lights"))
    [(utterance, context)] = utterances(client)
    assert utterance == "turn on the lights"
    message = client.emit.call_args.args[0].payload
    assert message.msg_type == "recognizer_loop:utterance"
    assert message.data["lang"] == "fr-fr"
    assert context["room_id"] == ROOM
    assert context["user"] == {"matrix_user_id": ALICE}
    assert context["session"]["session_id"] == f"matrix-{ROOM}-{ALICE}"
    assert context["destination"] == "HiveMind"


def test_the_bot_never_answers_itself():
    bridge, _, client = make_bridge({DM: 2})
    say(bridge, DM, text(BOT, "my own reply"))
    client.emit.assert_not_called()


def test_notices_edits_and_non_text_are_not_answered():
    bridge, _, client = make_bridge({DM: 2})
    notice = text(ALICE, "from another bot")
    notice["content"]["msgtype"] = "m.notice"
    edit = text(ALICE, "* fixed", **{"m.new_content": {"msgtype": "m.text", "body": "fixed"},
                                      "m.relates_to": {"rel_type": "m.replace", "event_id": "$x"}})
    image = text(ALICE, "photo.jpg")
    image["content"]["msgtype"] = "m.image"
    blank = text(ALICE, "   ")
    for event in (notice, edit, image, blank):
        say(bridge, DM, event)
    client.emit.assert_not_called()


def test_nothing_is_forwarded_before_the_hub_handshake():
    bridge, _, client = make_bridge({DM: 2}, connect=False)
    say(bridge, DM, text(ALICE, "hello"))
    client.emit.assert_not_called()


# ----------------------------------------------------------------------
# allow-list and direct messages
# ----------------------------------------------------------------------
def test_with_an_allow_list_only_those_rooms_are_answered():
    bridge, _, client = make_bridge({ROOM: 3, OTHER: 3}, allowed_rooms=[ROOM],
                                    require_mention=False)
    say(bridge, OTHER, text(ALICE, "not here"))
    say(bridge, ROOM, text(ALICE, "here"))
    assert [t for t, _ in utterances(client)] == ["here"]


def test_without_an_allow_list_every_room_is_answered():
    bridge, _, client = make_bridge({ROOM: 3, OTHER: 5}, require_mention=False)
    say(bridge, ROOM, text(ALICE, "one"))
    say(bridge, OTHER, text(ALICE, "two"))
    assert [t for t, _ in utterances(client)] == ["one", "two"]


def test_a_dm_cannot_step_around_the_allow_list():
    bridge, _, client = make_bridge({ROOM: 3, DM: 2}, allowed_rooms=[ROOM])
    assert bridge.dm_policy == "deny"
    say(bridge, DM, text(ALICE, "let me in"))
    client.emit.assert_not_called()


def test_a_listed_two_member_room_is_answered_whatever_the_dm_policy():
    bridge, _, client = make_bridge({DM: 2}, allowed_rooms=[DM], dm_policy="deny")
    say(bridge, DM, text(ALICE, "what time is it"))
    assert [t for t, _ in utterances(client)] == ["what time is it"]


def test_dm_policy_allow_opens_dms_outside_the_allow_list_but_not_groups():
    bridge, _, client = make_bridge({ROOM: 3, DM: 2, OTHER: 4}, allowed_rooms=[ROOM],
                                    dm_policy="allow", require_mention=False)
    say(bridge, DM, text(ALICE, "dm"))
    say(bridge, OTHER, text(ALICE, "group"))
    assert [t for t, _ in utterances(client)] == ["dm"]


def test_without_an_allow_list_dms_are_allowed_unless_denied():
    bridge, _, client = make_bridge({DM: 2})
    assert bridge.dm_policy == "allow"
    say(bridge, DM, text(ALICE, "hello"))
    assert len(utterances(client)) == 1

    bridge, _, client = make_bridge({DM: 2, ROOM: 3}, dm_policy="deny", require_mention=False)
    say(bridge, DM, text(ALICE, "hello"))
    say(bridge, ROOM, text(ALICE, "group"))
    assert [t for t, _ in utterances(client)] == ["group"]


def test_the_member_count_is_fetched_when_the_summary_has_none_and_kept():
    matrix = FakeMatrix()
    matrix.members[DM] = [BOT, ALICE]
    bridge, _, client = make_bridge({}, matrix=matrix)
    bridge.process_sync(joined(DM, member(BOT), member(ALICE)), initial=True)
    say(bridge, DM, text(ALICE, "first"))
    say(bridge, DM, text(ALICE, "second"))
    assert matrix.member_calls == [DM]
    assert [t for t, _ in utterances(client)] == ["first", "second"]


def test_a_membership_change_without_a_summary_recounts():
    matrix = FakeMatrix()
    bridge, _, client = make_bridge({DM: 2}, matrix=matrix)
    matrix.members[DM] = [BOT, ALICE, BOB]
    bridge.process_sync(joined(DM, member(BOB)))
    say(bridge, DM, text(ALICE, "no mention, three people now"))
    assert matrix.member_calls == [DM]
    client.emit.assert_not_called()


def test_a_room_that_cannot_be_counted_is_not_a_dm():
    matrix = FakeMatrix()  # joined_members answers 403 for every room
    bridge, _, client = make_bridge({}, matrix=matrix, dm_policy="allow", allowed_rooms=[ROOM])
    bridge.process_sync(joined(OTHER), initial=True)
    say(bridge, OTHER, text(ALICE, "hello"))
    client.emit.assert_not_called()


# ----------------------------------------------------------------------
# invites and joining
# ----------------------------------------------------------------------
def test_invites_are_accepted_only_to_allowed_rooms():
    bridge, matrix, _ = make_bridge({}, allowed_rooms=[ROOM])
    bridge.process_sync(invited(OTHER))
    bridge.process_sync(invited(ROOM))
    assert matrix.joins == [ROOM]


def test_without_an_allow_list_every_invite_is_accepted():
    bridge, matrix, _ = make_bridge({})
    bridge.process_sync(invited(OTHER))
    assert matrix.joins == [OTHER]


def test_a_failed_join_is_logged_not_raised():
    bridge, matrix, _ = make_bridge({}, allowed_rooms=[ROOM])
    matrix.join_error = MatrixError(403, "M_FORBIDDEN")
    bridge.process_sync(invited(ROOM))
    assert matrix.joins == [ROOM]


def test_pending_invites_in_the_initial_sync_are_accepted():
    matrix = FakeMatrix()
    bridge = HiveMindMatrixBridge(client=MagicMock(), matrix=matrix, allowed_rooms=[ROOM])
    bridge.identify()
    bridge.process_sync(invited(ROOM), initial=True)
    assert matrix.joins == [ROOM]


def test_messages_from_before_the_bot_joined_are_not_answered():
    bridge, _, client = make_bridge({}, allowed_rooms=[ROOM], require_mention=False)
    bridge.process_sync(invited(ROOM))
    bridge.process_sync(joined(ROOM, text(ALICE, "said before the bot came"), member(BOT),
                               text(ALICE, "said after"), members=3))
    assert [t for t, _ in utterances(client)] == ["said after"]


def test_a_new_room_whose_join_is_not_in_the_timeline_answers_from_the_next_sync():
    bridge, _, client = make_bridge({}, allowed_rooms=[ROOM], require_mention=False)
    bridge.process_sync(joined(ROOM, text(ALICE, "unknown age"), members=3))
    say(bridge, ROOM, text(ALICE, "new"))
    assert [t for t, _ in utterances(client)] == ["new"]


def test_a_left_room_is_forgotten():
    bridge, _, client = make_bridge({ROOM: 3}, require_mention=False)
    bridge.process_sync({"rooms": {"leave": {ROOM: {}}}})
    assert ROOM not in bridge.rooms


# ----------------------------------------------------------------------
# encrypted rooms
# ----------------------------------------------------------------------
def capture_log(monkeypatch):
    lines = []
    log = MagicMock()
    for level in ("debug", "info", "warning", "error", "exception"):
        getattr(log, level).side_effect = (
            lambda *a, level=level, **k: lines.append((level, " ".join(map(str, a)))))
    monkeypatch.setattr(bridge_module, "LOG", log)
    return lines


def test_an_encrypted_room_is_never_answered_and_logged_once(monkeypatch):
    lines = capture_log(monkeypatch)
    bridge, _, client = make_bridge({}, allowed_rooms=[ROOM], require_mention=False)
    bridge.process_sync(joined(ROOM, state=[encryption()], members=3), initial=True)
    say(bridge, ROOM, text(ALICE, "plaintext anyway"))
    say(bridge, ROOM, text(ALICE, "again"))
    client.emit.assert_not_called()
    warnings = [line for level, line in lines if level == "warning" and "encrypted" in line]
    assert len(warnings) == 1
    assert ROOM in warnings[0]


def test_encryption_turned_on_later_stops_the_answers():
    bridge, _, client = make_bridge({ROOM: 3}, require_mention=False)
    bridge.process_sync(joined(ROOM, encryption(), text(ALICE, "after")))
    client.emit.assert_not_called()
    assert bridge.rooms[ROOM].encrypted


def test_an_encrypted_event_marks_the_room_encrypted():
    bridge, _, client = make_bridge({ROOM: 3}, require_mention=False)
    bridge.process_sync(joined(ROOM, {"type": "m.room.encrypted", "sender": ALICE,
                                      "event_id": "$enc", "content": {"ciphertext": "x"}}))
    say(bridge, ROOM, text(ALICE, "plain"))
    client.emit.assert_not_called()


# ----------------------------------------------------------------------
# HiveMind -> Matrix
# ----------------------------------------------------------------------
def speak(bridge, utterance, room_id, **context):
    bridge.handle_speak(Message("speak", {"utterance": utterance}, {"room_id": room_id, **context}))


def test_a_reply_goes_back_to_the_room_and_mentions_nobody():
    bridge, matrix, _ = make_bridge({ROOM: 3})
    speak(bridge, "It is noon", ROOM)
    [(room_id, content)] = matrix.sent
    assert room_id == ROOM
    assert content == {"msgtype": "m.text", "body": "It is noon", "m.mentions": {}}


def test_the_bot_remembers_its_replies_as_its_own():
    bridge, matrix, _ = make_bridge({ROOM: 3})
    speak(bridge, "It is noon", ROOM)
    [own] = [key for key, mine in bridge._events.items() if mine]
    assert own[0] == ROOM


def test_a_reply_never_goes_to_a_room_the_bridge_does_not_answer_in():
    bridge, matrix, _ = make_bridge({ROOM: 3, OTHER: 3}, allowed_rooms=[ROOM])
    speak(bridge, "leak", OTHER)
    speak(bridge, "leak", "!unknown:example.org")
    bridge.process_sync(joined(ROOM, encryption()))
    speak(bridge, "leak", ROOM)
    assert matrix.sent == []


def test_a_question_in_a_thread_is_answered_in_the_thread():
    bridge, matrix, client = make_bridge({ROOM: 3})
    say(bridge, ROOM, text(ALICE, f"{BOT_NAME} hi", **{
        "m.relates_to": {"rel_type": "m.thread", "event_id": "$root",
                         "is_falling_back": True, "m.in_reply_to": {"event_id": "$last"}}}))
    [(_, context)] = utterances(client)
    assert context["thread_id"] == "$root"
    speak(bridge, "hello", ROOM, thread_id="$root")
    relates = matrix.sent[0][1]["m.relates_to"]
    assert relates["rel_type"] == "m.thread" and relates["event_id"] == "$root"


def test_a_complete_intent_failure_gets_the_fallback_line():
    bridge, matrix, _ = make_bridge({ROOM: 3})
    bridge.handle_intent_failure(Message("hive.complete_intent_failure", {}, {"room_id": ROOM}))
    assert matrix.sent[0][1]["body"] == "I don't know how to answer that"


def test_speak_without_a_room_or_text_sends_nothing():
    bridge, matrix, _ = make_bridge({ROOM: 3})
    bridge.handle_speak(Message("speak", {"utterance": "x"}, {}))
    bridge.handle_speak(Message("speak", {"utterance": ""}, {"room_id": ROOM}))
    assert matrix.sent == []


def test_a_failed_send_is_logged_not_raised():
    bridge, matrix, _ = make_bridge({ROOM: 3})
    matrix.send_error = MatrixError(500)
    speak(bridge, "x", ROOM)
    assert len(matrix.sent) == 1


# ----------------------------------------------------------------------
# the sync loop
# ----------------------------------------------------------------------
def looping_bridge(matrix, **options):
    client = MagicMock()
    client.handshake_event = threading.Event()
    client.handshake_event.set()
    bridge = HiveMindMatrixBridge(client=client, matrix=matrix, **options)
    matrix.on_exhausted = bridge._stop.set
    bridge.identify()
    bridge.connect_hivemind()
    return bridge, client


def test_the_loop_long_polls_from_the_last_batch():
    matrix = FakeMatrix()
    matrix.syncs = [joined(ROOM, members=3, next_batch="b1"), joined(ROOM, next_batch="b2")]
    bridge, _ = looping_bridge(matrix)
    bridge.run_matrix()
    first, second, third = matrix.sync_calls[:3]
    assert first["since"] is None and first["timeout_ms"] == 0
    assert first["filter"] == bridge_module.INITIAL_SYNC_FILTER
    assert second["since"] == "b1" and second["timeout_ms"] == bridge_module.SYNC_TIMEOUT_MS
    assert second["filter"] == bridge_module.SYNC_FILTER
    assert third["since"] == "b2"


def test_a_refused_token_during_sync_ends_the_loop():
    matrix = FakeMatrix()
    matrix.syncs = [joined(ROOM, members=3), TokenRejected(401, "M_UNKNOWN_TOKEN")]
    bridge, _ = looping_bridge(matrix)
    with pytest.raises(TokenRejected):
        bridge.run_matrix()


def test_a_refused_token_while_replying_ends_the_loop():
    matrix = FakeMatrix()
    matrix.syncs = [joined(ROOM, members=3)]
    bridge, _ = looping_bridge(matrix)
    matrix.send_error = TokenRejected(401, "M_UNKNOWN_TOKEN")

    def reply_then_continue():
        speak(bridge, "x", ROOM)

    matrix.on_exhausted = reply_then_continue
    with pytest.raises(TokenRejected):
        bridge.run_matrix()


def test_a_failed_sync_is_retried(monkeypatch):
    monkeypatch.setattr(HiveMindMatrixBridge, "_backoff", staticmethod(lambda error, failures: 0))
    matrix = FakeMatrix()
    matrix.syncs = [MatrixError(502), joined(ROOM, members=3), MatrixError(429, "M_LIMIT_EXCEEDED")]
    bridge, _ = looping_bridge(matrix)
    bridge.run_matrix()
    assert len(matrix.sync_calls) >= 4
    assert ROOM in bridge.rooms


def test_the_backoff_follows_the_rate_limit_and_is_capped():
    backoff = HiveMindMatrixBridge._backoff
    assert backoff(MatrixError(429, "M_LIMIT_EXCEEDED", 1500), 1) == 1.5
    assert backoff(MatrixError(429, "M_LIMIT_EXCEEDED", 10 ** 9), 1) == bridge_module.MAX_SYNC_BACKOFF
    assert backoff(MatrixError(502), 1) == 2.0
    assert backoff(MatrixError(502), 50) == bridge_module.MAX_SYNC_BACKOFF
