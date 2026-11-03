Last Edit: Claude Opus 5.5 - 2026-11-02 - Motive: Thalovant fork set up: managed multi-room Matrix bridge, Thalovant CI and signed images, docs set added.

# Quick facts: hivemind-matrix-bridge

| Key | Value |
| --- | --- |
| Package (distribution) | `HiveMind-matrix-bridge` |
| Import name | `hm_matrix_bridge` |
| Version | `0.3.0a1` (`hm_matrix_bridge/version.py`) |
| Console entry point | `hivemind-matrix-bridge` -> `hm_matrix_bridge.__main__:main` |
| Main class | `hm_matrix_bridge.HiveMindMatrixBridge` |
| Matrix client | `hm_matrix_bridge.matrix.MatrixClient` (client-server API v3 over `requests`; no Matrix SDK) |
| Health server | `hm_matrix_bridge.health.HealthServer` (`/healthz`, `/readyz`) |
| Image | `ghcr.io/thalovant/hivemind-matrix-bridge` (`:sha-<commit>` and `:latest`, signed by digest with cosign keyless) |
| Image user | `bridge`, uid/gid 10001, `HOME=/home/bridge`; writes only `/home/bridge/.config/hivemind` and `/tmp` |
| Noise key path | `/home/bridge/.config/hivemind/unnamed-node_noise.key` (hex X25519 private key) |
| Default branch | `main` (upstream: `JarbasHiveMind/HiveMind-matrix-bridge`, branch `dev`) |
| CI | `.github/workflows/ci.yml`: tests on Python 3.12 and 3.14, ruff `F,E9`, image smoke tests (incl. read-only root and `token_rejected`), live run, publish on `main` |
| Tests | `tests/`, 125 tests (`python -m pytest tests -q`); live run `tests/integration/live_matrix.py` (Conduit 0.10.9 + hivemind-core) |
| HiveMind message types | sends `recognizer_loop:utterance`; handles `speak`, `hive.complete_intent_failure` |
| Hub session id | `matrix-<room_id>-<user_id>` (`person`, default) or `matrix-<room_id>` (`chat`) |
| Utterance context | `room_id`, `thread_id` (in a thread), `user.matrix_user_id`, `session.session_id` |
| Termination | refused token: `token_rejected` in `/dev/termination-log`, exit status 3; hub refusal: exit status 1 |
| Encryption | not supported: encrypted rooms are never answered, logged once |
| License | Apache-2.0 |

## Settings

| Flag | Environment variable | Default |
| --- | --- | --- |
| `--homeserver` | `MATRIX_HOMESERVER` | required |
| `--access-token` | `MATRIX_ACCESS_TOKEN` (or `MATRIX_ACCESS_TOKEN_FILE`) | required |
| `--user-id` | `MATRIX_USER_ID` | the token's account |
| `--allowed-room` | `MATRIX_ALLOWED_ROOMS` | none (every room, every invite) |
| `--no-require-mention` | `MATRIX_REQUIRE_MENTION` | `true` |
| `--session-scope` | `MATRIX_SESSION_SCOPE` (`person` or `chat`) | `person` |
| `--dm-policy` | `MATRIX_DM_POLICY` (`auto`, `allow`, `deny`) | `auto` |
| `--health-port` | `BRIDGE_HEALTH_PORT` | off |
| `--access-key` | `HIVEMIND_ACCESS_KEY` (or `_FILE`) | identity file |
| `--password` | `HIVEMIND_PASSWORD` (or `_FILE`) | identity file |
| `--host`, `--port` | `HIVEMIND_HOST`, `HIVEMIND_PORT` | identity file (image `ws://127.0.0.1`), `5678` |
| `--site-id`, `--lang`, `--self-signed` | `HIVEMIND_SITE_ID`, `HIVEMIND_LANG`, `HIVEMIND_SELF_SIGNED` | `matrix`, `en-us`, off |
