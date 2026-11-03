"""Hub sessions: one per person in each room (default) or one per room.

The session id is what the hub keeps a conversation, and its per-person rate
limit, by.
"""
from fakes import ALICE, BOB, BOT_NAME, DM, ROOM, joined, make_bridge, text, utterances


def sessions(bridge, client, room_id, *senders):
    for sender in senders:
        bridge.process_sync(joined(room_id, text(sender, f"{BOT_NAME} hello")))
    return [context["session"]["session_id"] for _, context in utterances(client)]


def test_by_default_each_person_in_a_room_has_their_own_session():
    bridge, _, client = make_bridge({ROOM: 3})
    assert bridge.session_scope == "person"
    assert sessions(bridge, client, ROOM, ALICE, BOB, ALICE) == [
        f"matrix-{ROOM}-{ALICE}", f"matrix-{ROOM}-{BOB}", f"matrix-{ROOM}-{ALICE}"]


def test_chat_scope_shares_one_session_per_room():
    bridge, _, client = make_bridge({ROOM: 3}, session_scope="chat")
    assert sessions(bridge, client, ROOM, ALICE, BOB) == [f"matrix-{ROOM}"] * 2


def test_the_same_person_in_two_rooms_has_two_sessions():
    bridge, _, client = make_bridge({ROOM: 3, DM: 2})
    bridge.process_sync(joined(ROOM, text(ALICE, f"{BOT_NAME} hi")))
    bridge.process_sync(joined(DM, text(ALICE, "hi")))
    ids = [context["session"]["session_id"] for _, context in utterances(client)]
    assert ids == [f"matrix-{ROOM}-{ALICE}", f"matrix-{DM}-{ALICE}"]
