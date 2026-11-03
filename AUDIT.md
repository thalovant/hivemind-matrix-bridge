Last Edit: Claude Opus 5.5 - 2026-11-02 - Motive: Thalovant fork set up: audit of the managed multi-room bridge from the unit suite, a mutation pass and the live run.

# Audit: hivemind-matrix-bridge

Evidence: `python -m pytest tests -q` (125 passed, 2026-11-02, Python 3.12), a mutation pass (17 behaviours gutted one at a time, each caught by the suite), `tests/integration/live_matrix.py` against Conduit 0.10.9 and hivemind-core 5.3.10a1 (every check passed, transcript in `docs/live-test.md`), and reading the code at commit `2658b98a0625` (the line numbers below refer to it).

## 1. End-to-end encryption is not supported

- **Where:** `HiveMindMatrixBridge._mark_encrypted` (`hm_matrix_bridge/__init__.py:472`), `handle_message`.
- **Finding:** an encrypted room is never answered; it is logged once per process. Element encrypts private rooms and DMs by default, and encryption cannot be turned off, so many rooms people already have will not work. Supporting it needs a device and a persistent Olm/Megolm key store per bot, which the platform does not provide.
- **Test:** `tests/test_bridge.py::test_an_encrypted_room_is_never_answered_and_logged_once`, the live run's encrypted room.

## 2. Without an allow-list every room is answered and every invite accepted

- **Where:** `answer_policy` (`hm_matrix_bridge/__init__.py:550`), `handle_invite` (`:482`).
- **Finding:** anyone who can invite the bot then spends the hub's capacity. A managed deployment must always set `MATRIX_ALLOWED_ROOMS`.

## 3. Messages sent while the bridge is down are never answered

- **Where:** `run_matrix`; the sync token is kept in memory only (`_since`).
- **Finding:** after a restart the first sync is history by design. Persisting `next_batch` would answer them, possibly long after they were asked.

## 4. A burst bigger than the timeline limit loses messages

- **Where:** `SYNC_FILTER = sync_filter(50)` (`hm_matrix_bridge/__init__.py:121`).
- **Finding:** more than 50 matching events in one room between two syncs makes the sync `limited`; the older ones are not fetched (`/messages` back-pagination is not done).

## 5. A reply holds the hub's bus thread

- **Where:** `speak` (`hm_matrix_bridge/__init__.py:718`), `MatrixClient.send_message` (`hm_matrix_bridge/matrix.py:253`).
- **Finding:** the send runs on the HiveMind bus thread: pacing (1 s), up to `MAX_SEND_ATTEMPTS = 3` attempts (`matrix.py:47`) with waits up to `MAX_RATE_LIMIT_WAIT = 30.0` s (`matrix.py:51`) and a 30 s request timeout (`matrix.py:40`). A slow or rate-limiting homeserver delays every other message from the hub.

## 6. No per-person rate limit in the bridge

- **Where:** `forward_to_hivemind` (`hm_matrix_bridge/__init__.py:654`).
- **Finding:** every addressed message is forwarded at once. The platform's hub chain applies a per-person limit by session id (`person` scope); a bridge run elsewhere has none.

## 7. An unreachable homeserver is retried forever

- **Where:** `identify` (`hm_matrix_bridge/__init__.py:260`, wait at `:281`), `_sync_until_done` (wait at `:370`).
- **Finding:** a wrong `MATRIX_HOMESERVER` (typo, a non-Matrix site answering 404) keeps the pod running and not ready, with the reason only in the log, never in the termination message.

## 8. Display-name mentions can misfire

- **Where:** `_strip_mentions` (`hm_matrix_bridge/__init__.py:614`).
- **Finding:** a display name that is a common word ("Assistant") counts as a mention wherever it appears as a whole word. Pills (`m.mentions`) are exact.

## 9. The RSA identity key is new on every pod start

- **Where:** hivemind-bus-client writes `unnamed-node.pem` beside the Noise key in the emptyDir (seen in the live run's listing).
- **Finding:** the hub keeps the first RSA key it pinned and logs a mismatch on later HELLOs; it does not refuse the connection, and bridges send no INTERCOM messages that would need it. The live run's restart reconnects and answers. Same for the Telegram and Discord forks.

## 10. The fallback reply is English only

- **Where:** `handle_intent_failure` (`hm_matrix_bridge/__init__.py:695`).
- **Finding:** "I don't know how to answer that", whatever `HIVEMIND_LANG` is.

## 11. Tests cover one homeserver

- **Where:** `tests/integration/live_matrix.py` (`CONDUIT_IMAGE`).
- **Finding:** the live run uses Conduit; Synapse (the most common homeserver) is not exercised. The unit suite scripts the Matrix side and mocks the hub. Room summaries differ between servers; `is_direct` falls back to `/joined_members` when the summary has no count.

## 12. Prerelease dependencies, base image by tag

- **Where:** `pyproject.toml:16` (`hivemind-bus-client>=1.0.16a1`), `Dockerfile:1` (`FROM python:3.14-slim`).
- **Finding:** each build can resolve newer alphas and a newer base; images are not reproducible across builds.
