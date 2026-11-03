FROM python:3.14-slim

# Install from a copy of the package only, then drop the copy: nothing but
# the installed bridge is left in the image.
COPY pyproject.toml README.md LICENSE /src/
COPY hm_matrix_bridge /src/hm_matrix_bridge

# The floors name alphas (hivemind-bus-client>=1.0.16a1, ovos-bus-client>=2.9.1a1);
# upgrade the client first so a cached base layer never holds an older one.
RUN pip install --no-cache-dir --upgrade "hivemind-bus-client>=1.0.16a1" \
    && pip install --no-cache-dir /src \
    && rm -rf /src

# The bridge reads every setting from the environment (MATRIX_HOMESERVER,
# MATRIX_ACCESS_TOKEN_FILE, HIVEMIND_ACCESS_KEY_FILE, ...), or a secret from
# the file named by <VARIABLE>_FILE, so no secret is ever passed on the
# command line where other processes can read it. Nothing secret is baked
# into the image.
ENV HOME=/home/bridge \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HIVEMIND_HOST=ws://127.0.0.1 \
    HIVEMIND_PORT=5678 \
    HIVEMIND_SITE_ID=matrix \
    HIVEMIND_LANG=en-us

# An unprivileged user. Its ~/.config/hivemind holds the HiveMind identity and
# the Noise static key (unnamed-node_noise.key), which the hub pins: mount a
# volume there, or the key from a secret, so it survives restarts. That
# directory and /tmp are the only places the bridge writes, so the root
# filesystem can be read-only.
RUN groupadd --gid 10001 bridge \
    && useradd --create-home --uid 10001 --gid 10001 bridge \
    && mkdir -p /home/bridge/.config/hivemind \
    && chown -R bridge:bridge /home/bridge
USER bridge
WORKDIR /home/bridge

ENTRYPOINT ["hivemind-matrix-bridge"]
