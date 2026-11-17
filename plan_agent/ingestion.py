"""File ingestion: every delivery channel (CLI, watched folder, HTTP intake,
schedule, reprocess) stores the file in the content-addressed inbox and then
calls the same processing pipeline.  File names are kept only as metadata."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .pipeline import sha256_file
from .textio import MetadataError, read_legacy_json, write_json

MAX_FILE_BYTES = 50 * 1024 * 1024


def _validate_meta(obj) -> Optional[str]:
    if not isinstance(obj, dict):
        return "expected a JSON object"
    if not isinstance(obj.get("deliveries"), list):
        return "missing 'deliveries' list"
    return None


class Inbox:
    def __init__(self, state_dir: Path):
        self.root = Path(state_dir) / "inbox"
        self.root.mkdir(parents=True, exist_ok=True)
        self.migration_log = Path(state_dir) / "encoding_migrations.jsonl"

    def read_meta(self, meta_path: Path) -> dict:
        """meta.json in UTF-8; a legacy cp1251 file is migrated (backup kept). Raises MetadataError."""
        return read_legacy_json(meta_path, self.migration_log, _validate_meta)

    def write_meta(self, meta_path: Path, meta: dict) -> None:
        write_json(meta_path, meta)

    def store(self, src: Path, channel: str, original_name: Optional[str] = None,
              metadata: Optional[dict] = None) -> tuple[Path, dict]:
        src = Path(src)
        if src.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"file larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
        sha = sha256_file(src)
        d = self.root / sha
        d.mkdir(exist_ok=True)
        dest = d / "file"
        if not dest.exists():
            shutil.copyfile(src, dest)
        meta_path = d / "meta.json"
        meta = self.read_meta(meta_path) if meta_path.exists() else {"sha256": sha, "deliveries": []}
        meta["deliveries"].append({
            "channel": channel,
            "original_name": original_name or src.name,
            "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **(metadata or {}),
        })
        self.write_meta(meta_path, meta)
        return dest, meta

    def get(self, sha: str) -> tuple[Path, dict]:
        d = self.root / sha
        if not (d / "file").exists():
            matches = [p for p in self.root.iterdir() if p.name.startswith(sha)]
            if len(matches) != 1:
                raise FileNotFoundError(f"no unique stored file for {sha!r}")
            d = matches[0]
        return d / "file", self.read_meta(d / "meta.json")

    def list(self, errors: Optional[list] = None) -> list[dict]:
        """All readable metadata records; unreadable ones are reported in `errors`, never dropped silently."""
        out = []
        for d in sorted(self.root.iterdir()):
            if (d / "meta.json").exists():
                try:
                    out.append(self.read_meta(d / "meta.json"))
                except MetadataError as exc:
                    if errors is None:
                        raise
                    errors.append(str(exc))
        return out
