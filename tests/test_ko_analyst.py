"""shturman-ko-analyst: guard hook, idempotent ledger and agent definition (no network, no LLM)."""
import json
import pathlib
import re
import shutil
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
AGENT = ROOT / ".claude" / "agents" / "shturman-ko-analyst.md"


def test_guard_and_ledger_unit(tmp_path):
    node = shutil.which("node")
    assert node, "node is required"
    r = subprocess.run([node, str(ROOT / "tests/js/ko_analyst_unit.js"), str(tmp_path)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1])["ok"] is True


def test_agent_definition_is_valid_and_restricted():
    text = AGENT.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "frontmatter missing"
    fm = m.group(1)
    assert re.search(r"^name: shturman-ko-analyst$", fm, re.M)
    assert re.search(r"^description: ", fm, re.M)
    tools = re.search(r"^tools: (.*)$", fm, re.M).group(1)
    assert "Edit" not in tools.split(", ") and "WebFetch" not in tools
    assert "ko-analyst-guard.js" in fm, "guard hook must be wired in the agent frontmatter"
    for rule in ("api.telegram.org", "только читается", "requires_approval", "dedupe_key", "evidence"):
        assert rule in text, rule


def test_working_dir_is_gitignored():
    assert ".ko-analyst/" in (ROOT / ".gitignore").read_text(encoding="utf-8")
