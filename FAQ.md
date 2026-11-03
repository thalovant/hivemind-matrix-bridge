Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Review fixes: notices, long replies, upgraded rooms, DM invites with a list, homeserver URL.

# FAQ: hivemind-matrix-bridge

## Why does the bot not answer in my room at all?

Most often the room is end-to-end encrypted: the bridge keeps no device keys, so `HiveMindMatrixBridge.handle_message` drops everything there and `_mark_encrypted` logs the room once. Element encrypts private rooms and DMs by default, and encryption cannot be turned off; create a new room with encryption off. Otherwise the room is not on `MATRIX_ALLOWED_ROOMS`, or the hub has not allowed the client `recognizer_loop:utterance`.

## Why does the bot ignore some messages in a room?

In a room with more than two members, `HiveMindMatrixBridge.addressed_text` forwards only messages that mention the bot (pill, user id or display name) or reply to one of its messages, and removes the mention first. `MATRIX_REQUIRE_MENTION=false` forwards every message.

## What counts as a direct message?

A room with exactly two joined members, the bot and one person (`HiveMindMatrixBridge.is_direct`), counted from the sync's room summary or `/joined_members`. The `m.direct` account data and the invite's `is_direct` flag are not used.

## A two-member room is on my allow-list and `MATRIX_DM_POLICY=deny`. Is it answered?

Yes. A listed room is always answered (`answer_policy`); the DM policy only decides for two-member rooms that are not on the list. In a two-member room no mention is needed.

## Why does the bot not join a room it was invited to?

`handle_invite` accepts invites only to rooms on `MATRIX_ALLOWED_ROOMS` (to every room when there is no list). The others stay pending. The list takes room ids (`!opaque:server`), not aliases.

## Does the bot answer what was said while it was down?

No. The first sync after a start is history and is never answered, and the bridge keeps no sync token across restarts. Nothing from before the bot joined a room is answered either.

## How do I keep secrets off the command line?

Use `MATRIX_ACCESS_TOKEN_FILE`, `HIVEMIND_ACCESS_KEY_FILE` and `HIVEMIND_PASSWORD_FILE` pointing at mounted files (`resolve_options` in `hm_matrix_bridge/__main__.py`). The image's entrypoint passes no argument.

## What does exit status 3 mean?

The homeserver refused the access token, at start or later, or the token belongs to another account than `MATRIX_USER_ID`. `main` writes `token_rejected` to `/dev/termination-log`. Make a new token; retrying cannot help.

## What does `/readyz` check?

That the HiveMind session is live (the client's `handshake_event`), the first sync is done, and the last sync succeeded (`HiveMindMatrixBridge.ready`). `/healthz` only says the process answers.

## Why must `/home/bridge/.config/hivemind` persist, or the Noise key be mounted?

The hub pins the bridge's Noise static key (`unnamed-node_noise.key`). A new key on every start is refused. The live test (`docs/live-test.md`) checks the hub pinned the mounted key and accepts it again after a restart.

## Can I use a password instead of a token?

No. The managed bridge takes an access token only; `docs/setup.md` shows how to get one with a single login.

## Why is the default branch `main` and not `dev`?

This is the Thalovant fork. Upstream's release workflows were removed, so nothing here publishes to PyPI or to upstream's registry; images go to `ghcr.io/thalovant/hivemind-matrix-bridge`.

## Why are the bot's replies grey (notices) and not normal messages?

`HiveMindMatrixBridge.deliver` posts `m.notice`, the message type the Matrix spec reserves for bots and bridges: automated clients never answer one. With `m.text`, two bots in one room (two HiveMind bridges, say) answered each other without end. The bridge itself ignores notices.

## What happens to a very long reply, or a very long question?

A reply over 16000 UTF-8 bytes is split at a line break or space into at most 4 messages, the rest cut with ` [...]` (`split_reply`); a homeserver refuses an event over 64 KiB. A question is cut to 4096 characters before it goes to the hub (`MAX_UTTERANCE_CHARS`, Telegram's own limit).

## A listed room was upgraded and the bot went quiet. Why?

An upgrade makes a new room with a new id. The allow-list holds the old one, so the bot neither accepts an invite to the new room nor answers there. Add the new room id to `MATRIX_ALLOWED_ROOMS` and invite the bot.

## `MATRIX_DM_POLICY=allow` with an allow-list: why does a DM invite stay pending?

Invites are accepted only to listed rooms (`handle_invite`), and a DM starts with an invite. Accepting every invite flagged `is_direct` would let anyone pull the bot into any room by flagging the invite. List the DM's room id, or run without a list (AUDIT.md section 14).

## Which homeserver URLs are refused?

One with a user name or password (requests would replace the token's header with Basic auth), a query or a fragment (`resolve_options`). Plain `http://` to a host other than loopback starts, with a warning: the token would cross the network unencrypted. A redirect from the homeserver is never followed.
