"""Delivery channels other than the CLI.

* ``serve``  - HTTP intake for other agents:  POST /files  (raw body)
* ``watch``  - folder drop / scheduled polling
Both hand the file to PlanAgent.receive(), i.e. the same pipeline.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .ingestion import MAX_FILE_BYTES
from .service import PlanAgent

log = logging.getLogger("plan_agent.intake")
_lock = threading.Lock()   # one processing run at a time (keeps runs ordered)


def make_intake_server(agent: PlanAgent, host: str, port: int, intake_key: str) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.info("%s - %s", self.address_string(), fmt % args)

        def _send(self, status, body):
            raw = json.dumps(body, ensure_ascii=False, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _auth(self) -> bool:
            auth = self.headers.get("Authorization", "")
            token = auth[7:] if auth.startswith("Bearer ") else ""
            return bool(token) and hmac.compare_digest(token.encode(), intake_key.encode())

        def do_GET(self):
            if urlparse(self.path).path == "/health":
                return self._send(200, {"status": "ok"})
            if not self._auth():
                return self._send(401, {"error": "unauthorized"})
            if urlparse(self.path).path == "/files":
                return self._send(200, {"files": agent.inbox.list()})
            self._send(404, {"error": "not_found"})

        def do_POST(self):
            url = urlparse(self.path)
            if not self._auth():
                return self._send(401, {"error": "unauthorized"})
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            mode = q.get("mode", "auto")
            if mode not in ("auto", "dry_run"):
                return self._send(400, {"error": "mode must be auto or dry_run"})
            if url.path.startswith("/files/") and url.path.endswith("/reprocess"):
                sha = url.path.split("/")[2]
                with _lock:
                    try:
                        out = agent.reprocess(sha, mode)
                    except FileNotFoundError as exc:
                        return self._send(404, {"error": str(exc)})
                return self._send(200, _outcome(out))
            if url.path != "/files":
                return self._send(404, {"error": "not_found"})
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return self._send(400, {"error": "empty body"})
            if n > MAX_FILE_BYTES:
                return self._send(413, {"error": "file too large"})
            name = self.headers.get("X-Filename") or q.get("filename") or "uploaded_plan"
            name = os.path.basename(name)
            meta = {"sender": self.headers.get("X-Sender", "unknown")}
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                remaining = n
                while remaining:
                    chunk = self.rfile.read(min(remaining, 1 << 20))
                    if not chunk:
                        break
                    tmp.write(chunk)
                    remaining -= len(chunk)
            try:
                with _lock:
                    out = agent.receive(Path(tmp.name), "api", name, mode, meta, target_month=q.get("month"))
            finally:
                os.unlink(tmp.name)
            self._send(200, _outcome(out))

    return ThreadingHTTPServer((host, port), Handler)


def _outcome(out) -> dict:
    return {"run_id": out.run_id, "status": out.status, "counts": out.report["counts"],
            "report_text": out.text, "analysis_markdown": out.analysis_markdown}


def watch_folder(agent: PlanAgent, folder: Path, interval: float, once: bool = False) -> None:
    """Process every file dropped into ``folder``; move it to processed/ or failed/.

    Run with ``--once`` from cron / a systemd timer for scheduled processing."""
    folder = Path(folder)
    (folder / "processed").mkdir(parents=True, exist_ok=True)
    (folder / "failed").mkdir(exist_ok=True)
    seen_sizes: dict[Path, int] = {}
    while True:
        for f in sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith((".", "~$"))):
            size = f.stat().st_size
            if not once and seen_sizes.get(f) != size:      # wait until the writer has finished
                seen_sizes[f] = size
                continue
            seen_sizes.pop(f, None)
            with _lock:
                out = agent.receive(f, "watch_folder", f.name)
            print(out.text)
            target = folder / ("failed" if out.status == "Failed" else "processed")
            shutil.move(str(f), str(target / f"{out.run_id}__{f.name}"))
        if once:
            return
        time.sleep(interval)
