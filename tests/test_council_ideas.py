"""The council's two upgrades: a random Edge control port, and git with a secret scan that keeps secrets out."""
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.browser import EdgeController

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import git_secret_scan as scan  # noqa: E402


def test_port_comes_from_edge_and_is_never_fixed(tmp_path):
    edge = EdgeController(tmp_path / "edge-agent")
    assert edge.fixed_port == 0 and edge.port == 0 and not edge.running()      # not started: no port, not "running"
    (tmp_path / "edge-agent").mkdir()
    (tmp_path / "edge-agent" / "DevToolsActivePort").write_text("51234\n/devtools/browser/abc", encoding="utf-8")
    assert edge.port == 51234                                                   # whatever Edge picked this time
    assert EdgeController(tmp_path / "x", port=9333).port == 9333               # an explicit setting still works


@pytest.mark.parametrize("text, label", [
    # assembled at runtime so this file itself never contains anything key-shaped (the scanner checks it too)
    ("key = 'gsk_" + "A" * 40 + "'", "Groq API key"), ("-----BEGIN " + "PRIVATE KEY-----", "private key"),
    ("to" + "ken: '" + "abcdefghijklmnopqrstuvwxyz" + "123456'", "secret assignment"),
])
def test_scanner_patterns(text, label):
    import re
    assert re.search(scan.PATTERNS[label], text)


@pytest.mark.parametrize("path, forbidden", [("data/memory.json", True), (".env", True), (".env.local", True),
                                             (".env.example", False), ("data/edge-agent/Default/Cookies", True),
                                             ("backend/jarvis/tools.py", False), ("reminders.json", True)])
def test_scanner_forbidden_paths(path, forbidden):
    assert bool(scan.FORBIDDEN_PATHS.search(path)) is forbidden


@pytest.mark.skipif(not (ROOT / ".git").exists(), reason="project not under git")
def test_secrets_and_personal_data_are_ignored_by_git():
    for p in [".env", "data/memory.json", "data/edge-agent/Default/Preferences", "data/conversations/x.jsonl",
              "models/kokoro/model.onnx", ".venv/Scripts/python.exe"]:
        r = subprocess.run(["git", "check-ignore", "-q", p], cwd=ROOT)
        assert r.returncode == 0, f"{p} is not ignored by git"
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.splitlines()
    assert not [t for t in tracked if scan.FORBIDDEN_PATHS.search(t)]
