"""CLI entry point for the HiveMind <-> Matrix bridge.

Every option can come from the environment, so a container never has to put
a secret on its command line, where any process on the host can read it.
Secrets additionally accept a ``<VARIABLE>_FILE`` variable naming a file that
holds the value, the convention for secrets mounted into a container. A flag
wins over its environment variable, and the variable over its file.

HiveMind identity (key/password/host/port) falls back to what
``hivemind-client set-identity`` stored when not given anywhere.

When the homeserver refuses the access token, the bridge writes
``token_rejected`` to ``/dev/termination-log`` (Kubernetes' termination
message) and exits with status 3: retrying cannot fix a bad token.
"""
import argparse
import os
import re
from typing import List, Mapping, Optional, Sequence
from urllib.parse import urlparse

from ovos_utils.log import LOG

from hm_matrix_bridge import DM_POLICIES, SESSION_SCOPES, HiveMindMatrixBridge
from hm_matrix_bridge.matrix import MatrixError, TokenRejected

#: option -> environment variable it falls back to.
ENVIRONMENT = {
    "homeserver": "MATRIX_HOMESERVER",
    "user_id": "MATRIX_USER_ID",
    "access_token": "MATRIX_ACCESS_TOKEN",
    "allowed_rooms": "MATRIX_ALLOWED_ROOMS",
    "require_mention": "MATRIX_REQUIRE_MENTION",
    "session_scope": "MATRIX_SESSION_SCOPE",
    "dm_policy": "MATRIX_DM_POLICY",
    "health_port": "BRIDGE_HEALTH_PORT",
    "key": "HIVEMIND_ACCESS_KEY",
    "password": "HIVEMIND_PASSWORD",
    "host": "HIVEMIND_HOST",
    "port": "HIVEMIND_PORT",
    "site_id": "HIVEMIND_SITE_ID",
    "self_signed": "HIVEMIND_SELF_SIGNED",
    "lang": "HIVEMIND_LANG",
}
#: options whose value may be read from the file named by ``<VARIABLE>_FILE``.
SECRETS = ("access_token", "key", "password")

#: Where Kubernetes reads a container's termination message.
TERMINATION_LOG = "/dev/termination-log"
#: The termination message for a refused access token.
TOKEN_REJECTED = "token_rejected"
#: Exit status for a refused access token.
EXIT_TOKEN_REJECTED = 3

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
#: A Matrix server name: a hostname, an IPv4 or a bracketed IPv6 literal, and
#: an optional port.
_SERVER_NAME = r"(?:[A-Za-z0-9.\-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?"
#: ``!opaque:server`` (room versions 1-11) or ``!opaque`` (room version 12).
_ROOM_ID = re.compile(rf"![^\s,:]+(?::{_SERVER_NAME})?")
_USER_ID = re.compile(rf"@[^\s:]+:{_SERVER_NAME}")


