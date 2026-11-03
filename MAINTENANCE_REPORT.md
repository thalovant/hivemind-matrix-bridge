Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Adversarial review of the merged fork: eight fixes (hub outage leak and crash, crash-loop event, m.notice, reply sender, redirects, Retry-After, homeserver URL, question size).

# Maintenance report: hivemind-matrix-bridge

## 2026-11-03: review of the merged fork

**Why:** PR #1 was merged without an automated review. An adversarial read of `main` at `35bb1aea` (security first, then stability, then the contract) found eight problems; AUDIT.md has each one with its lines and test (R1-R8) and the open items (sections 14-20).

**Changes** (branch `fix/review-2026-11-03`, one commit each, oldest first)

1. A hub outage no longer stops the bridge or logs the question: `handle_message` checks the live session (`hub_live`), `forward_to_hivemind` catches what `HiveMessageBusClient.emit` raises (its `RuntimeError` text is the whole serialized message) and logs the type only.
2. One malformed event can no longer crash-loop the bridge: event ids from `m.relates_to` are used only when they are strings, each message goes through `_handle_one`, and odd sync sections are ignored.
3. Replies are `m.notice`, so two bots in a room cannot answer each other.
4. Replies are posted by one sender thread (`speak` queues, `deliver` posts; at most 100 waiting), long replies are split into messages of at most 16000 UTF-8 bytes (4 at most, then cut), `stop` drains for 5 s, and SIGTERM goes through `stop`.
5. `MatrixClient` follows no redirects (a 3xx is `MatrixError(..., "REDIRECT")`), unusable `Retry-After` values are ignored, and the homeserver URL may not carry credentials, a query or a fragment; plain `http://` to a non-loopback host is warned about.
6. A forwarded question is cut to 4096 characters.
7. Docs: AUDIT (review table, section 5 closed, sections 14-20), this entry, `docs/configuration.md`, `docs/index.md`, README, QUICK_FACTS, FAQ, SUGGESTIONS.

**Verification**

- Each new test was run before its fix and failed (for the URL checks: the CLI tests against the old `__main__.py`, 5 failed).
- `python -m pytest tests -q`: 167 passed (Python 3.12, local; 125 before).
- `ruff check --isolated --select F,E9 .`: clean.
- SIGTERM on a real process (the bridge retrying an unreachable homeserver): `shutting down`, exit status 0.
- Not run locally: the image smoke steps and `tests/integration/live_matrix.py` (CI runs both on the pull request). The live test reads the bot's replies by sender, so `m.notice` does not change what it checks.

**Transparency report**

- **AI model:** Claude Opus 5.5 (`claude-opus-5-5`), in Claude Code.
- **Actions taken:** reviewed the bridge, the Matrix client, the CLI, the image and CI against the bridge contract and the Telegram fork; checked hivemind-bus-client 1.2.12a1's `emit` and handler dispatch; wrote the failing tests, the fixes and these documents.
- **Oversight:** requested by the repository owner through the session lead as an independent review; the pull request is left for a human to review and merge.

## 2026-11-02: Thalovant fork

**Repository:** `main` created from upstream `dev` (650701e) and made the default branch; the `dev` mirror deleted. Upstream's other branches (`master`, `release-*`, feature branches) are left as they were.

**Changes** (each its own commit on `feat/managed-matrix-bridge`, oldest first)

1. The bridge rewritten as a managed multi-room bridge: `hm_matrix_bridge/matrix.py` speaks the Matrix client-server API directly (the unmaintained `matrix-client` and `python-matrixbot` are gone), `HiveMindMatrixBridge` drives the hub through `HiveMessageBusClient` (no more `HiveMindSolver`), one `/sync` loop over every room, allow-list and invites, DM policy, encrypted rooms refused, mention gating with pills, display names and replies, per-person sessions, no message text in logs, settings from the environment and `*_FILE`, `token_rejected` on a refused token, `/healthz` and `/readyz`. The `HiveMind-matrix run` CLI is replaced by `hivemind-matrix-bridge`. The pyproject license now says Apache-2.0, as the LICENSE file does.
2. The image: uid/gid 10001, `HOME=/home/bridge`, entrypoint without arguments, read-only root, no source left in it. The upstream entrypoint put the Matrix token on the command line.
3. `.github/workflows/ci.yml` as in the Telegram fork (tests on 3.12 and 3.14, ruff, image smoke, on `main` only a signed push to `ghcr.io/thalovant/hivemind-matrix-bridge`); the smoke job also runs the image read-only as uid 10001 against a stub homeserver that refuses the token. Every upstream workflow removed (PyPI alpha and stable releases, image push to upstream's registry, checks wired to `dev`).
4. `tests/integration/live_matrix.py`: the image against Conduit and a real hivemind-core hub, run by CI as the `live` job.
5. `.venv/` and `.ruff_cache/` ignored (AGENTS.md has the virtualenv made inside the clone).
6. Docs set: README, `docs/index.md`, `docs/setup.md`, `docs/configuration.md`, `docs/live-test.md`, QUICK_FACTS, FAQ, AUDIT, SUGGESTIONS, this report, AGENTS fork section; upstream's `docs/examples.md` and `docs/operator-setup.md` removed (they described the removed CLI); CHANGELOG kept with a header.

**Rationale:** run Matrix as a managed bridge workload like Telegram and Discord: secrets only in files, nothing answered outside the configured rooms, nothing forwarded that is not addressed to the bot, no message text in logs, a refused token reported to the platform, and probes it can use.

**Verification**

- `python -m pytest tests -q`: 125 passed (Python 3.12, local).
- Mutation pass: 17 behaviours gutted one at a time (allow-list, DM auto, encrypted rooms, initial sync as history, pre-join history, mention gating, invite filter, termination message, 401 handling, reply room check, readiness, log hygiene, mention stripping, same transaction on retry, reply-to-bot, handshake bound, notices); each made the suite fail.
- `ruff check --isolated --select F,E9 .`: clean.
- Image built locally with podman: `--help` answers, `id` is uid/gid 10001, refuses to start without settings naming `MATRIX_HOMESERVER` / `MATRIX_ACCESS_TOKEN`, no `/src` left. The CI read-only smoke step, run locally with podman: exit status 3, `token_rejected` in the termination log, no write error, token not logged.
- Live run (`docs/live-test.md`): Conduit 0.10.9 in a container, hivemind-core 5.3.10a1 via hivescope, the image read-only as uid 10001 with the Noise key mounted. Ready, joined only the two listed rooms, answered a mention, ignored an unmentioned message, the encrypted room (logged once) and the DM, the hub pinned the mounted Noise key, a restart reconnected and re-answered nothing, the log held no message text or secret. Every check passed.
- CI on the pull request (run 41577736217, head dd3e7d6): test 3.12 and 3.14, smoke (including the read-only `token_rejected` step), `live` (every check passed on the runner with docker) and the image build all passed. CodeQL raised one alert on test code, AUDIT.md section 13.
- Not verified: Synapse, a production homeserver, the operator rendering a Matrix bridge (it does not support one yet, see SUGGESTIONS.md).

**Transparency report**

- **AI model:** Claude Opus 5.5 (`claude-opus-5-5`), in Claude Code.
- **Actions taken:** designed and wrote the Matrix client, the bridge rewrite, the CLI, the Dockerfile, the CI workflow, the unit and live tests and these documents; ran the unit suite, the mutation pass, the image builds and smoke steps, and the live run against Conduit and a hivescope hub.
- **Oversight:** directed by the repository owner through the session lead (contract `bridge-contract.md`, network specifics, the live-homeserver requirement); no human code review yet.
