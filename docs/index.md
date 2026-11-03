Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Review fixes: the reply path (sender thread, m.notice, split replies) and the question cut.

# hivemind-matrix-bridge

A HiveMind bridge for Matrix: a satellite whose input and output are a Matrix bot account instead of a microphone. Messages addressed to the bot in the rooms it answers become `recognizer_loop:utterance` on the hub; `speak` replies go back to the same room.

- How to run it and every setting: [README](../README.md), [configuration.md](configuration.md)
- Bot account, access token, room ids, hub registration: [setup.md](setup.md)
- The run against a real homeserver and a real hub: [live-test.md](live-test.md)
- Facts for lookup: [QUICK_FACTS.md](../QUICK_FACTS.md)
- Questions: [FAQ.md](../FAQ.md)
- Known issues: [AUDIT.md](../AUDIT.md)
- Proposed work: [SUGGESTIONS.md](../SUGGESTIONS.md)
- What changed and how it was verified: [MAINTENANCE_REPORT.md](../MAINTENANCE_REPORT.md)

## Code map

| File | What it holds |
| --- | --- |
| `hm_matrix_bridge/__init__.py` | `HiveMindMatrixBridge`: `identify` (whoami), `connect_hivemind`, `run_matrix` (sync loop), `process_sync`, `handle_invite`, `handle_message`, `answer_policy` (allow-list, DM policy), `is_direct`, `addressed_text` (mentions, replies), `forward_to_hivemind`, `hub_live`, `session_id`, `speak` (queues), `deliver` (posts), `wait_for_replies`, `ready`; `RoomState`; `sync_filter`, `split_reply` |
| `hm_matrix_bridge/matrix.py` | `MatrixClient` (client-server API over `requests`: `whoami`, `sync`, `join`, `joined_members`, `event`, `send_message` with pacing and bounded rate-limit retries); `MatrixError`, `TokenRejected`, `AccountMismatch` |
| `hm_matrix_bridge/__main__.py` | `resolve_options` (flags, environment, `*_FILE` secrets), `write_termination_message`, `main` (exit status 3 and `token_rejected` on a refused token) |
| `hm_matrix_bridge/health.py` | `HealthServer`: `/healthz` and `/readyz` |
| `Dockerfile` | the image: uid 10001, no arguments, read-only root |
| `.github/workflows/ci.yml` | tests, lint, image smoke tests, the live run, signed publish to `ghcr.io/thalovant/hivemind-matrix-bridge` |
| `tests/integration/live_matrix.py` | the image against Conduit and a real hivemind-core hub |

## The message path

1. `run_matrix` long-polls `/sync` with `SYNC_FILTER` (messages, encryption and membership only, members lazy-loaded so the room summary carries the joined member count).
2. `process_sync` updates `RoomState` (encrypted, joined members, the bot's display name in the room), accepts allowed invites, and hands each new `m.text` message to `handle_message`.
3. `handle_message` drops edits, notices, the bot's own messages and encrypted rooms, asks `answer_policy` whether the room is answered, then `addressed_text` whether the message is for the bot (mention stripped).
4. `forward_to_hivemind` emits `recognizer_loop:utterance` (the question cut to `MAX_UTTERANCE_CHARS`) with `room_id`, `thread_id` (in a thread), `user.matrix_user_id` and `session.session_id` in the context, only while the hub session is live (`hub_live`); a failed emit drops the message and logs the exception type only.
5. The hub's `speak` arrives on the bus thread; `speak` only queues it. The sender thread (`_send_replies`) calls `deliver`, which checks the room is still one the bridge answers in, splits a long reply (`split_reply`) and posts `m.notice` with `m.mentions: {}` (in the thread when the question was).

Related: the hub ([HiveMind-core](https://github.com/JarbasHiveMind/HiveMind-core)), the client library ([hivemind-bus-client](https://github.com/JarbasHiveMind/hivemind-bus-client)), the Telegram sibling ([thalovant/hivemind-telegram-bridge: docs/index.md](https://github.com/thalovant/hivemind-telegram-bridge/blob/main/docs/index.md)).
