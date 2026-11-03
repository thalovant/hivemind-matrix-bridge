Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Adversarial review of the merged fork (R1-R8 fixed, sections 14-20 open); CodeRabbit follow-ups on PR #2 (Retry-After overflow, split keeps separators, mark within limit, shutdown count).

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

## 5. A reply holds the hub's bus thread (fixed 2026-11-03)

- **Where:** `speak` (`hm_matrix_bridge/__init__.py:718`), `MatrixClient.send_message` (`hm_matrix_bridge/matrix.py:253`).
- **Finding:** the send runs on the HiveMind bus thread: pacing (1 s), up to `MAX_SEND_ATTEMPTS = 3` attempts (`matrix.py:47`) with waits up to `MAX_RATE_LIMIT_WAIT = 30.0` s (`matrix.py:51`) and a 30 s request timeout (`matrix.py:40`). A slow or rate-limiting homeserver delays every other message from the hub.
- **Fixed:** see R4 below; `speak` only queues, a sender thread posts.

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

## 13. CodeQL flags the live test's credential files

- **Where:** `tests/integration/live_matrix.py:247` (`write_secret`), CodeQL alert 1, `py/clear-text-storage-sensitive-data`.
- **Finding:** the harness writes throwaway hub credentials and the test bot's token to files, because the bridge reads secrets from files exactly as the operator mounts them. They live in a per-run `mkdtemp` directory (mode 0700) and are made for that run. Not a production path; it can be dismissed as used in tests.

## Review 2026-11-03 (fixed on `fix/review-2026-11-03`)