def build_parser() -> argparse.ArgumentParser:
    """The command line; every option also has an environment variable."""
    parser = argparse.ArgumentParser(
        prog="hivemind-matrix-bridge",
        description="Bridge a Matrix bot account to a HiveMind node",
        epilog="Every option falls back to an environment variable "
               "(MATRIX_HOMESERVER, MATRIX_USER_ID, MATRIX_ACCESS_TOKEN, "
               "MATRIX_ALLOWED_ROOMS, MATRIX_REQUIRE_MENTION, MATRIX_SESSION_SCOPE, "
               "MATRIX_DM_POLICY, BRIDGE_HEALTH_PORT, HIVEMIND_ACCESS_KEY, "
               "HIVEMIND_PASSWORD, HIVEMIND_HOST, HIVEMIND_PORT, HIVEMIND_SITE_ID, "
               "HIVEMIND_SELF_SIGNED, HIVEMIND_LANG). The access token, access key "
               "and password also accept <VARIABLE>_FILE naming a file that holds "
               "the value.")
    # Matrix
    parser.add_argument("--homeserver", default=None,
                        help="client API base URL, e.g. https://matrix.example.org")
    parser.add_argument("--user-id", default=None,
                        help="the bot's @user:server; when given, the token must "
                             "belong to it (default: the token's own account)")
    parser.add_argument("--access-token", default=None,
                        help="the bot account's access token (prefer "
                             "MATRIX_ACCESS_TOKEN_FILE)")
    parser.add_argument("--allowed-room", dest="allowed_rooms", action="append",
                        default=None,
                        help="room id (!opaque:server) the bridge answers in and "
                             "accepts invites to (repeatable); default: every room "
                             "the bot is in, and every invite")
    parser.add_argument("--no-require-mention", dest="require_mention",
                        action="store_false", default=None,
                        help="in a room with more than two members, answer every "
                             "message, not only those that mention the bot or "
                             "reply to it")
    parser.add_argument("--session-scope", choices=SESSION_SCOPES, default=None,
                        help="one hub conversation per person in each room "
                             "(default) or per room")
    parser.add_argument("--dm-policy", choices=DM_POLICIES, default=None,
                        help="two-member rooms not on the allow-list: deny, allow, "
                             "or auto (default: deny when rooms are listed, allow "
                             "otherwise)")
    parser.add_argument("--health-port", type=int, default=None,
                        help="serve /healthz and /readyz on this port (default: off)")
    # HiveMind
    parser.add_argument("--access-key", dest="key", default=None,
                        help="HiveMind access key (default: from identity file)")
    parser.add_argument("--password", default=None,
                        help="HiveMind password (default: from identity file)")
    parser.add_argument("--host", default=None,
                        help="HiveMind host, e.g. ws://127.0.0.1 (default: from identity file)")
    parser.add_argument("--port", type=int, default=None,
                        help="HiveMind port (default: 5678)")
    parser.add_argument("--site-id", default=None,
                        help="this bridge's HiveMind site id (default: matrix)")
    parser.add_argument("--self-signed", action="store_true", default=None,
                        help="accept self-signed SSL certificates")
    parser.add_argument("--lang", default=None, help="utterance language (default: en-us)")
    return parser


