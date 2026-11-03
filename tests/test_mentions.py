"""In a room with other people, only what is addressed to the bot reaches the hub.

Addressed means a pill (``m.mentions``), the bot's user id or display name in
the text, or a reply to one of the bot's messages. The mention is removed
before the text is forwarded. A direct message needs none of it.
"""
import pytest

from fakes import ALICE, BOB, BOT, BOT_NAME, DM, ROOM, joined, make_bridge, member, text, utterances


def forwarded(bridge, client, room_id, event):
    before = len(client.emit.call_args_list)
    bridge.process_sync(joined(room_id, event))
    sent = utterances(client)[before:]
    return sent[0][0] if sent else None


@pytest.fixture
def group():
    bridge, matrix, client = make_bridge({ROOM: 3, DM: 2})
    return bridge, matrix, client


def test_a_message_that_does_not_mention_the_bot_is_not_forwarded(group):
    bridge, _, client = group
    assert forwarded(bridge, client, ROOM, text(ALICE, "lunch anyone?")) is None


def test_a_pill_is_a_mention_and_its_text_is_removed(group):
    bridge, _, client = group
    event = text(ALICE, f"{BOT_NAME}: what time is it", **{"m.mentions": {"user_ids": [BOT]}})
    assert forwarded(bridge, client, ROOM, event) == "what time is it"


def test_a_pill_whose_text_is_not_the_display_name_still_counts(group):
    bridge, _, client = group
    event = text(ALICE, "assistant, what time is it", **{"m.mentions": {"user_ids": [BOT]}})
    assert forwarded(bridge, client, ROOM, event) == "assistant, what time is it"


def test_a_pill_for_someone_else_is_not_a_mention(group):
    bridge, _, client = group
    event = text(ALICE, "Bob: lunch?", **{"m.mentions": {"user_ids": [BOB]}})
    assert forwarded(bridge, client, ROOM, event) is None


@pytest.mark.parametrize("body", [
    f"{BOT_NAME}: what time is it",
    f"{BOT_NAME.lower()}, what time is it",
    f"what time is it {BOT_NAME}",
    f"{BOT} what time is it",
    f"{BOT}: what time is it",
    f"[{BOT_NAME}](https://matrix.to/#/{BOT}): what time is it",
    f"[{BOT_NAME}](https://matrix.to/#/%40hive%3Aexample.org) what time is it",
])
def test_the_display_name_or_user_id_in_the_text_is_a_mention_and_is_removed(group, body):
    bridge, _, client = group
    assert forwarded(bridge, client, ROOM, text(ALICE, body)) == "what time is it"


def test_a_word_that_merely_contains_the_name_is_not_a_mention(group):
    bridge, _, client = group
    assert forwarded(bridge, client, ROOM, text(ALICE, f"{BOT_NAME}s are great")) is None


def test_the_display_name_the_bot_has_in_this_room_is_a_mention_too(group):
    bridge, _, client = group
    bridge.process_sync(joined(ROOM, member(BOT, displayname="Kitchen Helper")))
    assert forwarded(bridge, client, ROOM, text(ALICE, "Kitchen Helper: timer 5 minutes")) == "timer 5 minutes"


def test_a_mention_with_nothing_else_is_not_forwarded(group):
    bridge, _, client = group
    assert forwarded(bridge, client, ROOM, text(ALICE, f"{BOT_NAME}:")) is None


def test_a_direct_message_needs_no_mention(group):
    bridge, _, client = group
    assert forwarded(bridge, client, DM, text(ALICE, "what time is it")) == "what time is it"


def test_a_direct_message_still_loses_the_mention(group):
    bridge, _, client = group
    assert forwarded(bridge, client, DM, text(ALICE, f"{BOT_NAME} what time is it")) == "what time is it"


def reply(sender, body, to_event, fallback=None):
    full = f"{fallback}\n\n{body}" if fallback else body
    return text(sender, full, **{"m.relates_to": {"m.in_reply_to": {"event_id": to_event}}})


def test_a_reply_to_the_bot_is_addressed_to_it_and_loses_the_quoted_fallback(group):
    bridge, _, client = group
    bridge.process_sync(joined(ROOM, text(BOT, "It is noon", event_id="$bot1")))
    event = reply(ALICE, "and in Tokyo?", "$bot1", fallback=f"> <{BOT}> It is noon")
    assert forwarded(bridge, client, ROOM, event) == "and in Tokyo?"


def test_a_reply_to_someone_else_is_not(group):
    bridge, _, client = group
    bridge.process_sync(joined(ROOM, text(BOB, "anyone?", event_id="$bob1")))
    assert forwarded(bridge, client, ROOM, reply(ALICE, "me", "$bob1")) is None


def test_a_reply_to_an_older_bot_message_is_looked_up_once(group):
    bridge, matrix, client = group
    matrix.events[(ROOM, "$old")] = {"event_id": "$old", "sender": BOT}
    assert forwarded(bridge, client, ROOM, reply(ALICE, "first", "$old")) == "first"
    assert forwarded(bridge, client, ROOM, reply(ALICE, "second", "$old")) == "second"
    assert matrix.event_calls == [(ROOM, "$old")]


def test_a_reply_target_that_cannot_be_read_is_not_the_bot(group):
    bridge, _, client = group
    assert forwarded(bridge, client, ROOM, reply(ALICE, "hm", "$missing")) is None


def test_a_thread_fallback_reply_is_not_a_reply_to_the_bot(group):
    bridge, _, client = group
    bridge.process_sync(joined(ROOM, text(BOT, "answer", event_id="$bot2")))
    event = text(ALICE, "chatting in the thread", **{"m.relates_to": {
        "rel_type": "m.thread", "event_id": "$root", "is_falling_back": True,
        "m.in_reply_to": {"event_id": "$bot2"}}})
    assert forwarded(bridge, client, ROOM, event) is None


def test_the_replies_the_bot_sends_count_as_its_own():
    from ovos_bus_client.message import Message

    bridge, matrix, client = make_bridge({ROOM: 3})
    bridge.handle_speak(Message("speak", {"utterance": "It is noon"}, {"room_id": ROOM}))
    sent_id = next(key[1] for key, mine in bridge._events.items() if mine)
    assert forwarded(bridge, client, ROOM, reply(ALICE, "thanks, and tomorrow?", sent_id)) \
        == "thanks, and tomorrow?"
    assert matrix.event_calls == []


def test_without_require_mention_every_message_is_forwarded():
    bridge, _, client = make_bridge({ROOM: 3}, require_mention=False)
    assert forwarded(bridge, client, ROOM, text(ALICE, "lunch anyone?")) == "lunch anyone?"
