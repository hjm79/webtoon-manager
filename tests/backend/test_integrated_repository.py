"""A single checkout builds both components without changing persistence boundaries."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
compose = yaml.safe_load((ROOT / "compose.integrated.yml").read_text())["services"]
manager, viewer = compose["webtoon-manager"], compose["webtoon-server"]
assert manager["build"] == {"context": ".", "dockerfile": "Dockerfile.fork"}
assert viewer["build"] == {"context": "./viewer", "dockerfile": "Dockerfile.fork"}
assert not viewer.get("ports"), "The reader must remain private behind the manager"
assert manager["environment"]["VIEWER_INTERNAL_URL"] == "http://webtoon-server:8000"
assert manager["environment"]["VIEWER_PUBLIC_PATH"] == viewer["environment"]["BASE_PATH"] == "/viewer"
assert viewer["environment"]["PUBLIC_BASE_URL"].endswith("/viewer")
assert viewer["environment"]["INTEGRATION_TOKEN"].startswith("${VIEWER_INTEGRATION_TOKEN:?")
libraries = [mount for mount in viewer["volumes"] if ":/library/" in mount]
assert len(libraries) == 3 and all(mount.endswith(":ro") for mount in libraries)
assert any(mount.endswith(":/data") for mount in manager["volumes"])
assert any(mount.endswith(":/data") for mount in viewer["volumes"])
assert (ROOT / "viewer/app/main.py").is_file() and (ROOT / "viewer/static/reader-progress.js").is_file()
assert (ROOT / "viewer/LICENSE").is_file() and not (ROOT / "viewer/.git").exists()
assert not (ROOT / ".gitmodules").exists(), "Viewer code must not depend on an external checkout"
for dockerfile in (ROOT / "Dockerfile.fork", ROOT / "viewer/Dockerfile.fork"):
    assert "ghcr.io/hjm79/webtoon-server" not in dockerfile.read_text()
workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
assert workflow["jobs"]["viewer"]["defaults"]["run"]["working-directory"] == "viewer"
print("Single-checkout build, private viewer, isolated tests and original data mounts verified")
