"""What the image promises a container platform, read from the Dockerfile.

CI's smoke job builds and runs the image; this keeps the promises from
drifting between runs: the entrypoint is the bridge with no argument (so no
secret in it), the user is uid 10001 with its home at /home/bridge.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()


def test_the_entrypoint_is_the_bridge_with_no_argument():
    entrypoint = [line for line in DOCKERFILE if line.startswith("ENTRYPOINT")]
    assert entrypoint == ['ENTRYPOINT ["hivemind-matrix-bridge"]']
    assert not any(line.startswith("CMD") for line in DOCKERFILE)
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'hivemind-matrix-bridge = "hm_matrix_bridge.__main__:main"' in pyproject


def test_the_image_runs_as_bridge_uid_10001_with_its_home():
    assert "USER bridge" in DOCKERFILE
    text = "\n".join(DOCKERFILE)
    assert "--uid 10001" in text and "--gid 10001" in text
    assert "HOME=/home/bridge" in text
    assert "mkdir -p /home/bridge/.config/hivemind" in text
