Last Edit: Claude Opus 5.5 - 2026-11-02 - Motive: Rewritten for the managed bridge: access token, room ids, unencrypted rooms, hub registration, image.

# Setup walkthrough

```
Matrix user  ⇄  homeserver  ⇄  hivemind-matrix-bridge  ⇄  HiveMind hub  ⇄  OVOS skills
```

## 1. A Matrix account for the bot

Register an account for the bot on any homeserver (`@hivebot:example.org`), and give it a display name: people mention the bot by it.

## 2. An access token

The bridge logs in with an access token, never a password. Get one by logging in once:

```bash
curl -s -X POST https://matrix.example.org/_matrix/client/v3/login \
  -H 'Content-Type: application/json' \
  -d '{"type":"m.login.password","identifier":{"type":"m.id.user","user":"hivebot"},"password":"...","initial_device_display_name":"hivemind-matrix-bridge"}'
```

Keep `access_token` from the answer in a file (that is what `MATRIX_ACCESS_TOKEN_FILE` reads). Do not log the session out: that revokes the token, and the bridge then stops with `token_rejected`. In Element, *Settings → Help & About → Access Token* shows the token of that Element session, which ends when Element logs out; a dedicated login is better.

## 3. Rooms without encryption

The bridge cannot read or answer in end-to-end encrypted rooms. Element turns encryption on by default for private rooms and direct messages, and it cannot be turned off later. So:

- create the room with *Enable end-to-end encryption* switched off (or make it public), then invite the bot;
- a DM opened from Element is encrypted, so the bot will not answer there. A two-person room created unencrypted works, and counts as a DM for the mention rule (no mention needed).

The bridge logs an encrypted room once (`room !id is end-to-end encrypted; ...`) and stays silent there.

## 4. The room ids

`MATRIX_ALLOWED_ROOMS` takes room ids, not aliases: Element shows them under *Room settings → Advanced* (`!abcdef:example.org`, or `!abcdef` for room version 12). Invite the bot to each; it accepts invites only to listed rooms.

## 5. Register the bridge on the hub

```bash
hivemind-core add-client --name matrix-bridge --access-key "<key>" --password "<password>"
hivemind-core allow-msg recognizer_loop:utterance matrix-bridge
```

A new client can send nothing until it is allowed `recognizer_loop:utterance`; a bridge that connects and never answers usually lacks this.

## 6. Run it

See the [README](../README.md#run-it) for the `docker run` line and [configuration.md](configuration.md) for every setting. `/readyz` turns 200 once the hub session is up and the first sync is done.

## 7. Check the round trip

In a listed room with at least two other people, send `HiveBot: what time is it` (with the mention pill). The bot answers in the room. A message without the mention gets no answer; in a two-member room every message does.

## Troubleshooting

- **Exit status 3, `token_rejected`**: the homeserver refused the token, or it belongs to another account than `MATRIX_USER_ID`. Make a new token (step 2).
- **Never ready, log says `cannot reach the Matrix homeserver`**: `MATRIX_HOMESERVER` must be the client API URL the bridge can reach (outbound HTTPS).
- **Bot never joins**: the room id is not on `MATRIX_ALLOWED_ROOMS`, or it is an alias.
- **Bot joined but is silent**: the room is encrypted (see the log), the message did not mention the bot, or the hub has not allowed `recognizer_loop:utterance`.
- **Hub refuses after a reinstall**: the hub pinned the bridge's Noise key; keep `/home/bridge/.config/hivemind` (or the mounted key) across restarts, or reset the pin on the hub.
