Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Platform integration and the RSA identity done in the operator; the homeserver URL check is in the platform API's connector catalog change.

# Suggestions: hivemind-matrix-bridge

## Platform integration (operator and API) (done in the operator, 2026-11-03)

- **Problem:** the operator's `HiveMindBridge` knew Telegram and Discord only: numeric chat ids and no place for the two non-secret Matrix settings.
- **Done:** hivemind-k8s-operator#121 keeps no network table. The platform renders each bridge's `env` (`MATRIX_HOMESERVER`, `MATRIX_USER_ID` when set, `MATRIX_REQUIRE_MENTION`, `MATRIX_SESSION_SCOPE`, `MATRIX_DM_POLICY=deny`), `secretEnv` (`MATRIX_ACCESS_TOKEN`, mounted as `MATRIX_ACCESS_TOKEN_FILE`) and `allowedEnv` (`MATRIX_ALLOWED_ROOMS`), and the operator applies them within guard rails; allowed entries are any id without spaces or commas, at most 50. The Matrix entry in the platform's connector catalog (room ids `^![A-Za-z0-9._=/+-]+(:[A-Za-z0-9.:-]+)?$`, so room version 12 ids without a server part pass; egress 443 only) is in review in thalovant-api#467, hidden until its first version is published.

## Tell people about encrypted rooms

- **Problem:** AUDIT.md section 1: an encrypted room is silent, and Element encrypts by default.
- **Proposal:** surface the bridge's "room is encrypted" log line as a bridge status the console shows ("this room is encrypted; create one with encryption off"), for example by the bridge exposing the rooms it refuses on a status endpoint next to `/readyz`.
- **Impact:** high for the first-use experience; small change in the bridge, some in the API.

## End-to-end encryption

- **Problem:** AUDIT.md section 1.
- **Proposal:** a persistent device store per bot (a small volume, or a Secret the bridge updates) and an Olm/Megolm implementation (`vodozemac` bindings or `matrix-nio[e2e]`), with device verification decided by the platform.
- **Impact:** high; large change, needs a storage decision on the platform first.

## Reply off the bus thread (done 2026-11-03)

- **Problem:** AUDIT.md section 5.
- **Done:** `speak` queues, one sender thread posts (AUDIT.md R4).

## Check the homeserver URL where it is entered (in review in the platform API, 2026-11-03)

- **Problem:** AUDIT.md section 19 and R7: the homeserver URL is whatever the customer types. The bridge refuses credentials, queries and fragments and warns on plain `http://`, but only once the pod starts, and it cannot tell a public homeserver from an internal address.
- **Proposal:** the platform API accepts only `https://` URLs with a public host name, and the bridge's egress policy allows 443 to public addresses only (no cluster, node, link-local or metadata ranges).
- **State:** the egress policy already allows TCP 443 to public addresses only. The API check (https, port 443 only, a host name that resolves to public addresses only, checked on create and on every settings edit) is part of thalovant-api#467, in review.
- **Impact:** high for a managed service; small change in the API and the policy.

## A termination reason for a wrong homeserver

- **Problem:** AUDIT.md section 7.
- **Proposal:** after N failed `whoami` calls that are not network errors (404, non-JSON), stop with `homeserver_unreachable` in the termination log, once the platform maps that reason.
- **Impact:** low; clearer status for a typo.

## Run the live test against Synapse too

- **Problem:** AUDIT.md section 11.
- **Proposal:** a second homeserver in `tests/integration/live_matrix.py` (Synapse with a generated config and a registration shared secret), selected by an option, run in CI as a matrix.
- **Impact:** medium; Synapse is what most people run.

## Persist the RSA identity key (done in the operator, 2026-11-03)

- **Problem:** AUDIT.md section 9 (all bridges).
- **Done:** hivemind-k8s-operator#122 mounts `unnamed-node.pem` read-only from the credentials Secret (`rsaKey`), the way it mounts the Noise key.

## Reproducible images

- **Problem:** AUDIT.md section 12.
- **Proposal:** pin the base image by digest and build from a constraints file generated in CI; Renovate updates both.
- **Impact:** medium; makes a rollback mean something.
