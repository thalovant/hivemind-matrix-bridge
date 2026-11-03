"""Nothing anyone wrote or was told ends up in the bridge's log, at any level.

A debug log is shipped and read far more widely than the room it came from.
Neither does the access token.
"""
from unittest.mock import MagicMock

from ovos_bus_client.message import Message

import hm_matrix_bridge as bridge_module
import hm_matrix_bridge.matrix as matrix_module
from fakes import ALICE, BOT_NAME, DM, OTHER, ROOM, encryption, joined, make_bridge, text
from hm_matrix_bridge.matrix import MatrixClient, MatrixError

SECRET_QUESTION = "is my biopsy result back"
SECRET_ANSWER = "Your appointment is on Tuesday"
TOKEN = "syt_c2VjcmV0_TOKENVALUE"


def capture(monkeypatch):
    lines = []
    log = MagicMock()
    for level in ("debug", "info", "warning", "error", "exception"):
        getattr(log, level).side_effect = lambda *a, **k: lines.append(" ".join(map(str, a)))
    monkeypatch.setattr(bridge_module, "LOG", log)
    monkeypatch.setattr(matrix_module, "LOG", log)
    return lines


def test_neither_questions_nor_answers_are_logged(monkeypatch):
    lines = capture(monkeypatch)
    bridge, matrix, _ = make_bridge({ROOM: 3, DM: 2, OTHER: 3}, allowed_rooms=[ROOM, DM])
    # answered, ignored (no mention), refused (not allowed), and encrypted
    bridge.process_sync(joined(DM, text(ALICE, SECRET_QUESTION)))
    bridge.process_sync(joined(ROOM, text(ALICE, SECRET_QUESTION)))
    bridge.process_sync(joined(OTHER, text(ALICE, f"{BOT_NAME} {SECRET_QUESTION}")))
    bridge.handle_speak(Message("speak", {"utterance": SECRET_ANSWER}, {"room_id": DM}))
    matrix.send_error = MatrixError(500, "M_UNKNOWN")
    bridge.handle_speak(Message("speak", {"utterance": SECRET_ANSWER}, {"room_id": DM}))
    bridge.handle_speak(Message("speak", {"utterance": SECRET_ANSWER}, {"room_id": OTHER}))
    bridge.process_sync(joined(ROOM, encryption(), text(ALICE, f"{BOT_NAME} {SECRET_QUESTION}")))

    assert lines, "the bridge logged nothing; the test would prove nothing"
    joined_lines = "\n".join(lines)
    assert SECRET_QUESTION not in joined_lines
    assert SECRET_ANSWER not in joined_lines


def test_the_access_token_is_not_in_the_client_repr_or_its_errors():
    client = MatrixClient("https://matrix.example.org", TOKEN)
    assert TOKEN not in repr(client)
    assert TOKEN not in str(MatrixError(401, "M_UNKNOWN_TOKEN"))
