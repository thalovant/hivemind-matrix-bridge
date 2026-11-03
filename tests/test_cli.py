"""The CLI resolves its settings from flags, the environment and secret files.

A secret on the command line is readable by every process on the host
(``ps``, ``/proc/<pid>/cmdline``), so a container must be able to start the
bridge with no secret in its arguments at all.
"""
import pytest

from hm_matrix_bridge.__main__ import resolve_options

BASE = {"MATRIX_HOMESERVER": "https://matrix.example.org", "MATRIX_ACCESS_TOKEN": "tok"}


def test_everything_can_come_from_the_environment():
    options = resolve_options([], {
        "MATRIX_HOMESERVER": "https://matrix.example.org/",
        "MATRIX_USER_ID": "@hive:example.org",
        "MATRIX_ACCESS_TOKEN": "tok",
        "MATRIX_ALLOWED_ROOMS": "!abc:example.org, !def:example.org !v12roomwithnoserver",
        "MATRIX_REQUIRE_MENTION": "false",
        "MATRIX_SESSION_SCOPE": "chat",
        "MATRIX_DM_POLICY": "allow",
        "BRIDGE_HEALTH_PORT": "8080",
        "HIVEMIND_ACCESS_KEY": "key",
        "HIVEMIND_PASSWORD": "pw",
        "HIVEMIND_HOST": "hub.local",
        "HIVEMIND_PORT": "6000",
        "HIVEMIND_SITE_ID": "kitchen",
        "HIVEMIND_SELF_SIGNED": "true",
        "HIVEMIND_LANG": "fr-fr",
    })
    assert options == {
        "homeserver": "https://matrix.example.org",
        "access_token": "tok",
        "user_id": "@hive:example.org",
        "key": "key",
        "password": "pw",
        "host": "ws://hub.local",
        "port": 6000,
        "self_signed": True,
        "lang": "fr-fr",
        "site_id": "kitchen",
        "allowed_rooms": ["!abc:example.org", "!def:example.org", "!v12roomwithnoserver"],
        "require_mention": False,
        "session_scope": "chat",
        "dm_policy": "allow",
        "health_port": 8080,
    }


def test_defaults_when_only_the_homeserver_and_token_are_given():
    options = resolve_options([], BASE)
    assert options["port"] == 5678
    assert options["site_id"] == "matrix"
    assert options["lang"] == "en-us"
    assert options["self_signed"] is False
    assert options["allowed_rooms"] is None
    assert options["require_mention"] is True
    assert options["session_scope"] == "person"
    assert options["dm_policy"] == "auto"
    assert options["health_port"] is None
    assert options["host"] is None
    assert options["user_id"] is None


def test_secrets_can_come_from_files(tmp_path):
    token = tmp_path / "token"
    token.write_text("tok-from-file\n", encoding="utf-8")
    key = tmp_path / "key"
    key.write_text("key-from-file", encoding="utf-8")
    password = tmp_path / "password"
    password.write_text("pw-from-file", encoding="utf-8")
    options = resolve_options([], {
        "MATRIX_HOMESERVER": "https://matrix.example.org",
        "MATRIX_ACCESS_TOKEN_FILE": str(token),
        "HIVEMIND_ACCESS_KEY_FILE": str(key),
        "HIVEMIND_PASSWORD_FILE": str(password),
    })
    assert options["access_token"] == "tok-from-file"
    assert options["key"] == "key-from-file"
    assert options["password"] == "pw-from-file"


def test_a_flag_wins_over_the_environment_and_the_environment_over_a_file(tmp_path):
    token = tmp_path / "token"
    token.write_text("from-file", encoding="utf-8")
    environ = {"MATRIX_HOMESERVER": "https://matrix.example.org",
               "MATRIX_ACCESS_TOKEN": "from-env", "MATRIX_ACCESS_TOKEN_FILE": str(token)}
    assert resolve_options(["--access-token", "from-flag"], environ)["access_token"] == "from-flag"
    assert resolve_options([], environ)["access_token"] == "from-env"
    del environ["MATRIX_ACCESS_TOKEN"]
    assert resolve_options([], environ)["access_token"] == "from-file"


def test_flags_cover_every_matrix_setting():
    options = resolve_options([
        "--homeserver", "https://hs.example.org", "--access-token", "t",
        "--user-id", "@bot:hs.example.org", "--allowed-room", "!a:hs.example.org",
        "--allowed-room", "!b:hs.example.org,!c:hs.example.org", "--no-require-mention",
        "--session-scope", "chat", "--dm-policy", "deny", "--health-port", "9000",
    ], {})
    assert options["allowed_rooms"] == ["!a:hs.example.org", "!b:hs.example.org", "!c:hs.example.org"]
    assert options["require_mention"] is False
    assert (options["session_scope"], options["dm_policy"], options["health_port"]) == ("chat", "deny", 9000)


def test_the_homeserver_and_the_token_are_required(capsys):
    with pytest.raises(SystemExit):
        resolve_options([], {"MATRIX_ACCESS_TOKEN": "tok"})
    assert "MATRIX_HOMESERVER" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        resolve_options([], {"MATRIX_HOMESERVER": "https://matrix.example.org"})
    assert "MATRIX_ACCESS_TOKEN" in capsys.readouterr().err


@pytest.mark.parametrize("homeserver", [
    "matrix.example.org", "ftp://matrix.example.org", "https://",
    # credentials in the URL replace the token's Authorization header
    "https://bot:secret@matrix.example.org",
    # a query or fragment would swallow every API path appended after it
    "https://matrix.example.org/?x=1", "https://matrix.example.org/#top",
])
def test_the_homeserver_must_be_a_url(homeserver):
    with pytest.raises(SystemExit):
        resolve_options([], {**BASE, "MATRIX_HOMESERVER": homeserver})


