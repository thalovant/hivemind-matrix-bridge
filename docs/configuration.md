Last Edit: Claude Opus 5.5 - 2026-11-02 - Motive: Rewritten for the managed bridge: every environment setting, its default and the rules behind it.

# Configuration

Every setting comes from a flag, an environment variable or, for a secret, a file named by `<VARIABLE>_FILE`. A flag wins over its variable, the variable over its file (`resolve_options` in `hm_matrix_bridge/__main__.py`). The image passes no argument at all.

## Matrix

| Variable | Flag | Default | Notes |
| --- | --- | --- | --- |
| `MATRIX_HOMESERVER` | `--homeserver` | required | The client API base URL, `https://matrix.example.org`. No `.well-known` discovery: give the URL clients actually call. |
| `MATRIX_ACCESS_TOKEN`, `MATRIX_ACCESS_TOKEN_FILE` | `--access-token` | required | An access token of the bot account. There is no password login. Sent only in the `Authorization` header. |
| `MATRIX_USER_ID` | `--user-id` | the token's own account | `@bot:example.org`. When set, `/account/whoami` must return it, or the bridge stops as `token_rejected`. |
| `MATRIX_ALLOWED_ROOMS` | `--allowed-room` (repeatable) | none | Room ids separated by commas or spaces: `!opaque:server`, or `!opaque` for room version 12. Aliases (`#room:server`) are refused: Room settings > Advanced shows the id. |
| `MATRIX_REQUIRE_MENTION` | `--no-require-mention` | `true` | `true`/`false` (also `1/0`, `yes/no`, `on/off`). Anything else is a usage error. |
| `MATRIX_SESSION_SCOPE` | `--session-scope` | `person` | `person`: hub session `matrix-<room>-<user>`. `chat`: `matrix-<room>`. |
| `MATRIX_DM_POLICY` | `--dm-policy` | `auto` | For two-member rooms not on the allow-list: `allow`, `deny`, or `auto` = `deny` when rooms are listed, `allow` otherwise. |

## HiveMind and health

| Variable | Flag | Default |
| --- | --- | --- |
| `HIVEMIND_HOST` | `--host` | identity file (image: `ws://127.0.0.1`); a missing `ws://` is added |
| `HIVEMIND_PORT` | `--port` | `5678` |
| `HIVEMIND_ACCESS_KEY`, `HIVEMIND_ACCESS_KEY_FILE` | `--access-key` | identity file |
| `HIVEMIND_PASSWORD`, `HIVEMIND_PASSWORD_FILE` | `--password` | identity file |
| `HIVEMIND_SITE_ID` | `--site-id` | `matrix` |
| `HIVEMIND_LANG` | `--lang` | `en-us` |
| `HIVEMIND_SELF_SIGNED` | `--self-signed` | off |
| `BRIDGE_HEALTH_PORT` | `--health-port` | off |

The Noise static key is read from `~/.config/hivemind/unnamed-node_noise.key` (`/home/bridge/.config/hivemind/` in the image), the path hivemind-bus-client uses for a client built without a named identity. The hub pins that key on first contact, so it must survive restarts: keep the directory, or mount the key there read-only. The directory must stay writable: the client writes `_identity.json`, its PSK cache and an RSA key (`unnamed-node.pem`) beside the Noise key.

## Rules

Which rooms are answered (`HiveMindMatrixBridge.answer_policy`):

| Room | Allow-list set | No allow-list |
| --- | --- | --- |
| on the list | answered, whatever its size | (no list) |
| two joined members, not on the list | `MATRIX_DM_POLICY` (`auto` = refused) | `MATRIX_DM_POLICY` (`auto` = answered) |
| more members, not on the list | refused | answered |
| end-to-end encrypted | never answered, logged once | never answered, logged once |

The member count comes from the sync's room summary (`m.joined_member_count`), or from `/joined_members` when the summary has none. A room whose members cannot be counted is not treated as a DM.

Invites (`handle_invite`): accepted to a room on the list; with no list, every invite. Other invites are left pending, not declined.

Addressed to the bot (`addressed_text`), required in a room with more than two members while `MATRIX_REQUIRE_MENTION` is on:

- `m.mentions.user_ids` contains the bot (a pill);
- the bot's user id, its global display name, or its display name in that room, as a whole word in the text (case-insensitive), or a `matrix.to` link to it;
- a reply (`m.relates_to.m.in_reply_to`) to one of the bot's messages. A thread's fallback reply (`is_falling_back`) is not a reply.

The user id, display names and `matrix.to` links are removed with a following `:` or `,`, as is the quoted fallback of a reply. A message that is only the mention is not forwarded.

Never answered: notices (`m.notice`, what other bots send), edits (`m.replace`), anything but `m.text`, the bot's own messages, the initial sync, and anything in a newly joined room before the bot's join event.

Replies are `m.text` with `m.mentions: {}` (they ping nobody), in the thread when the question was in one, and only to rooms the bridge answers in, whatever room the hub's context names.

## Exit and termination

| Condition | Exit status | `/dev/termination-log` |
| --- | --- | --- |
| Missing or impossible setting | 2 (usage) | — |
| Homeserver refuses the token (401, `M_UNKNOWN_TOKEN`, `M_MISSING_TOKEN`, `M_USER_DEACTIVATED`, `M_USER_LOCKED`), at start or later | 3 | `token_rejected` |
| Token of another account than `MATRIX_USER_ID` | 3 | `token_rejected` |
| Hub refuses the bridge's credentials | 1 | — |
| Homeserver unreachable or failing | keeps retrying with a backoff up to 60 s; `/readyz` 503 | — |
