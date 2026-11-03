Last Edit: Claude Opus 5.5 - 2026-11-02 - Motive: Live test against Conduit and a real hivemind-core hub added; how to run it and what it proves.

# Live test: a real homeserver and a real hub

`tests/integration/live_matrix.py` runs the bridge **image** against:

- **Conduit 0.10.9** (`docker.io/matrixconduit/matrix-conduit`, pinned by digest in `CONDUIT_IMAGE`), started in a container on the host network with open registration;
- a real **hivemind-core** hub, booted in the test process by hivescope (`TopologyBuilder`), whose agent answers every utterance with `speak` "answer #N";
- the bridge container laid out as the operator renders the pod: `--read-only`, `--user 10001:10001`, `--cap-drop ALL`, `no-new-privileges`, tmpfs at `/tmp` and `/home/bridge/.config/hivemind`, the Noise key mounted read-only at `unnamed-node_noise.key`, every secret a file under `/var/run/thalovant`, every setting from the environment.

## Scenario

alice, bob and the bot `@hivebot:bridge.test` (display name HiveBot). alice creates `allowed` (alice, bob, bot: three members), `encrypted` (same, with `m.room.encryption`), `unlisted`, a DM invite, and a DM the bot already joined. `MATRIX_ALLOWED_ROOMS` lists `allowed` and `encrypted`; `MATRIX_DM_POLICY` is left at `auto`.

| Check | How |
| --- | --- |
| uid 10001, read-only root | `id` and two `touch` attempts in the running container |
| ready | `/readyz` 200, `/healthz` 200 |
| joins only the listed rooms | the bot's `/joined_rooms`; its membership in `unlisted` and the DM invite stays `invite` |
| answers a mention | alice: `HiveBot: what time is it` with a pill; the hub receives `what time is it` in session `matrix-<room>-@alice:bridge.test` (the hub prefixes its own client namespace) and the bot posts `answer #1` |
| ignores an unmentioned message, the encrypted room, the DM | bob's plain message in `allowed`, alice's and bob's in `encrypted`, alice's in both DMs; then a mentioned barrier question. The hub must have received only the two mentioned questions, and the bot posted nothing in `encrypted` or the DM |
| encrypted room logged once | one log line per run naming the room |
| Noise key from the mount | the hub's pinned `noise_pubkey` equals the mounted key's public half |
| restart | a fresh container (empty identity directory, same key) is ready again, answers a new question and re-answers no history |
| clean log | no message text, no secret or token, no read-only or permission error, no traceback |

Ignored messages are proven with that barrier, not a sleep.

## Run it

```bash
pip install -r tests/integration/requirements.txt   # hivescope, hivemind-core and plugins
docker build -t hivemind-matrix-bridge:it .
python tests/integration/live_matrix.py --image hivemind-matrix-bridge:it --transcript live.txt
```

`CONTAINER_ENGINE` (`docker` or `podman`; default podman when installed) and `CONTAINER_ENGINE_ARGS` (for example `--cgroup-manager=cgroupfs` for rootless podman without a systemd user session) choose how containers run. On an SELinux host the bind mounts are relabelled (`:z`). `--keep` leaves the containers running. Exit status 0 means every check passed; the bridge log is saved next to the secrets in the run's temporary directory.

CI runs it as the `live` job of `.github/workflows/ci.yml`, on pull requests and on `main`. It does not gate publishing. On the pull request's first run (GitHub runner, docker) every check passed in about 40 seconds.

## Transcript (2026-11-02, podman 5, SELinux enforcing)

```
[ok]   homeserver Conduit 0.10.9 at http://127.0.0.1:49893 (server name bridge.test)
[ok]   registered @hivebot:bridge.test (HiveBot), @alice:bridge.test, @bob:bridge.test
[ok]   rooms: allowed !v6Ek7ywR7ZmKQY5saX9u0D3fUmFsA1KYeoQk2i0rkO4 (alice, bob, bot invited), encrypted !2BkQPvOMT4FXis9IwZ6-MKz1GwAYE99C9iCMpXYCcas (m.megolm), unlisted !-YvwqU1C_ZjSOioJw-LdmbAE_yS24T4MLPiFuzEXL9s, DM invite !U6lZUU6CCsj3JIqadRNyX6QYnKxjdPbPsbScLJajhzA, DM already joined !...
[ok]   HiveMind hub (hivemind-core via hivescope) at ws://127.0.0.1:34657; client allowed recognizer_loop:utterance only
[ok]   bridge container it-bridge-686fa40c: uid=10001(bridge) gid=10001(bridge) groups=10001(bridge); root filesystem read-only
[ok]   /readyz 200 after 1.5s, /healthz 200
[ok]   accepted only the listed invites (allowed, encrypted); unlisted and DM invite left pending: {'!-YvwqU1C_...': 'invite', '!U6lZUU6C...': 'invite'}
[ok]   mention answered in allowed: alice asked 'what time is it', hub got it in session 66490d861a65c462:matrix-!v6Ek7ywR...-@alice:bridge.test, bot replied ['answer #1']
[ok]   ignored: an unmentioned message in allowed, both messages in encrypted, the DM (dm policy auto with an allow-list); the hub received only the two mentioned questions
[ok]   the bot said nothing in the encrypted room or the DM; second answer in allowed
[ok]   hub pinned the bridge's Noise key from the mounted file (b876abb701f9c3f5...)
[info] ~/.config/hivemind in the container: ['_identity.json', 'unnamed-node.pem', 'unnamed-node_noise.key', 'unnamed-node_noise_psks.json']
[ok]   restarted in a fresh container: /readyz 200 after 1.3s (the hub accepted the same pinned Noise key)
[ok]   after the restart: answered the new question, re-answered nothing from history
[ok]   the encrypted room was logged once per run (two runs): room !2BkQPvOM... is end-to-end encrypted; encrypted rooms are not supported, so the bridge will not answer there
[ok]   bridge log (58 lines): no message text, no secret, no filesystem error, no traceback
[pass] every check passed
```

Conduit gives rooms version-12 style ids with no `:server` part (`!v6Ek7...`); the bridge's room id check accepts both forms.
