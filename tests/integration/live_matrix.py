"""The bridge image against a real Matrix homeserver and a real HiveMind hub.

What runs:

- Conduit (``CONDUIT_IMAGE``), a real homeserver, in a container on the host
  network, with open registration;
- a real hivemind-core hub, booted in this process by hivescope, whose agent
  answers every utterance with a ``speak``;
- the bridge image, the way the operator renders its pod: uid 10001, a
  read-only root, tmpfs at ``~/.config/hivemind`` and ``/tmp``, the Noise key
  mounted read-only at ``~/.config/hivemind/unnamed-node_noise.key``, every
  secret a file, every setting from the environment.

The scenario: a bot account, two people (alice, bob), and five rooms alice
makes: ``allowed`` (unencrypted, three members), ``encrypted`` (on the
allow-list too, ``m.room.encryption`` set), ``unlisted``, a DM invite, and a
DM the bot already joined. Only ``allowed`` and ``encrypted`` are on
``MATRIX_ALLOWED_ROOMS``; ``MATRIX_DM_POLICY`` is left at ``auto``.

Checked: the bridge reports ready; it joins only the listed rooms; it answers
a mention in ``allowed``; it ignores an unmentioned message there, anything in
``encrypted`` (logging it once), and DMs; the hub pinned the mounted Noise
key; the log has no message text, no secret and no filesystem error.

Ignored messages are checked with a barrier, not a sleep: after them a
mentioned question is sent and its answer awaited, and then the hub's own
record of what reached it must hold only the mentioned questions.

Usage::

    pip install -r tests/integration/requirements.txt
    docker build -t hivemind-matrix-bridge:it .
    python tests/integration/live_matrix.py --image hivemind-matrix-bridge:it

``CONTAINER_ENGINE`` (docker or podman, default: podman if installed) and
``CONTAINER_ENGINE_ARGS`` (e.g. ``--cgroup-manager=cgroupfs``) choose how
containers are run. Exit status 0 means every check passed.
"""
import argparse
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

import requests

# The hub here does not need ovos-config's hot reload, and its file watcher
# fails on a host whose inotify watches are used up: switch it off before
# hivemind_core is imported.
import ovos_config.config  # noqa: E402

ovos_config.config.Configuration.set_config_watcher = staticmethod(lambda *a, **k: None)

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey  # noqa: E402
from hivescope.topology import TopologyBuilder  # noqa: E402

CONDUIT_IMAGE = ("docker.io/matrixconduit/matrix-conduit:v0.10.9@sha256:"
                 "849d5c49e4800bc9689dd4549005bf625cd2a9923f272af26a2cf33765be59d8")
SERVER_NAME = "bridge.test"
HUB_KEY = "integration-bridge-access-key-0123456789"
HUB_PASSWORD = "integration-bridge-password-01234"
MATRIX_PASSWORD = "integration-password-0123456789"
BOT_NAME = "HiveBot"

TRANSCRIPT: List[str] = []


class CheckFailed(Exception):
    pass


def say(line: str) -> None:
    TRANSCRIPT.append(line)
    print(line, flush=True)


def check(condition: bool, ok: str, failed: str) -> None:
    if not condition:
        say(f"[FAIL] {failed}")
        raise CheckFailed(failed)
    say(f"[ok]   {ok}")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def selinux_enforcing() -> bool:
    try:
        with open("/sys/fs/selinux/enforce", encoding="ascii") as handle:
            return handle.read().strip() == "1"
    except OSError:
        return False


