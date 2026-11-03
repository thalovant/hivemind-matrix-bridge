Last Edit: Claude Opus 5.5 - 2026-11-02 - Motive: Thalovant fork set up: proposals for the managed Matrix bridge and its platform integration.

# Suggestions: hivemind-matrix-bridge

## Platform integration (operator and API)

- **Problem:** the operator's `HiveMindBridge` knows Telegram and Discord only. Its allow-list validation takes numeric ids (`_CHAT_ID = -?[0-9]{1,20}` in `operator_lib/bridge_manager.py`), and `BridgeNetwork` has no place for the two non-secret Matrix settings.
- **Proposal:** a `matrix` network entry with `token_env=MATRIX_ACCESS_TOKEN`, `allowed_env=MATRIX_ALLOWED_ROOMS`, `mention_env=MATRIX_REQUIRE_MENTION`, `scope_env=MATRIX_SESSION_SCOPE` (`person`/`chat` map as for Telegram), plus `MATRIX_HOMESERVER` and `MATRIX_USER_ID` from new spec fields (homeserver URL, bot user id) and `MATRIX_DM_POLICY` left at `auto`. Room ids validated as `!opaque` with an optional `:server` (room version 12 ids have none). The bridge's egress policy must reach the customer's homeserver on 443, which is any host, unlike Telegram's and Discord's fixed APIs.
- **Impact:** high; required before Matrix can be offered.

## Tell people about encrypted rooms

- **Problem:** AUDIT.md section 1: an encrypted room is silent, and Element encrypts by default.
- **Proposal:** surface the bridge's "room is encrypted" log line as a bridge status the console shows ("this room is encrypted; create one with encryption off"), for example by the bridge exposing the rooms it refuses on a status endpoint next to `/readyz`.
- **Impact:** high for the first-use experience; small change in the bridge, some in the API.

## End-to-end encryption

- **Problem:** AUDIT.md section 1.
- **Proposal:** a persistent device store per bot (a small volume, or a Secret the bridge updates) and an Olm/Megolm implementation (`vodozemac` bindings or `matrix-nio[e2e]`), with device verification decided by the platform.
- **Impact:** high; large change, needs a storage decision on the platform first.

## Reply off the bus thread

- **Problem:** AUDIT.md section 5.
- **Proposal:** queue replies to a sender thread and return at once; log failures from it.
- **Impact:** medium; removes head-of-line blocking on a slow homeserver.

## A termination reason for a wrong homeserver

- **Problem:** AUDIT.md section 7.
- **Proposal:** after N failed `whoami` calls that are not network errors (404, non-JSON), stop with `homeserver_unreachable` in the termination log, once the platform maps that reason.
- **Impact:** low; clearer status for a typo.

## Run the live test against Synapse too

- **Problem:** AUDIT.md section 11.
- **Proposal:** a second homeserver in `tests/integration/live_matrix.py` (Synapse with a generated config and a registration shared secret), selected by an option, run in CI as a matrix.
- **Impact:** medium; Synapse is what most people run.

## Persist the RSA identity key

- **Problem:** AUDIT.md section 9 (all bridges).
- **Proposal:** the operator mounts `unnamed-node.pem` from the credentials Secret the way it mounts the Noise key.
- **Impact:** low; removes a hub warning per restart and keeps INTERCOM usable.

## Reproducible images

- **Problem:** AUDIT.md section 12.
- **Proposal:** pin the base image by digest and build from a constraints file generated in CI; Renovate updates both.
- **Impact:** medium; makes a rollback mean something.
