"""File ingestion: every delivery channel (CLI, watched folder, HTTP intake,
schedule, reprocess) stores the file in the content-addressed inbox and then
calls the same processing pipeline.  File names are kept only as metadata."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .pipeline import sha256_file

MAX_FILE_BYTES = 50 * 1024 * 1024


class Inbox:
    def __init__(self, state_dir: Path):
        self.root = Path(state_dir) / "inbox"
        self.root.mkdir(parents=True, exist_ok=True)

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
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {"sha256": sha, "deliveries": []}
        meta["deliveries"].append({
            "channel": channel,
            "original_name": original_name or src.name,
            "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **(metadata or {}),
        })
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        return dest, meta

    def get(self, sha: str) -> tuple[Path, dict]:
        d = self.root / sha
        if not (d / "file").exists():
            matches = [p for p in self.root.iterdir() if p.name.startswith(sha)]
            if len(matches) != 1:
                raise FileNotFoundError(f"no unique stored file for {sha!r}")
            d = matches[0]
        return d / "file", json.loads((d / "meta.json").read_text())

    def list(self) -> list[dict]:
        out = []
        for d in sorted(self.root.iterdir()):
            if (d / "meta.json").exists():
                out.append(json.loads((d / "meta.json").read_text()))
        return out