def test_an_unreadable_secret_file_names_the_variable(tmp_path):
    with pytest.raises(SystemExit) as stopped:
        resolve_options([], {"MATRIX_HOMESERVER": "https://matrix.example.org",
                             "MATRIX_ACCESS_TOKEN_FILE": str(tmp_path / "absent")})
    assert "MATRIX_ACCESS_TOKEN_FILE" in str(stopped.value)


@pytest.mark.parametrize("rooms", ["#general:example.org", "general", "!a:example.org;!b"])
def test_the_allow_list_takes_room_ids_only(rooms, capsys):
    with pytest.raises(SystemExit):
        resolve_options([], {**BASE, "MATRIX_ALLOWED_ROOMS": rooms})
    assert "MATRIX_ALLOWED_ROOMS" in capsys.readouterr().err


def test_an_alias_is_refused_with_a_hint(capsys):
    with pytest.raises(SystemExit):
        resolve_options([], {**BASE, "MATRIX_ALLOWED_ROOMS": "#general:example.org"})
    assert "alias" in capsys.readouterr().err


def test_a_malformed_user_id_is_refused():
    with pytest.raises(SystemExit):
        resolve_options([], {**BASE, "MATRIX_USER_ID": "hive"})


@pytest.mark.parametrize("variable,value", [
    ("MATRIX_SESSION_SCOPE", "room"),
    ("MATRIX_DM_POLICY", "sometimes"),
    ("MATRIX_REQUIRE_MENTION", "maybe"),
    ("HIVEMIND_PORT", "http"),
    ("BRIDGE_HEALTH_PORT", "eighty"),
])
def test_an_impossible_value_is_a_usage_error(variable, value):
    with pytest.raises(SystemExit):
        resolve_options([], {**BASE, variable: value})


def test_mention_gating_is_on_by_default_and_can_be_turned_off():
    assert resolve_options([], BASE)["require_mention"] is True
    assert resolve_options(["--no-require-mention"], BASE)["require_mention"] is False
    assert resolve_options([], {**BASE, "MATRIX_REQUIRE_MENTION": "false"})["require_mention"] is False
    assert resolve_options([], {**BASE, "MATRIX_REQUIRE_MENTION": "true"})["require_mention"] is True



def test_sigterm_stops_the_bridge_the_way_ctrl_c_does(monkeypatch):
    """Kubernetes stops a pod with SIGTERM; Python's default ends it without
    ``stop()``, so queued replies and the hub session would just vanish."""
    import signal

    import hm_matrix_bridge.__main__ as cli

    installed = {}
    monkeypatch.setattr(cli.signal, "signal", lambda signum, handler: installed.update({signum: handler}))
    stopped = []

    def run(**options):
        stopped.append("ran")
        raise KeyboardInterrupt  # what the handler raises in the main thread

    monkeypatch.setattr(cli, "connect_matrix_to_hivemind", run)
    cli.main(["--homeserver", "https://matrix.example.org", "--access-token", "tok"])
    assert stopped == ["ran"]
    with pytest.raises(KeyboardInterrupt):
        installed[signal.SIGTERM](signal.SIGTERM, None)


@pytest.mark.parametrize("homeserver, warned", [
    ("http://matrix.example.org", True),
    ("http://10.0.0.5:8008", True),
    ("http://127.0.0.1:8008", False),
    ("http://localhost:8008", False),
    ("http://[::1]:8008", False),
    ("https://matrix.example.org", False),
])
def test_plain_http_to_another_host_is_warned_about(monkeypatch, homeserver, warned):
    import hm_matrix_bridge.__main__ as cli

    lines = []
    monkeypatch.setattr(cli.LOG, "warning", lambda message, *a, **k: lines.append(message))
    resolve_options([], {**BASE, "MATRIX_HOMESERVER": homeserver})
    assert any("http://" in line and "token" in line for line in lines) is warned



def test_sigterm_during_the_sync_runs_stop_closes_the_hub_and_sends_queued_replies(monkeypatch):
    """The real ``start()``: the handler's KeyboardInterrupt arrives in the sync
    long-poll; ``start()``'s ``finally: stop()`` must close the hub session and
    post what is queued before ``main`` returns."""
    import threading
    from unittest.mock import MagicMock

    from ovos_bus_client.message import Message

    import hm_matrix_bridge as bridge_module
    import hm_matrix_bridge.__main__ as cli
    from fakes import ROOM, FakeMatrix, joined

    monkeypatch.setattr(cli.signal, "signal", lambda signum, handler: None)
    hub = MagicMock()
    hub.handshake_event = threading.Event()
    hub.connect.side_effect = lambda **kwargs: hub.handshake_event.set()

    class Interrupted(FakeMatrix):
        def sync(self, since=None, timeout_ms=0, filter_json=None):
            self.sync_calls.append(since)
            if len(self.sync_calls) == 1:
                return joined(ROOM, members=3)
            # the hub answered just before the pod was told to stop
            speak = next(call.args[1] for call in hub.on_mycroft.call_args_list
                         if call.args[0] == "speak")
            speak(Message("speak", {"utterance": "it is noon"}, {"room_id": ROOM}))
            raise KeyboardInterrupt  # what _stop_on_sigterm raises in the main thread

    matrix = Interrupted()
    monkeypatch.setattr(bridge_module, "HiveMessageBusClient", lambda **kwargs: hub)
    monkeypatch.setattr(bridge_module, "MatrixClient", lambda *args, **kwargs: matrix)
    cli.main(["--homeserver", "https://matrix.example.org", "--access-token", "tok"])
    hub.close.assert_called()
    assert [content["body"] for _, content in matrix.sent] == ["it is noon"]
