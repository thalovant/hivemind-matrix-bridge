"""The Matrix client, against a scripted homeserver over real HTTP."""
import json

import pytest

import hm_matrix_bridge.matrix as matrix_module
from homeserver import Homeserver
from hm_matrix_bridge.matrix import MatrixClient, MatrixError, TokenRejected

TOKEN = "syt_aGl2ZQ_secretvalue"
ROOM = "!room:example.org"


@pytest.fixture
def hs():
    with Homeserver() as server:
        yield server


def client_for(hs, **options):
    waits = []
    options.setdefault("min_send_interval", 0)
    client = MatrixClient(hs.url + "/", TOKEN, sleep=waits.append, **options)
    return client, waits


def test_the_token_travels_only_in_the_authorization_header(hs):
    hs.route("GET", "/account/whoami", (200, {"user_id": "@hive:example.org"}))
    hs.route("GET", "/sync", (200, {"next_batch": "b"}))
    client, _ = client_for(hs)
    assert client.whoami() == {"user_id": "@hive:example.org"}
    client.sync(since="a", timeout_ms=0, filter_json="{}")
    for request in hs.requests:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in request.raw_path
        assert TOKEN not in json.dumps(request.query)


def test_a_401_is_a_rejected_token(hs):
    hs.route("GET", "/account/whoami", (401, {"errcode": "M_UNKNOWN_TOKEN", "error": "Invalid"}))
    client, _ = client_for(hs)
    with pytest.raises(TokenRejected) as rejected:
        client.whoami()
    assert rejected.value.status == 401
    assert rejected.value.errcode == "M_UNKNOWN_TOKEN"
    assert "Invalid" not in str(rejected.value), "the server's free text is not carried"


@pytest.mark.parametrize("status,errcode", [(403, "M_USER_DEACTIVATED"), (401, None),
                                            (401, "M_USER_LOCKED")])
def test_every_way_of_refusing_the_token_is_a_rejected_token(hs, status, errcode):
    hs.route("GET", "/account/whoami", (status, {"errcode": errcode} if errcode else {}))
    client, _ = client_for(hs)
    with pytest.raises(TokenRejected):
        client.whoami()


def test_other_errors_are_not_a_rejected_token(hs):
    hs.route("POST", r"/rooms/[^/]+/join", (403, {"errcode": "M_FORBIDDEN"}))
    client, _ = client_for(hs)
    with pytest.raises(MatrixError) as failed:
        client.join(ROOM)
    assert not isinstance(failed.value, TokenRejected)
    assert failed.value.describe() == "HTTP 403 M_FORBIDDEN"


def test_room_and_event_ids_are_escaped_in_the_path(hs):
    hs.route("GET", r"/rooms/.+/event/.+", (200, {"sender": "@a:example.org"}))
    hs.route("GET", r"/rooms/.+/joined_members", (200, {"joined": {}}))
    client, _ = client_for(hs)
    client.event(ROOM, "$ev/ent:example.org")
    client.joined_members(ROOM)
    paths = [request.raw_path for request in hs.requests]
    assert paths[0].endswith("/rooms/%21room%3Aexample.org/event/%24ev%2Fent%3Aexample.org")
    assert paths[1].endswith("/rooms/%21room%3Aexample.org/joined_members")


def test_sync_sends_since_timeout_and_filter(hs):
    hs.route("GET", "/sync", (200, {"next_batch": "b2"}))
    client, _ = client_for(hs)
    assert client.sync(since="b1", timeout_ms=30000, filter_json='{"room":{}}') == {"next_batch": "b2"}
    query = hs.requests[0].query
    assert query == {"since": ["b1"], "timeout": ["30000"], "filter": ['{"room":{}}']}


def test_a_missing_display_name_is_none(hs):
    hs.route("GET", r"/profile/.+/displayname", (404, {"errcode": "M_NOT_FOUND"}))
    client, _ = client_for(hs)
    assert client.display_name("@hive:example.org") is None
    hs.route("GET", r"/profile/.+/displayname", (200, {"displayname": "HiveBot"}))
    assert client.display_name("@hive:example.org") == "HiveBot"


def test_a_message_is_put_with_a_transaction_id(hs):
    hs.route("PUT", r"/rooms/[^/]+/send/m\.room\.message/[^/]+", (200, {"event_id": "$sent"}))
    client, _ = client_for(hs)
    assert client.send_message(ROOM, {"msgtype": "m.text", "body": "hi"}) == "$sent"
    assert client.send_message(ROOM, {"msgtype": "m.text", "body": "again"}) == "$sent"
    first, second = hs.requests
    assert first.body == {"msgtype": "m.text", "body": "hi"}
    assert first.raw_path.rsplit("/", 1)[1] != second.raw_path.rsplit("/", 1)[1]


