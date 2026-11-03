Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: CodeRabbit on PR #2: replies ping nobody only on Matrix spec v1.7 and later.

# HiveMind Matrix Bridge

Answer [Matrix](https://matrix.org) rooms from a [HiveMind](https://github.com/JarbasHiveMind/HiveMind-core) hub.

The bridge is a HiveMind **satellite** whose input and output are a Matrix bot account instead of a microphone. A message addressed to the bot becomes an utterance on the hub; the hub's spoken reply is posted back into the same room.

```
Matrix rooms  ⇄  homeserver  ⇄  hivemind-matrix-bridge  ⇄  HiveMind hub  ⇄  OVOS skills
```

## What it answers

- Rooms on `MATRIX_ALLOWED_ROOMS` (room ids). Without a list, every room the bot is in.
- Invites are accepted only to listed rooms (to any room without a list). Other invites are left pending.
- A **direct message** is a room with exactly two joined members, the bot and one person. One that is not on the list follows `MATRIX_DM_POLICY`: `auto` (default) refuses it when rooms are listed, `allow`, or `deny`.
- In a room with more people, only messages that **mention the bot** (a pill, its user id or its display name) or **reply to one of its messages**. The mention is removed before the text goes to the hub. `MATRIX_REQUIRE_MENTION=false` answers every message.
- **End-to-end encrypted rooms are not supported.** The bridge keeps no device keys, so it never answers in an encrypted room and logs the room once. Create rooms for the bot with encryption off (it cannot be turned off later).
- Nothing from before the bridge started, or from before the bot joined a room, is answered.
- Replies are **notices** (`m.notice`): the message type bots send and never answer, so two bots in a room cannot loop. On Matrix spec v1.7 and later they ping nobody; an older server or client may still notify a member named in a reply, or the room for `@room` when the bot's power level reaches the room's `notifications.room` value (50 by default), so keep the bot at power level 0. A reply too long for one Matrix message is split (at most 4 messages); a question is cut to 4096 characters.

## Run it

The image reads every setting from the environment and every secret from a file:

```bash
docker run --read-only --tmpfs /tmp \
  -v bridge-identity:/home/bridge/.config/hivemind \
  -v "$PWD/secrets:/run/secrets:ro" \
  -e MATRIX_HOMESERVER=https://matrix.example.org \
  -e MATRIX_USER_ID=@hivebot:example.org \
  -e MATRIX_ACCESS_TOKEN_FILE=/run/secrets/matrix-token \
  -e MATRIX_ALLOWED_ROOMS='!abcdefghijklmnop:example.org' \
  -e HIVEMIND_HOST=ws://hub.example.org -e HIVEMIND_PORT=5678 \
  -e HIVEMIND_ACCESS_KEY_FILE=/run/secrets/hivemind-key \
  -e HIVEMIND_PASSWORD_FILE=/run/secrets/hivemind-password \
  -e BRIDGE_HEALTH_PORT=8080 \
  ghcr.io/thalovant/hivemind-matrix-bridge:latest
```

`/home/bridge/.config/hivemind` holds the HiveMind identity and the Noise key the hub pins: keep it (or mount the key) across restarts.

From a checkout: `pip install .` installs the `hivemind-matrix-bridge` command, which takes the same settings as flags (`hivemind-matrix-bridge --help`).

## Settings

| Variable | Flag | Default |
| --- | --- | --- |
| `MATRIX_HOMESERVER` | `--homeserver` | required: client API URL, `https://...` |
| `MATRIX_ACCESS_TOKEN` / `MATRIX_ACCESS_TOKEN_FILE` | `--access-token` | required |
| `MATRIX_USER_ID` | `--user-id` | the token's account; when set, the token must belong to it |
| `MATRIX_ALLOWED_ROOMS` | `--allowed-room` (repeatable) | none: every room, every invite |
| `MATRIX_REQUIRE_MENTION` | `--no-require-mention` | `true` |
| `MATRIX_SESSION_SCOPE` | `--session-scope` | `person` (or `chat`) |
| `MATRIX_DM_POLICY` | `--dm-policy` | `auto` (or `allow`, `deny`) |
| `BRIDGE_HEALTH_PORT` | `--health-port` | off |
| `HIVEMIND_HOST`, `HIVEMIND_PORT` | `--host`, `--port` | identity file, `5678` (image: `ws://127.0.0.1`) |
| `HIVEMIND_ACCESS_KEY` / `_FILE`, `HIVEMIND_PASSWORD` / `_FILE` | `--access-key`, `--password` | identity file |
| `HIVEMIND_SITE_ID`, `HIVEMIND_LANG`, `HIVEMIND_SELF_SIGNED` | `--site-id`, `--lang`, `--self-signed` | `matrix`, `en-us`, off |

A flag wins over its variable, and the variable over its `_FILE`. Details: [docs/configuration.md](docs/configuration.md).

## Health and exit

- `GET /healthz`: the process answers. `GET /readyz`: the hub session is live, the first sync is done and the last one succeeded.
- A refused access token (or a token of another account than `MATRIX_USER_ID`) writes `token_rejected` to `/dev/termination-log` and exits with status 3. A hub that refuses the bridge's credentials exits with status 1.

## Documentation

- [docs/index.md](docs/index.md): overview and code map.
- [docs/setup.md](docs/setup.md): bot account, access token, room ids, hub registration.
- [docs/configuration.md](docs/configuration.md): every setting and rule.
- [docs/live-test.md](docs/live-test.md): the run against a real homeserver and hub.

## License

Apache-2.0, see [LICENSE](LICENSE).