class Engine:
    """docker or podman, as the environment says."""

    def __init__(self):
        name = os.environ.get("CONTAINER_ENGINE") or ("podman" if shutil.which("podman") else "docker")
        self.command = [name, *shlex.split(os.environ.get("CONTAINER_ENGINE_ARGS", ""))]
        self.relabel = ",z" if selinux_enforcing() else ""

    def __call__(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run([*self.command, *args], check=check, capture_output=True, text=True)

    def mount(self, source: str, target: str, read_only: bool = True) -> List[str]:
        options = ("ro" if read_only else "rw") + self.relabel
        return ["-v", f"{source}:{target}:{options}"]


class Account:
    """One Matrix user, speaking the client-server API directly."""

    def __init__(self, base: str, username: str):
        self.base = base
        answer = self._register(username)
        self.user_id = answer["user_id"]
        self.token = answer["access_token"]
        self._txn = 0

    def _register(self, username: str) -> Dict[str, Any]:
        url = f"{self.base}/_matrix/client/v3/register"
        body = {"username": username, "password": MATRIX_PASSWORD, "inhibit_login": False}
        first = requests.post(url, json=body, timeout=10)
        if first.status_code == 200:
            return first.json()
        session = first.json().get("session")
        second = requests.post(url, json={**body, "auth": {"type": "m.login.dummy", "session": session}},
                               timeout=10)
        second.raise_for_status()
        return second.json()

    def call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
             params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        answer = requests.request(method, f"{self.base}/_matrix/client/v3{path}", json=body,
                                  params=params, timeout=15,
                                  headers={"Authorization": f"Bearer {self.token}"})
        answer.raise_for_status()
        return answer.json() if answer.content else {}

    def create_room(self, **body) -> str:
        return self.call("POST", "/createRoom", body)["room_id"]

    def join(self, room_id: str) -> None:
        self.call("POST", f"/rooms/{requests.utils.quote(room_id, safe='')}/join", {})

    def send(self, room_id: str, body: str, mention: Optional[str] = None) -> str:
        self._txn += 1
        content: Dict[str, Any] = {"msgtype": "m.text", "body": body}
        if mention:
            content["m.mentions"] = {"user_ids": [mention]}
        room = requests.utils.quote(room_id, safe="")
        return self.call("PUT", f"/rooms/{room}/send/m.room.message/it{self._txn}{uuid.uuid4().hex[:6]}",
                         content)["event_id"]

    def messages_from(self, room_id: str, sender: str) -> List[str]:
        room = requests.utils.quote(room_id, safe="")
        chunk = self.call("GET", f"/rooms/{room}/messages", params={"dir": "b", "limit": 100})["chunk"]
        return [event["content"].get("body") for event in reversed(chunk)
                if event.get("type") == "m.room.message" and event.get("sender") == sender]

    def membership(self, room_id: str, user_id: str) -> Optional[str]:
        room = requests.utils.quote(room_id, safe="")
        user = requests.utils.quote(user_id, safe="")
        try:
            return self.call("GET", f"/rooms/{room}/state/m.room.member/{user}").get("membership")
        except requests.HTTPError:
            return None


def wait_until(condition, timeout: float, step: float = 0.25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(step)
    return condition()


def health(port: int, path: str) -> Optional[int]:
    try:
        return requests.get(f"http://127.0.0.1:{port}{path}", timeout=2).status_code
    except requests.RequestException:
        return None


def start_conduit(engine: Engine, name: str, port: int) -> str:
    engine("rm", "-f", name, check=False)
    engine("run", "-d", "--name", name, "--network", "host",
           "--tmpfs", "/var/lib/matrix-conduit",
           "-e", "CONDUIT_CONFIG=", "-e", f"CONDUIT_SERVER_NAME={SERVER_NAME}",
           "-e", "CONDUIT_DATABASE_PATH=/var/lib/matrix-conduit",
           "-e", "CONDUIT_DATABASE_BACKEND=rocksdb",
           "-e", f"CONDUIT_PORT={port}", "-e", "CONDUIT_ADDRESS=127.0.0.1",
           "-e", "CONDUIT_ALLOW_REGISTRATION=true", "-e", "CONDUIT_ALLOW_FEDERATION=false",
           "-e", "CONDUIT_ALLOW_CHECK_FOR_UPDATES=false", "-e", "CONDUIT_TRUSTED_SERVERS=[]",
           "-e", "CONDUIT_LOG=warn", CONDUIT_IMAGE)
    base = f"http://127.0.0.1:{port}"

    def up():
        try:
            return requests.get(f"{base}/_matrix/client/versions", timeout=1).ok
        except requests.RequestException:
            return False

    if not wait_until(up, 60):
        raise CheckFailed("the homeserver did not start")
    return base


def start_hub():
    builder = TopologyBuilder()
    hub = builder.add_master("M0", use_loopback=True)
    hub.register_satellite(HUB_KEY, password=HUB_PASSWORD,
                           allowed_types=["recognizer_loop:utterance"])
    builder.start_all()
    asked: List[Dict[str, Any]] = []
    lock = threading.Lock()
    bus = hub.agent_protocol.bus

    def answer(message):
        with lock:
            asked.append({"utterance": (message.data.get("utterances") or [None])[0],
                          "session": (message.context.get("session") or {}).get("session_id")})
            number = len(asked)
        bus.emit(message.reply("speak", {"utterance": f"answer #{number}"}))
        bus.emit(message.reply("ovos.utterance.handled", {}))

    bus.on("recognizer_loop:utterance", answer)
    return builder, hub, asked


def write_secret(path: str, value: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(value)
    os.chmod(path, 0o444)


def run(image: str, keep: bool) -> None:
    engine = Engine()
    work = tempfile.mkdtemp(prefix="matrix-bridge-it-")
    suffix = uuid.uuid4().hex[:8]
    conduit_name, bridge_name = f"it-conduit-{suffix}", f"it-bridge-{suffix}"
    builder = None
    bridge_log = ""
    try:
        base = start_conduit(engine, conduit_name, free_port())
        say(f"[ok]   homeserver Conduit 0.10.9 at {base} (server name {SERVER_NAME})")

        # Conduit makes the first account an admin with its own admin room:
        # let that be alice, not the bot.
        alice = Account(base, "alice")
        bob = Account(base, "bob")
        bot = Account(base, "hivebot")
        bot.call("PUT", f"/profile/{requests.utils.quote(bot.user_id, safe='')}/displayname",
                 {"displayname": BOT_NAME})
        say(f"[ok]   registered {bot.user_id} ({BOT_NAME}), {alice.user_id}, {bob.user_id}")

        allowed = alice.create_room(name="allowed", preset="private_chat", invite=[bot.user_id, bob.user_id])
        bob.join(allowed)
        encrypted = alice.create_room(name="encrypted", preset="private_chat",
                                      invite=[bot.user_id, bob.user_id], initial_state=[{
                                          "type": "m.room.encryption", "state_key": "",
                                          "content": {"algorithm": "m.megolm.v1.aes-sha2"}}])
        bob.join(encrypted)
        unlisted = alice.create_room(name="unlisted", preset="private_chat", invite=[bot.user_id])
        dm_invite = alice.create_room(is_direct=True, preset="trusted_private_chat", invite=[bot.user_id])
        dm_joined = alice.create_room(is_direct=True, preset="trusted_private_chat", invite=[bot.user_id])
        bot.join(dm_joined)  # a DM the bot was in before this bridge ran
        say(f"[ok]   rooms: allowed {allowed} (alice, bob, bot invited), encrypted {encrypted} "
            f"(m.megolm), unlisted {unlisted}, DM invite {dm_invite}, DM already joined {dm_joined}")

        builder, hub, asked = start_hub()
        hub_url = hub.network_protocol.url.rstrip("/")
        hub_host, hub_port = hub_url.replace("ws://", "").split(":")
        say(f"[ok]   HiveMind hub (hivemind-core via hivescope) at {hub_url}; "
            "client allowed recognizer_loop:utterance only")

        secrets = os.path.join(work, "hivemind")
        token_dir = os.path.join(work, "bridge-token")
        os.makedirs(secrets)
        os.makedirs(token_dir)
        write_secret(os.path.join(secrets, "apiKey"), HUB_KEY)
        write_secret(os.path.join(secrets, "password"), HUB_PASSWORD)
        write_secret(os.path.join(token_dir, "token"), bot.token)
        noise = X25519PrivateKey.generate()
        write_secret(os.path.join(work, "noiseKey"), noise.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
            serialization.NoEncryption()).hex())
        noise_public = noise.public_key().public_bytes(serialization.Encoding.Raw,
                                                       serialization.PublicFormat.Raw).hex()

        health_port = free_port()
        environment = {
            "HOME": "/home/bridge",
            "HIVEMIND_HOST": f"ws://{hub_host}", "HIVEMIND_PORT": hub_port,
            "HIVEMIND_SITE_ID": "matrix-it",
            "HIVEMIND_ACCESS_KEY_FILE": "/var/run/thalovant/hivemind/apiKey",
            "HIVEMIND_PASSWORD_FILE": "/var/run/thalovant/hivemind/password",
            "MATRIX_HOMESERVER": base, "MATRIX_USER_ID": bot.user_id,
            "MATRIX_ACCESS_TOKEN_FILE": "/var/run/thalovant/bridge-token/token",
            "MATRIX_ALLOWED_ROOMS": f"{allowed},{encrypted}",
            "MATRIX_REQUIRE_MENTION": "true", "MATRIX_SESSION_SCOPE": "person",
            "BRIDGE_HEALTH_PORT": str(health_port),
        }
        def start_bridge() -> None:
            """The pod as the operator renders it; the identity directory starts empty."""
            engine("rm", "-f", bridge_name, check=False)
            engine("run", "-d", "--name", bridge_name, "--network", "host",
                   "--read-only", "--user", "10001:10001", "--cap-drop", "ALL",
                   "--security-opt", "no-new-privileges",
                   "--tmpfs", "/tmp", "--tmpfs", "/home/bridge/.config/hivemind:mode=1777",
                   *engine.mount(os.path.join(work, "noiseKey"),
                                 "/home/bridge/.config/hivemind/unnamed-node_noise.key"),
                   *engine.mount(secrets, "/var/run/thalovant/hivemind"),
                   *engine.mount(token_dir, "/var/run/thalovant/bridge-token"),
                   *[part for key, value in environment.items() for part in ("-e", f"{key}={value}")],
                   image)

        start_bridge()
        identity = engine("exec", bridge_name, "id").stdout.strip()
        root_write = engine("exec", bridge_name, "sh", "-c",
                            "touch /usr/probe 2>&1 || true; touch /home/bridge/probe 2>&1 || true",
                            check=False).stdout
        check(identity.startswith("uid=10001") and root_write.count("Read-only file system") == 2,
              f"bridge container {bridge_name}: {identity}; root filesystem read-only",
              f"bridge container identity {identity!r}, root write test {root_write!r}")

        started = time.monotonic()
        ready = wait_until(lambda: health(health_port, "/readyz") == 200, 90)
        check(bool(ready) and health(health_port, "/healthz") == 200,
              f"/readyz 200 after {time.monotonic() - started:.1f}s, /healthz 200",
              f"/readyz {health(health_port, '/readyz')}, /healthz {health(health_port, '/healthz')}")

        def joined_rooms():
            return set(bot.call("GET", "/joined_rooms")["joined_rooms"])

        wait_until(lambda: {allowed, encrypted} <= joined_rooms(), 30)
        joined = joined_rooms()
        pending = {room: alice.membership(room, bot.user_id) for room in (unlisted, dm_invite)}
        check(joined == {allowed, encrypted, dm_joined} and set(pending.values()) == {"invite"},
              f"accepted only the listed invites (allowed, encrypted); unlisted and DM invite left "
              f"pending: {pending}",
              f"joined {sorted(joined)}, pending {pending}")

        answers_before = len(alice.messages_from(allowed, bot.user_id))

        def barrier(question: str) -> List[str]:
            """Ask a mentioned question in ``allowed``; its answer proves the earlier ones were seen."""
            alice.send(allowed, f"{BOT_NAME}: {question}", mention=bot.user_id)
            wait_until(lambda: any(item["utterance"] == question for item in asked), 30)
            expected = answers_before + len(asked)
            wait_until(lambda: len(alice.messages_from(allowed, bot.user_id)) >= expected, 30)
            return alice.messages_from(allowed, bot.user_id)

        replies = barrier("what time is it")
        check([item["utterance"] for item in asked] == ["what time is it"] and replies[-1:] == ["answer #1"]
              # the hub namespaces a client's session ids with its own prefix
              and str(asked[0]["session"]).endswith(f"matrix-{allowed}-{alice.user_id}"),
              f"mention answered in allowed: alice asked 'what time is it', hub got it in session "
              f"{asked[0]['session'] if asked else None}, bot replied {replies[-1:]}",
              f"hub saw {asked}, bot replied {replies}")

        bob.send(allowed, "just chatting about lunch, no bot here")
        alice.send(encrypted, f"{BOT_NAME}: secret question in the encrypted room", mention=bot.user_id)
        bob.send(encrypted, "anyone here?")
        alice.send(dm_joined, "hello bot, this is a DM")
        alice.send(dm_invite, "hello again")
        replies = barrier("and the date")
        utterances = [item["utterance"] for item in asked]
        check(utterances == ["what time is it", "and the date"],
              "ignored: an unmentioned message in allowed, both messages in encrypted, the DM "
              "(dm policy auto with an allow-list); the hub received only the two mentioned questions",
              f"hub saw {utterances}")
        silent = {name: alice.messages_from(room, bot.user_id)
                  for name, room in (("encrypted", encrypted), ("dm", dm_joined))}
        check(all(not bodies for bodies in silent.values()) and replies[-1:] == ["answer #2"],
              "the bot said nothing in the encrypted room or the DM; second answer in allowed",
              f"bot messages elsewhere {silent}, replies {replies}")

        pinned = (hub.db.get_client_by_api_key(HUB_KEY).metadata or {}).get("noise_pubkey")
        check(pinned == noise_public,
              f"hub pinned the bridge's Noise key from the mounted file ({noise_public[:16]}...)",
              f"hub pinned {pinned!r}, mounted key's public half {noise_public!r}")
        listing = engine("exec", bridge_name, "ls", "-A", "/home/bridge/.config/hivemind").stdout.split()
        say(f"[info] ~/.config/hivemind in the container: {listing}")

        # A restart is a new pod: empty identity directory, same mounted Noise key.
        first_log = engine("logs", bridge_name, check=False)
        bridge_log = (first_log.stdout or "") + (first_log.stderr or "")
        start_bridge()
        restarted = time.monotonic()
        check(bool(wait_until(lambda: health(health_port, "/readyz") == 200, 90)),
              f"restarted in a fresh container: /readyz 200 after {time.monotonic() - restarted:.1f}s "
              "(the hub accepted the same pinned Noise key)",
              f"after a restart /readyz is {health(health_port, '/readyz')}")
        replies = barrier("still there")
        check([item["utterance"] for item in asked] == ["what time is it", "and the date", "still there"]
              and replies[-1:] == ["answer #3"],
              "after the restart: answered the new question, re-answered nothing from history",
              f"hub saw {[item['utterance'] for item in asked]}, replies {replies}")
    finally:
        logs = engine("logs", bridge_name, check=False)
        bridge_log += (logs.stdout or "") + (logs.stderr or "")
        with open(os.path.join(work, "bridge.log"), "w", encoding="utf-8") as handle:
            handle.write(bridge_log)
        if not keep:
            engine("rm", "-f", bridge_name, check=False)
            engine("rm", "-f", conduit_name, check=False)
        if builder is not None:
            builder.stop_all()

    encrypted_lines = [line for line in bridge_log.splitlines() if "end-to-end encrypted" in line]
    check(len(encrypted_lines) == 2 and all(encrypted in line for line in encrypted_lines),
          "the encrypted room was logged once per run (two runs): " + (encrypted_lines[0].split(" - ")[-1] if encrypted_lines else ""),
          f"encrypted-room log lines: {encrypted_lines}")
    leaked = [text for text in ("what time is it", "secret question", "just chatting", "this is a DM",
                                "answer #1", HUB_PASSWORD, HUB_KEY, bot.token) if text in bridge_log]
    errors = [line for line in bridge_log.splitlines()
              if "Read-only file system" in line or "Permission denied" in line or "Traceback" in line]
    check(not leaked and not errors,
          f"bridge log ({len(bridge_log.splitlines())} lines, saved to {work}/bridge.log): no message "
          "text, no secret, no filesystem error, no traceback",
          f"leaked {leaked}, errors {errors[:3]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--image", default=os.environ.get("BRIDGE_IMAGE", "hivemind-matrix-bridge:it"))
    parser.add_argument("--keep", action="store_true", help="leave the containers running")
    parser.add_argument("--transcript", help="also write the transcript to this file")
    args = parser.parse_args()
    status = 0
    try:
        run(args.image, args.keep)
        say("[pass] every check passed")
    except CheckFailed:
        status = 1
    except Exception as error:  # an unexpected failure is a failed run, with its reason
        say(f"[FAIL] {type(error).__name__}: {error}")
        status = 1
    if args.transcript:
        with open(args.transcript, "w", encoding="utf-8") as handle:
            handle.write("\n".join(TRANSCRIPT) + "\n")
    return status


if __name__ == "__main__":
    sys.exit(main())