def _read_secret_file(path: str, variable: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError as error:
        raise SystemExit(f"{variable}: cannot read {path}: {error.strerror}") from None


def _from_environment(option: str, environ: Mapping[str, str]) -> Optional[str]:
    variable = ENVIRONMENT[option]
    value = environ.get(variable)
    if value:
        return value
    if option in SECRETS:
        path = environ.get(f"{variable}_FILE")
        if path:
            return _read_secret_file(path, f"{variable}_FILE")
    return None


def _room_ids(values: Sequence[str]) -> List[str]:
    """Room ids from flags or one variable, separated by commas or spaces."""
    rooms: List[str] = []
    for value in values:
        for item in value.replace(",", " ").split():
            if item.startswith("#"):
                raise ValueError(f"{item!r} is a room alias; give the room id "
                                 "(!opaque:server, Room settings > Advanced)")
            if not _ROOM_ID.fullmatch(item):
                raise ValueError(f"{item!r} is not a room id (!opaque:server)")
            if item not in rooms:
                rooms.append(item)
    return rooms


def resolve_options(argv: Optional[Sequence[str]] = None,
                    environ: Optional[Mapping[str, str]] = None) -> dict:
    """The bridge's settings: flags first, then the environment, then defaults.

    Raises ``SystemExit`` with a usage message when the homeserver or the
    access token is missing, or when a setting has an impossible value.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ

    def pick(option):
        given = getattr(args, option)
        return given if given is not None else _from_environment(option, environ)

    def flag(option, default):
        given = getattr(args, option)
        if given is not None:
            return given
        setting = environ.get(ENVIRONMENT[option])
        if not setting:
            return default
        if setting.strip().lower() in _TRUE:
            return True
        if setting.strip().lower() in _FALSE:
            return False
        parser.error(f"{ENVIRONMENT[option]} must be true or false, not {setting!r}")

    homeserver = pick("homeserver")
    if not homeserver:
        parser.error("a homeserver is required: --homeserver or MATRIX_HOMESERVER "
                     "(the client API URL, e.g. https://matrix.example.org)")
    parsed = urlparse(homeserver.strip())
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        parser.error("MATRIX_HOMESERVER must be an https:// URL, e.g. "
                     "https://matrix.example.org")
    homeserver = homeserver.strip().rstrip("/")

    access_token = pick("access_token")
    if not access_token:
        parser.error("an access token is required: --access-token, "
                     "MATRIX_ACCESS_TOKEN or MATRIX_ACCESS_TOKEN_FILE")

    user_id = pick("user_id")
    if user_id:
        user_id = user_id.strip()
        if not _USER_ID.fullmatch(user_id):
            parser.error("MATRIX_USER_ID must look like @bot:example.org")

    listed = args.allowed_rooms
    if listed is None:
        variable = _from_environment("allowed_rooms", environ)
        listed = [variable] if variable else None
    allowed_rooms = None
    if listed is not None:
        try:
            allowed_rooms = _room_ids(listed) or None
        except ValueError as error:
            parser.error(f"MATRIX_ALLOWED_ROOMS: {error}")

    session_scope = pick("session_scope") or "person"
    if session_scope not in SESSION_SCOPES:
        parser.error("MATRIX_SESSION_SCOPE must be 'person' or 'chat'")

    dm_policy = pick("dm_policy") or "auto"
    if dm_policy not in DM_POLICIES:
        parser.error("MATRIX_DM_POLICY must be 'auto', 'allow' or 'deny'")

    port = pick("port")
    try:
        port = int(port) if port is not None else 5678
    except ValueError:
        parser.error("HIVEMIND_PORT must be a number")

    health_port = pick("health_port")
    try:
        health_port = int(health_port) if health_port is not None else None
    except ValueError:
        parser.error("BRIDGE_HEALTH_PORT must be a number")

    host = pick("host")
    if host and not host.startswith("ws://") and not host.startswith("wss://"):
        host = "ws://" + host

    return {
        "homeserver": homeserver,
        "access_token": access_token,
        "user_id": user_id or None,
        "key": pick("key"),
        "password": pick("password"),
        "host": host,
        "port": port,
        "self_signed": flag("self_signed", False),
        "lang": pick("lang") or "en-us",
        "site_id": pick("site_id") or "matrix",
        "allowed_rooms": allowed_rooms,
        "require_mention": flag("require_mention", True),
        "session_scope": session_scope,
        "dm_policy": dm_policy,
        "health_port": health_port,
    }


def write_termination_message(message: str, path: Optional[str] = None) -> None:
    """Leave ``message`` where Kubernetes reads why the container stopped.

    Outside Kubernetes the file usually cannot be written; that is not an error.
    """
    path = path or TERMINATION_LOG
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(message)
    except OSError:
        LOG.debug(f"no termination log at {path}")


def connect_matrix_to_hivemind(**options) -> HiveMindMatrixBridge:
    """Build the bridge from ``options`` and run it until it stops."""
    bridge = HiveMindMatrixBridge(**options)
    bridge.start()
    return bridge


def main(argv: Optional[Sequence[str]] = None) -> None:
    options = resolve_options(argv)
    LOG.info("bridge starting; press Ctrl-C to stop")
    try:
        connect_matrix_to_hivemind(**options)
    except TokenRejected as error:
        LOG.error(f"the Matrix homeserver refused the access token ({error.describe()}); "
                  "not retrying: give the bridge a new token")
        write_termination_message(TOKEN_REJECTED)
        raise SystemExit(EXIT_TOKEN_REJECTED) from None
    except ConnectionRefusedError as error:
        LOG.error(f"{error}; not retrying")
        raise SystemExit(1) from None
    except MatrixError as error:
        LOG.error(f"the Matrix homeserver answered something unusable ({error.describe()})")
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        LOG.info("shutting down")


if __name__ == '__main__':
    main()
