"""Liveness and readiness, as a container platform asks for them."""
import urllib.error
import urllib.request

import pytest

from fakes import ROOM, FakeMatrix, joined, make_bridge
from hm_matrix_bridge.health import HealthServer
from hm_matrix_bridge.matrix import MatrixError


def get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as answer:
            return answer.status
    except urllib.error.HTTPError as error:
        return error.code


@pytest.fixture
def served():
    state = {"ready": False}
    server = HealthServer(0, lambda: state["ready"], host="127.0.0.1")
    server.start()
    yield server, state
    server.stop()


def test_liveness_answers_while_not_ready(served):
    server, _ = served
    assert get(server.port, "/healthz") == 200
    assert get(server.port, "/readyz") == 503


def test_readiness_follows_the_bridge(served):
    server, state = served
    state["ready"] = True
    assert get(server.port, "/readyz") == 200
    state["ready"] = False
    assert get(server.port, "/readyz") == 503


def test_a_failing_check_is_not_ready_not_a_crash():
    def broken():
        raise RuntimeError("boom")

    server = HealthServer(0, broken, host="127.0.0.1")
    server.start()
    try:
        assert get(server.port, "/readyz") == 503
        assert get(server.port, "/healthz") == 200
    finally:
        server.stop()


def test_anything_else_is_not_found(served):
    server, _ = served
    assert get(server.port, "/") == 404


def test_ready_needs_a_live_hub_session_and_the_initial_sync():
    matrix = FakeMatrix()
    bridge, _, client = make_bridge({}, matrix=matrix)
    assert not bridge.ready(), "connected to the hub, but Matrix has not synced"

    matrix.syncs = [joined(ROOM, members=3)]
    matrix.on_exhausted = bridge._stop.set
    bridge._stop.clear()
    bridge._sync_until_done(initial=True)
    bridge._synced.set()
    assert bridge.ready()

    client.handshake_event.clear()  # the hub connection dropped
    assert not bridge.ready()
    client.handshake_event.set()
    assert bridge.ready()


def test_a_failing_sync_makes_the_bridge_unready_until_one_succeeds(monkeypatch):
    from hm_matrix_bridge import HiveMindMatrixBridge

    monkeypatch.setattr(HiveMindMatrixBridge, "_backoff", staticmethod(lambda error, failures: 0))
    matrix = FakeMatrix()
    bridge, _, _ = make_bridge({}, matrix=matrix)
    bridge._synced.set()
    bridge._sync_ok.set()
    assert bridge.ready()
    failures = iter([MatrixError(502)])
    seen_while_retrying = []
    real_sync = matrix.sync

    def sync(**kwargs):
        for error in failures:
            raise error
        seen_while_retrying.append(bridge.ready())
        return real_sync(**kwargs)

    matrix.sync = sync
    bridge._sync_until_done(initial=False)
    assert seen_while_retrying == [False], "readiness must drop while the homeserver fails"
    assert bridge.ready()


def test_ready_is_false_before_the_hub_handshake():
    bridge, _, _ = make_bridge({ROOM: 3}, connect=False)
    bridge._synced.set()
    bridge._sync_ok.set()
    assert not bridge.ready()
