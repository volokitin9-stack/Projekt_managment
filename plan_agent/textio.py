"""Text files of the agent: always UTF-8, written atomically.

Python on Windows uses the system code page (cp1251 for Russian) when no
encoding is given. Earlier versions therefore wrote some files in cp1251
(e.g. inbox ``meta.json`` with Cyrillic file names) and failed to write
reports containing characters cp1251 cannot represent (→, ≈, ✓, …).

* ``write_text``/``write_json``: UTF-8, temp file + fsync + os.replace.
* ``read_text``/``read_json``: strict UTF-8 (a UTF-8 BOM is accepted).
* ``read_legacy_json``: for the KNOWN legacy metadata files only - strict
  UTF-8 first; if that fails, strict cp1251; the result must be valid JSON
  (and pass an optional validator). The original bytes are kept as a backup,
  the file is rewritten as UTF-8 atomically and the migration is logged.
  Undecodable or invalid content raises ``MetadataError``; nothing is
  ignored, replaced or discarded.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("plan_agent.textio")

UTF8 = "utf-8"
LEGACY_ENCODING = "cp1251"


class MetadataError(Exception):
    """A metadata file cannot be read as UTF-8 or legacy cp1251 JSON."""

    def __init__(self, path: Path, message: str):
        super().__init__(f"{path}: {message}")
        self.path = Path(path)


def write_bytes_atomic(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_text(path: Path, text: str) -> None:
    write_bytes_atomic(path, text.encode(UTF8))


def write_json(path: Path, obj: Any, **kw) -> None:
    kw.setdefault("ensure_ascii", False)
    kw.setdefault("indent", 2)
    write_text(path, json.dumps(obj, **kw))


def read_text(path: Path) -> str:
    """Strict UTF-8 (optional BOM). Raises UnicodeDecodeError on anything else."""
    return Path(path).read_bytes().decode("utf-8-sig")


def read_json(path: Path) -> Any:
    return json.loads(read_text(path))


def read_legacy_json(path: Path, migration_log: Optional[Path] = None,
                     validate: Optional[Callable[[Any], Optional[str]]] = None) -> Any:
    """Read a known legacy metadata JSON file; migrate cp1251 to UTF-8 if needed.

    validate(obj) returns None when the content is acceptable, else a reason."""
    path = Path(path)
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
        encoding = UTF8
    except UnicodeDecodeError as utf_err:
        try:
            text = raw.decode(LEGACY_ENCODING)        # strict: undefined bytes raise
            encoding = LEGACY_ENCODING
        except UnicodeDecodeError as legacy_err:
            raise MetadataError(path, f"neither UTF-8 ({utf_err.reason} at byte {utf_err.start}) nor "
                                      f"{LEGACY_ENCODING} ({legacy_err.reason}); the file was not changed") from None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MetadataError(path, f"not valid JSON when decoded as {encoding} ({exc.msg}, line {exc.lineno}); "
                                  "the file was not changed") from None
    problem = validate(obj) if validate else None
    if problem:
        raise MetadataError(path, f"unexpected content when decoded as {encoding}: {problem}; the file was not changed")
    if encoding == UTF8:
        return obj

    # ---- legacy cp1251: keep the original, rewrite as UTF-8, record the migration
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.{LEGACY_ENCODING}-{stamp}.bak")
    n = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.{LEGACY_ENCODING}-{stamp}-{n}.bak")
        n += 1
    write_bytes_atomic(backup, raw)
    new = json.dumps(obj, ensure_ascii=False, indent=2).encode(UTF8)
    if json.loads(new.decode(UTF8)) != obj:                 # round-trip check before replacing
        raise MetadataError(path, "UTF-8 re-encoding did not round-trip; the file was not changed")
    write_bytes_atomic(path, new)
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "file": str(path),
             "from": LEGACY_ENCODING, "to": UTF8, "backup": str(backup), "bytes_before": len(raw),
             "bytes_after": len(new)}
    log.warning("legacy %s metadata migrated to UTF-8: %s (backup %s)", LEGACY_ENCODING, path, backup.name)
    if migration_log is not None:
        migration_log.parent.mkdir(parents=True, exist_ok=True)
        with open(migration_log, "a", encoding=UTF8) as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return obj