def test_a_rate_limited_send_is_retried_with_the_same_transaction_after_the_wait(hs):
    answers = [(429, {"errcode": "M_LIMIT_EXCEEDED", "retry_after_ms": 1500}, {}),
               (200, {"event_id": "$sent"}, {})]
    hs.route("PUT", r"/rooms/[^/]+/send/m\.room\.message/[^/]+", lambda request: answers.pop(0))
    client, waits = client_for(hs)
    assert client.send_message(ROOM, {"msgtype": "m.text", "body": "hi"}) == "$sent"
    assert waits == [1.5]
    first, second = hs.requests
    assert first.raw_path == second.raw_path, "a retry must not post the message twice"


def test_the_retry_after_header_is_honoured(hs):
    answers = [(429, {"errcode": "M_LIMIT_EXCEEDED"}, {"Retry-After": "2"}),
               (200, {"event_id": "$sent"}, {})]
    hs.route("PUT", r"/rooms/.+", lambda request: answers.pop(0))
    client, waits = client_for(hs)
    client.send_message(ROOM, {"msgtype": "m.text", "body": "hi"})
    assert waits == [2.0]


def test_a_send_that_stays_rate_limited_is_given_up(hs):
    hs.route("PUT", r"/rooms/.+", (429, {"errcode": "M_LIMIT_EXCEEDED", "retry_after_ms": 10}))
    client, waits = client_for(hs)
    with pytest.raises(MatrixError) as failed:
        client.send_message(ROOM, {"msgtype": "m.text", "body": "hi"})
    assert failed.value.rate_limited
    assert len(hs.requests) == matrix_module.MAX_SEND_ATTEMPTS
    assert len(waits) == matrix_module.MAX_SEND_ATTEMPTS - 1


def test_a_send_asked_to_wait_too_long_is_dropped_at_once(hs):
    hs.route("PUT", r"/rooms/.+", (429, {"errcode": "M_LIMIT_EXCEEDED",
                                          "retry_after_ms": 3_600_000}))
    client, waits = client_for(hs)
    with pytest.raises(MatrixError):
        client.send_message(ROOM, {"msgtype": "m.text", "body": "hi"})
    assert len(hs.requests) == 1 and waits == []


def test_sends_are_paced(hs):
    hs.route("PUT", r"/rooms/.+", (200, {"event_id": "$sent"}))
    client, waits = client_for(hs, min_send_interval=5)
    client.send_message(ROOM, {"msgtype": "m.text", "body": "one"})
    client.send_message(ROOM, {"msgtype": "m.text", "body": "two"})
    assert len(waits) == 1, "only the second send has an interval to wait out"
    assert 4 < waits[0] <= 5


def test_an_answer_that_is_not_an_object_reads_as_empty(hs):
    hs.route("GET", "/account/whoami", lambda request: (200, "not an object", {}))
    client, _ = client_for(hs)
    assert client.whoami() == {}


def test_a_redirect_is_not_followed(hs):
    """The homeserver URL is whatever the customer typed; a redirect could
    point the bridge's requests (a reply's text in a PUT body) anywhere."""
    with Homeserver() as elsewhere:
        elsewhere.route("GET", "/account/whoami", (200, {"user_id": "@hive:example.org"}))
        elsewhere.route("PUT", r"/rooms/.+", (200, {"event_id": "$x"}))
        target = f"{elsewhere.url}/_matrix/client/v3"
        hs.route("GET", "/account/whoami", (302, {}, {"Location": f"{target}/account/whoami"}))
        hs.route("PUT", r"/rooms/.+", (307, {}, {"Location": f"{target}/rooms/x"}))
        client, _ = client_for(hs)
        with pytest.raises(MatrixError) as refused:
            client.whoami()
        assert (refused.value.status, refused.value.errcode) == (302, "REDIRECT")
        assert not isinstance(refused.value, TokenRejected)
        with pytest.raises(MatrixError):
            client.send_message(ROOM, {"msgtype": "m.notice", "body": "private answer"})
        assert elsewhere.requests == []


@pytest.mark.parametrize("answer", [
    (429, {"errcode": "M_LIMIT_EXCEEDED"}, {"Retry-After": "inf"}),
    (429, {"errcode": "M_LIMIT_EXCEEDED"}, {"Retry-After": "nan"}),
    (429, {"errcode": "M_LIMIT_EXCEEDED"}, {"Retry-After": "-5"}),
    (429, {"errcode": "M_LIMIT_EXCEEDED", "retry_after_ms": -5000}, {}),
    (429, {"errcode": "M_LIMIT_EXCEEDED"}, {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}),
])
def test_an_odd_retry_after_never_raises_or_waits_a_negative_time(hs, answer):
    hs.route("PUT", r"/rooms/.+", answer)
    client, waits = client_for(hs)
    with pytest.raises(MatrixError) as refused:
        client.send_message(ROOM, {"msgtype": "m.notice", "body": "hi"})
    assert refused.value.rate_limited
    assert refused.value.retry_after_ms is None or refused.value.retry_after_ms >= 0
    assert all(wait >= 0 for wait in waits)
