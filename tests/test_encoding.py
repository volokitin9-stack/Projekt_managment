"""Encoding regression tests: UTF-8 everywhere, migration of legacy cp1251 metadata
written by earlier versions on Windows, Cyrillic names and Unicode symbols."""
import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import make_fixtures
from conftest import ROOT
from plan_agent.ingestion import Inbox
from plan_agent.textio import MetadataError, read_legacy_json, write_json, write_text

HARNESS = ROOT / "tests" / "fixtures" / "win_cp1251_harness.py"
NAME = "AfM СIS+OEM 2026.xlsx"                      # Cyrillic «С» in «СIS»
META = {"sha256": "ab" * 32, "deliveries": [
    {"channel": "watch", "original_name": NAME, "received_at": "2026-11-10T04:00:00+00:00",
     "sender": "Конструкторский отдел", "note": "план на ноябрь"}]}


def legacy_meta(tmp_path, obj=META):
    p = tmp_path / "inbox" / "x" / "meta.json"
    p.parent.mkdir(parents=True)
    p.write_bytes(json.dumps(obj, ensure_ascii=False, indent=2).encode("cp1251"))
    return p


def backups(p):
    return sorted(p.parent.glob("meta.json.cp1251-*.bak"))


# ------------------------------------------------------------------ metadata reading / migration
def test_utf8_metadata_is_read_without_migration(tmp_path):
    p = tmp_path / "meta.json"
    write_json(p, META)
    log = tmp_path / "migrations.jsonl"
    assert read_legacy_json(p, log) == META
    assert not backups(p) and not log.exists()
    p.write_bytes(b"\xef\xbb\xbf" + p.read_bytes())          # UTF-8 with BOM (Notepad) is UTF-8 too
    assert read_legacy_json(p, log) == META and not log.exists()


def test_legacy_cp1251_metadata_is_migrated_with_backup(tmp_path):
    p = legacy_meta(tmp_path)
    original = p.read_bytes()
    with pytest.raises(UnicodeDecodeError):
        original.decode("utf-8")                              # the user's error
    log = tmp_path / "encoding_migrations.jsonl"
    assert read_legacy_json(p, log) == META
    assert p.read_bytes().decode("utf-8") and json.loads(p.read_bytes().decode("utf-8")) == META
    (bak,) = backups(p)
    assert bak.read_bytes() == original                       # original kept byte for byte
    entry = json.loads(log.read_text(encoding="utf-8"))
    assert entry["from"] == "cp1251" and entry["to"] == "utf-8" and entry["backup"] == str(bak)
    assert not list(p.parent.glob(".meta.json.*.tmp"))        # no temp files left


def test_repeated_processing_of_migrated_file(tmp_path):
    p = legacy_meta(tmp_path)
    log = tmp_path / "m.jsonl"
    read_legacy_json(p, log)
    after = p.read_bytes()
    for _ in range(3):
        assert read_legacy_json(p, log) == META
    assert p.read_bytes() == after and len(backups(p)) == 1
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1


def test_unicode_symbols_survive(tmp_path):
    obj = {"deliveries": [{"original_name": "План → факт ≈ 100 % ✓ «СIS» №5 😀.xlsx"}]}
    p = tmp_path / "meta.json"
    write_json(p, obj)
    assert read_legacy_json(p) == obj
    md = tmp_path / "отчёт.md"
    write_text(md, "Приоритет 1 → Высокий; ≈ 185 ч ✓")
    assert md.read_bytes().decode("utf-8") == "Приоритет 1 → Высокий; ≈ 185 ч ✓"


@pytest.mark.parametrize("raw,what", [
    (b'{"deliveries": [ ', "not valid JSON"),                                        # truncated
    ("{\"deliveries\": [{\"original_name\": \"План\"}]".encode("cp1251"), "not valid JSON"),
    (b"\xff\xfe\x98\x98 not text", "neither UTF-8"),                                 # 0x98 is undefined in cp1251
    ('["Ноябрь"]'.encode("cp1251"), "expected a JSON object"),                         # wrong structure
    ('{"sha256": "Ноябрь"}'.encode("cp1251"), "missing 'deliveries'"),
])
def test_invalid_metadata_is_reported_and_left_untouched(tmp_path, raw, what):
    p = tmp_path / "inbox" / "y" / "meta.json"
    p.parent.mkdir(parents=True)
    p.write_bytes(raw)
    inbox = Inbox(tmp_path)
    with pytest.raises(MetadataError) as exc:
        inbox.read_meta(p)
    assert what in str(exc.value) and str(p) in str(exc.value)
    assert p.read_bytes() == raw and not backups(p)            # nothing changed, nothing discarded
    errors = []
    assert inbox.list(errors) == [] and len(errors) == 1       # reported, not silently skipped


def test_migration_is_atomic(tmp_path, monkeypatch):
    p = legacy_meta(tmp_path)
    original = p.read_bytes()
    real_replace = os.replace
    calls = {"n": 0}

    def failing_replace(src, dst):
        calls["n"] += 1
        if str(dst) == str(p):                                 # the backup succeeds, the rewrite fails
            raise OSError("disk full")
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError):
        read_legacy_json(p, tmp_path / "m.jsonl")
    assert p.read_bytes() == original                          # original intact
    assert not list(p.parent.glob(".meta.json.*.tmp"))
    monkeypatch.setattr(os, "replace", real_replace)
    assert read_legacy_json(p, tmp_path / "m.jsonl") == META   # next run migrates cleanly