Evidence: an adversarial read of `main` at `35bb1aea` (PR #1 merged without an automated review), one failing test written per finding before its fix, `python -m pytest tests -q` 167 passed afterwards (Python 3.12), `ruff check --isolated --select F,E9 .` clean, the CLI tests run against the old `__main__.py` (5 of them fail there), and the SIGTERM path checked on a real process. Line numbers refer to `60ba42736260`.

| # | Severity | Where | Finding | Fix and test |
| --- | --- | --- | --- | --- |
| R1 | High | `forward_to_hivemind` (`hm_matrix_bridge/__init__.py:751`), `hub_live` (`:480`) | While the hub connection is down (any hub restart or roll), `HiveMessageBusClient.emit()` raises `RuntimeError("... Failed to emit : <serialized message>")`. Nothing caught it: the bridge exited and Python printed a traceback whose last line held the question, the sender and the room. `handle_message` only checked a flag that is set once and never cleared. | Checks the live session (`handshake_event`) and catches what `emit` raises, logging the type only (`:769`). `tests/test_logs.py::test_a_failed_forward_neither_stops_the_bridge_nor_logs_the_question`, `tests/test_bridge.py::test_a_message_while_the_hub_reconnects_is_dropped_not_emitted` |
| R2 | High | `addressed_text` (`:663`), `_handle_one` (`:538`) | Any member of an answered room could crash-loop the bridge with one event: `"m.in_reply_to": "x"` raised `AttributeError`, a non-string event id `TypeError` when hashed or quoted; it left the sync loop, the bridge exited, and the member could resend after every restart. | Ids used only when they are strings (`_event_id`, `:911`; `_mapping`, `:906`); each message handled in `_handle_one`, which drops what it cannot handle (type logged only, `:549`); odd sync sections ignored. `tests/test_bridge.py::test_a_malformed_relation_from_a_member_cannot_stop_the_bridge` (6 shapes, real id quoting), `::test_one_event_that_cannot_be_handled_does_not_stop_the_others`, `::test_an_odd_sync_answer_is_ignored_not_fatal` |
| R3 | Medium | `deliver` (`:879`) | Replies were `m.text` while the bridge ignores `m.notice`. A second bot that follows the same rule, a second HiveMind bridge included, answered every reply: two bridges in a two-member room looped until a hub quota ran out. The spec reserves `m.notice` for bots and asks them never to answer one. | Replies are `m.notice` (still `m.mentions: {}`). `tests/test_bridge.py::test_two_bridges_in_one_room_do_not_answer_each_other` |
| R4 | Medium | `speak` (`:802`), `_send_replies` (`:842`), `deliver` (`:858`), `stop` (`:450`) | Section 5, and worse: hivemind-bus-client closes the hub socket when a handler raises, so an odd reply failure dropped the session. A reply over 64 KiB was refused (`M_TOO_LARGE`) and lost. SIGTERM (how Kubernetes stops a pod) ended Python at once, without `stop()`. | One sender thread, queue of `MAX_PENDING_REPLIES = 100` (`:104`, more dropped and logged); replies split at `MAX_BODY_BYTES = 16000` (`:113`, `split_reply` `:192`) into at most 4 messages, never inside a character; `stop` drains 5 s; SIGTERM raises `KeyboardInterrupt` (`hm_matrix_bridge/__main__.py:303`, installed `:316`). `tests/test_replies.py`, `tests/test_cli.py::test_sigterm_stops_the_bridge_the_way_ctrl_c_does` |
| R5 | Medium | `MatrixClient.request` (`hm_matrix_bridge/matrix.py:192`) | `requests` followed redirects. It drops the `Authorization` header on a host change, but re-sends a 307/308 PUT body (a reply's text) to any host, internal ones included; a 3xx without a body read as an empty success (an empty sync kept `/readyz` green). | `allow_redirects=False`; a 3xx is `MatrixError(status, "REDIRECT")` (`:195`). `tests/test_matrix_client.py::test_a_redirect_is_not_followed` |
| R6 | Low | `MatrixClient._error` (`matrix.py:207`), `_retry_after_ms` (`:103`) | `Retry-After: inf` raised `OverflowError` out of `request()`; a negative `retry_after_ms` or header made `time.sleep` raise `ValueError` (a malicious or broken homeserver could stop the sync loop or drop the hub session). | Unusable values (date, inf, nan, negative) count as absent. `tests/test_matrix_client.py::test_an_odd_retry_after_never_raises_or_waits_a_negative_time` |
| R7 | Low | `resolve_options` (`hm_matrix_bridge/__main__.py:209`, `:211`, `:214`) | A homeserver URL with `user:password@` made `requests` replace the token's header with Basic auth; a query or fragment swallowed every API path; plain `http://` to a remote host passed silently (the error text already said "must be https"). | Refused (userinfo, query, fragment); `http://` to a non-loopback host is logged as a warning. `tests/test_cli.py::test_the_homeserver_must_be_a_url`, `::test_plain_http_to_another_host_is_warned_about` |
| R8 | Low | `addressed_text` (`:663`), `MAX_UTTERANCE_CHARS` (`:101`) | A Matrix message can carry ~64 kB, 16 times Telegram's limit, and was forwarded whole: each rate-limited question could cost the hub's model that much more. | Cut to 4096 characters. `tests/test_mentions.py::test_a_very_long_question_is_cut_to_the_utterance_limit` |

CodeRabbit follow-ups on PR #2: R6 also refuses a `Retry-After` that is finite but overflows once in milliseconds (`1e306`: `int(inf)` raised out of the sync loop; Major), test case added to `tests/test_matrix_client.py::test_an_odd_retry_after_never_raises_or_waits_a_negative_time`, which now also checks the retries; R4's `split_reply` keeps the line break or space (and any indentation after it) at a split, so the parts joined are the reply (`tests/test_replies.py::test_a_long_reply_keeps_line_breaks_and_indentation_at_split_points`), adds the cut mark only when it fits the limit (`::test_the_cut_mark_never_takes_a_part_over_its_limit`), and the shutdown warning counts the reply the sender still holds (`::test_the_shutdown_warning_counts_the_reply_still_being_sent`); the SIGTERM path is tested through the real `start()` (`tests/test_cli.py::test_sigterm_during_the_sync_runs_stop_closes_the_hub_and_sends_queued_replies`); README and `docs/configuration.md` say replies ping nobody on spec v1.7 and later only (section 15).

Checked and fine: the token is only in the `Authorization` header and never in a log line, exception text, URL, the command line or the termination message (only `token_rejected`); TLS is verified (`requests` defaults; nothing sets `verify`); edits (`m.replace`, `m.new_content`), notices and non-text messages are dropped; the allow-list is by room id, so a rename changes nothing; an upgraded room's new id is not on the list, so its invite is not accepted; replies go only to rooms the bridge answers in; `_events` is bounded (1024); session ids are `matrix-<room>-<user>`; `/readyz` follows the live hub session and the last sync.

## 14. With an allow-list, `MATRIX_DM_POLICY=allow` cannot start a DM

- **Where:** `handle_invite` (`hm_matrix_bridge/__init__.py:574`).
- **Finding:** invites are accepted only to listed rooms, and a DM needs an invite, so `allow` with a list only works for two-member rooms the bot already sits in. Accepting `is_direct` invites would let anyone pull the bot into an unlisted room by flagging any invite as direct. The platform uses `auto` (deny with a list), so this is left as is and documented (FAQ).

## 15. Legacy push rules on an old homeserver

- **Where:** `deliver` (`:879`, `m.mentions: {}`).
- **Finding:** intentional mentions (spec v1.7) switch off the body-based push rules for an event that has `m.mentions`. A homeserver or client older than that still evaluates them: a reply that names someone's display name highlights them, and `@room` notifies the room if the bot's power level reaches `notifications.room` (50 by default). The bridge does not rewrite the hub's text; give the bot power level 0.

## 16. A reply-to lookup that fails is not remembered

- **Where:** `is_bot_event` (`:720`).
- **Finding:** a reply to an event the bridge cannot fetch (404, 403) is looked up again each time. A member replying to made-up event ids makes the bridge call `/event` once per message, on the sync thread. Bounded by that member's own rate limit on the homeserver; not cached because a 403 can be temporary.

## 17. Readiness trusts the read timeout

- **Where:** `ready` (`:471`), `MatrixClient.sync` (`matrix.py`).
- **Finding:** `/readyz` turns 503 on a failed sync. A homeserver that keeps a sync open by trickling bytes never fails `requests`' per-read timeout, so `/readyz` stays 200 with nothing arriving. Only a misbehaving server can do that.

## 18. Missed messages after an outage are answered late

- **Where:** `_sync_until_done` (`:418`).
- **Finding:** after a homeserver outage the next sync returns what was said meanwhile (up to 50 per room), and the bridge answers it, minutes late. Events are not filtered by `origin_server_ts`.

## 19. The homeserver can be any address

- **Where:** `resolve_options` (`hm_matrix_bridge/__main__.py`).
- **Finding:** the URL is whatever the customer enters, cluster-internal and link-local addresses included; redirects are no longer followed (R5), but the first request still goes there. The pod's egress policy must keep it off internal ranges (SUGGESTIONS.md).

## 20. `HIVEMIND_SELF_SIGNED` turns off hub TLS verification

- **Where:** `HiveMessageBusClient(self_signed=...)` (`hm_matrix_bridge/__init__.py:298`).
- **Finding:** opt-in, as in the Telegram fork; a managed pod must not set it.