def test_cyrillic_file_names_in_the_inbox(tmp_path):
    src_dir = tmp_path / "Отделы" / "Конструкторский" / "1. Планы и отчеты"
    src_dir.mkdir(parents=True)
    src = src_dir / NAME
    make_fixtures.make_monthly_workbook(src, 1)
    inbox = Inbox(tmp_path / "state")
    stored, meta = inbox.store(src, "watch", NAME, {"sender": "Иванов И.И."})
    stored2, meta2 = inbox.store(src, "manual", NAME)
    assert [d["original_name"] for d in meta2["deliveries"]] == [NAME, NAME]
    raw = (stored.parent / "meta.json").read_bytes()
    assert NAME in raw.decode("utf-8") and "Иванов" in raw.decode("utf-8")


def test_receive_with_unreadable_metadata_stops_with_a_clear_error(tmp_path, make_agent):
    agent = make_agent(state="st")
    wb = tmp_path / NAME
    make_fixtures.make_monthly_workbook(wb, 1)
    stored, _ = agent.inbox.store(wb, "cli", NAME)
    meta = stored.parent / "meta.json"
    meta.write_bytes(b"\x98\x98 broken")
    out = agent.receive(wb, "cli", NAME, "dry_run", target_month="2026-11")
    assert out.status == "Failed" and "metadata file cannot be read" in out.report["fatal_error"]
    assert meta.read_bytes() == b"\x98\x98 broken"


# ------------------------------------------------------------------ end to end, Windows code page
def run_agent_windows(state, *args):
    env = dict(os.environ, PLAN_AGENT_STATE_DIR=str(state), PYTHONIOENCODING="cp1251")
    for k in ("PYTHONUTF8", "PM_API_URL", "PM_AGENT_API_KEY"):
        env.pop(k, None)
    return subprocess.run([sys.executable, str(HARNESS), str(ROOT)] + list(args), capture_output=True,
                          env=env, timeout=300)


def test_agent_on_windows_code_page_reads_legacy_metadata_and_writes_utf8(tmp_path):
    wb = tmp_path / "Планы" / NAME
    wb.parent.mkdir()
    make_fixtures.make_monthly_workbook(wb, 1)
    state = tmp_path / "state"
    # an earlier version left cp1251 metadata for this very file
    from plan_agent.pipeline import sha256_file
    sha = sha256_file(wb)
    legacy = state / "inbox" / sha / "meta.json"
    legacy.parent.mkdir(parents=True)
    old = {"sha256": sha, "deliveries": [{"channel": "cli", "original_name": NAME, "received_at": "2026-11-09T10:00:00+00:00"}]}
    legacy.write_bytes(json.dumps(old, ensure_ascii=False, indent=2).encode("cp1251"))

    r = run_agent_windows(state, "analyze", str(wb), "--month", "2026-11", "--name", NAME, "--json")
    assert r.returncode in (0, 3), r.stderr.decode("utf-8", "backslashreplace")
    out = json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])     # stdout is UTF-8 even on cp1251
    assert out["status"] == "Approval required (dry run)" and out["counts"]["new"] > 0
    md = Path(out["analysis_path"]).read_bytes().decode("utf-8")
    assert "→" in md and "Ноябрь" in md and NAME in md
    meta = json.loads(legacy.read_bytes().decode("utf-8"))
    assert meta["deliveries"][0] == old["deliveries"][0] and len(meta["deliveries"]) == 2
    assert len(list(legacy.parent.glob("meta.json.cp1251-*.bak"))) == 1
    # every text file the agent wrote is strict UTF-8
    for p in state.rglob("*"):
        if p.is_file() and p.name != "file" and not p.name.endswith(".bak"):
            p.read_bytes().decode("utf-8")
    # a second run: no new migration, still works
    r2 = run_agent_windows(state, "analyze", str(wb), "--month", "2026-11", "--json")
    assert r2.returncode in (0, 3), r2.stderr.decode("utf-8", "backslashreplace")
    assert len(list(legacy.parent.glob("meta.json.cp1251-*.bak"))) == 1
    assert len((state / "encoding_migrations.jsonl").read_text(encoding="utf-8").splitlines()) == 1


# ------------------------------------------------------------------ guard against regressions
TEXT_CALLS = {"read_text", "write_text"}


def test_no_text_io_without_explicit_encoding_in_plan_agent():
    problems = []
    for f in (ROOT / "plan_agent").rglob("*.py"):
        if f.name == "textio.py":
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None
            kws = {k.arg for k in node.keywords}
            if name in TEXT_CALLS and isinstance(fn, ast.Attribute) and "encoding" not in kws:
                problems.append(f"{f.relative_to(ROOT)}:{node.lineno} {name}() without encoding")
            if name == "open" and isinstance(fn, ast.Name):
                mode = node.args[1].value if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) else \
                    next((k.value.value for k in node.keywords if k.arg == "mode" and isinstance(k.value, ast.Constant)), "r")
                if "b" not in mode and "encoding" not in kws:
                    problems.append(f"{f.relative_to(ROOT)}:{node.lineno} open(mode={mode!r}) without encoding")
    assert not problems, "\n".join(problems)


def test_server_starts_the_agent_in_utf8_mode():
    src = (ROOT / "server" / "routes" / "planImports.js").read_text(encoding="utf-8")
    assert 'PYTHONUTF8: "1"' in src and 'PYTHONIOENCODING: "utf-8"' in src
