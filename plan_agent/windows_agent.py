"""The Windows-laptop half of the Railway split (see docs/RAILWAY_DEPLOYMENT.md).

Railway runs the Node/Express server with no access to the corporate network, so it cannot read
the Excel workbook itself. This script runs on the always-on Windows laptop, next to the real
file, and does nothing the local, single-machine deployment didn't already do: it shells out to
the *unchanged* `python -m plan_agent analyze`/`sync --json` commands exactly the way
server/routes/planImports.js's runAgent() already does for a local install - plan_agent's own
HTTP client then writes project data straight through the server's existing /api/agent/* API
(PM_API_URL pointed at Railway instead of 127.0.0.1). No workflow, stage or priority logic lives
here or is duplicated here.

What this script adds on top of that is purely the handoff: poll Railway's job queue
(server/routes/agentJobs.js) for work, run the CLI locally, and submit the --json report back.
Outbound HTTPS only - the laptop is never a server and exposes nothing.

Run it with:  python -m plan_agent.windows_agent
See docs/RAILWAY_DEPLOYMENT.md and windows/ for how to keep it running unattended (Task
Scheduler / NSSM) and docs/RAILWAY_DEPLOYMENT.md#windows-agent-env for every variable below.
"""
from __future__ import annotations

import glob
import hashlib
import json
import logging
import logging.handlers
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl  # not used on the real deployment target (Windows), only when testing on Linux/macOS

log = logging.getLogger("windows_agent")


# --------------------------------------------------------------------------------------- settings

def _int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v else default


def _float(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v else default


@dataclass
class AgentSettings:
    api_url: str
    api_key: str
    agent_api_key: str
    workbook_path: Optional[str]
    workbook_folder: Optional[str]
    workbook_pattern: str
    poll_seconds: float
    settle_seconds: float
    read_attempts: int
    retry_seconds: float
    heartbeat_seconds: float
    request_timeout: float
    state_dir: Path
    log_dir: Path
    lock_path: Path
    agent_version: str

    @classmethod
    def from_env(cls) -> "AgentSettings":
        api_url = (os.environ.get("PM_API_URL") or "").rstrip("/")
        windows_url = (os.environ.get("WINDOWS_AGENT_URL") or api_url).rstrip("/")
        state_dir = Path(os.environ.get("PLAN_AGENT_STATE_DIR", Path(__file__).resolve().parent.parent / "state"))
        missing = [n for n, v in [("PM_API_URL", api_url), ("PM_AGENT_API_KEY", os.environ.get("PM_AGENT_API_KEY")),
                                   ("WINDOWS_AGENT_API_KEY", os.environ.get("WINDOWS_AGENT_API_KEY"))] if not v]
        if not os.environ.get("WINDOWS_AGENT_WORKBOOK_PATH") and not os.environ.get("WINDOWS_AGENT_WORKBOOK_FOLDER"):
            missing.append("WINDOWS_AGENT_WORKBOOK_PATH (or WINDOWS_AGENT_WORKBOOK_FOLDER)")
        if missing:
            raise SystemExit("windows_agent: missing required settings: " + ", ".join(missing) +
                              " - see docs/RAILWAY_DEPLOYMENT.md#windows-agent-env")
        return cls(
            api_url=windows_url,
            api_key=os.environ["WINDOWS_AGENT_API_KEY"],
            agent_api_key=os.environ["PM_AGENT_API_KEY"],
            workbook_path=os.environ.get("WINDOWS_AGENT_WORKBOOK_PATH") or None,
            workbook_folder=os.environ.get("WINDOWS_AGENT_WORKBOOK_FOLDER") or None,
            workbook_pattern=os.environ.get("WINDOWS_AGENT_WORKBOOK_PATTERN", "*.xlsx"),
            poll_seconds=_float("WINDOWS_AGENT_POLL_SECONDS", 20),
            settle_seconds=_float("WINDOWS_AGENT_SETTLE_SECONDS", 5),
            read_attempts=_int("WINDOWS_AGENT_READ_ATTEMPTS", 6),
            retry_seconds=_float("WINDOWS_AGENT_RETRY_SECONDS", 10),
            heartbeat_seconds=_float("WINDOWS_AGENT_HEARTBEAT_SECONDS", 60),
            request_timeout=_float("WINDOWS_AGENT_HTTP_TIMEOUT", 30),
            state_dir=state_dir,
            log_dir=Path(os.environ.get("WINDOWS_AGENT_LOG_DIR", state_dir / "logs")),
            lock_path=Path(os.environ.get("WINDOWS_AGENT_LOCK_FILE", state_dir / "windows_agent.lock")),
            agent_version=os.environ.get("WINDOWS_AGENT_VERSION", "1"),
        )


class AgentGiveUp(Exception):
    """This job cannot be completed this attempt; tell the server and let the lease recover it."""


# --------------------------------------------------------------------------------- single instance
# A held OS file lock, not a PID file: it cannot go stale - a crash or kill releases it immediately,
# so there is no stale-lock cleanup to get wrong (spec: "prevent multiple instances").

class SingleInstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        try:
            if sys.platform == "win32":
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}")
        fh.flush()
        self._fh = fh
        return True


# -------------------------------------------------------------------------------- keep the PC awake
# Windows only (spec: "prevent sleep"); harmless no-op everywhere else, including this sandbox.

def keep_system_awake():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    except Exception:
        log.warning("could not call SetThreadExecutionState - sleep/standby is not suppressed", exc_info=True)


# ------------------------------------------------------------------------------------ job-queue API

class JobQueueClient:
    def __init__(self, settings: AgentSettings):
        self.s = settings
        self.session = requests.Session()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.s.api_key}",
            "X-Agent-Timestamp": str(int(time.time() * 1000)),
            "X-Agent-Nonce": secrets.token_hex(16),
            "X-Agent-Version": self.s.agent_version,
        }

    def next_job(self) -> Optional[dict]:
        r = self.session.get(f"{self.s.api_url}/api/agent-jobs/agent/next", headers=self._headers(),
                              timeout=self.s.request_timeout)
        r.raise_for_status()
        return r.json().get("job")

    def heartbeat(self, job_id: str) -> None:
        r = self.session.post(f"{self.s.api_url}/api/agent-jobs/agent/{job_id}/heartbeat",
                               headers=self._headers(), timeout=self.s.request_timeout)
        if r.status_code not in (200, 404, 409):
            r.raise_for_status()

    def submit_result(self, job_id: str, body: dict) -> requests.Response:
        return self.session.post(f"{self.s.api_url}/api/agent-jobs/agent/{job_id}/result", json=body,
                                  headers=self._headers(), timeout=self.s.request_timeout)

    def report_fail(self, job_id: str, error: str) -> None:
        try:
            self.session.post(f"{self.s.api_url}/api/agent-jobs/agent/{job_id}/fail", json={"error": error[:4000]},
                               headers=self._headers(), timeout=self.s.request_timeout)
        except requests.RequestException:
            log.warning("could not report failure for job %s to the server (network) - its lease will "
                        "expire and free it up on its own", job_id)


# ------------------------------------------------------------------------------- heartbeat thread

class Heartbeats:
    """Keeps a claimed job's lease alive while the (possibly slow) analyze/sync subprocess runs."""
    def __init__(self, client: JobQueueClient, job_id: str, interval: float):
        self._client, self._job_id, self._interval = client, job_id, interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.wait(self._interval):
            try:
                self._client.heartbeat(self._job_id)
            except requests.RequestException as e:
                log.warning("heartbeat for job %s failed (will retry): %s", self._job_id, e)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)


# ------------------------------------------------------------------------------------- the workbook

def _stat_or_none(p: Path):
    try:
        return p.stat()
    except OSError:
        return None


def locate_workbook(settings: AgentSettings) -> Path:
    if settings.workbook_path:
        p = Path(settings.workbook_path)
        if not p.exists():
            raise AgentGiveUp(f"файл не найден: {p}")
        return p
    folder = Path(settings.workbook_folder)
    matches = [Path(m) for m in glob.glob(str(folder / settings.workbook_pattern))]
    matches = [m for m in matches if not m.name.startswith("~$")]  # Excel's own lock files
    if not matches:
        raise AgentGiveUp(f"в папке {folder} нет файлов по образцу {settings.workbook_pattern}")
    return max(matches, key=lambda m: m.stat().st_mtime)


def wait_for_stable_file(settings: AgentSettings) -> Path:
    """Mirrors server/planSource/adapters/local.js's own settle/retry loop, in Python, against a
    file only this laptop can read. Raises AgentGiveUp if the file never settles - the job is then
    failed cleanly and the next poll (or the next scheduled/manual trigger) tries again."""
    last_sig = None
    for attempt in range(1, settings.read_attempts + 1):
        path = locate_workbook(settings)
        st = _stat_or_none(path)
        if st is None:
            raise AgentGiveUp(f"файл исчез во время проверки: {path}")
        sig = (st.st_size, st.st_mtime)
        stable = sig == last_sig
        last_sig = sig
        if stable:
            try:
                with open(path, "rb"):
                    pass
            except (PermissionError, OSError) as e:
                log.info("file present but not readable yet (attempt %s/%s): %s", attempt, settings.read_attempts, e)
            else:
                return path
            time.sleep(settings.retry_seconds)
        else:
            time.sleep(settings.settle_seconds)
    raise AgentGiveUp(f"файл не стабилизировался за {settings.read_attempts} попыток "
                       "(похоже, он постоянно изменяется или открыт исключительно)")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


# --------------------------------------------------------------------------------- running the CLI

def run_cli(args: list) -> dict:
    """python -m plan_agent <args>, exactly as runAgent() in planImports.js runs it for a local
    install - PM_API_URL/PM_AGENT_API_KEY already point at Railway via this process's own
    environment, so plan_agent's HTTP client writes there with no change to plan_agent itself.
    PLAN_AGENT_REQUIRE_VERIFIED_DEPLOYMENT is deliberately left as this environment set it
    (default: enabled) - unlike the in-app local import, this *is* a real remote deployment, and a
    `sync` should refuse to write if verify-deployment says the dashboard is not healthy."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    repo_root = Path(__file__).resolve().parent.parent
    try:
        proc = subprocess.run([sys.executable, "-m", "plan_agent"] + args, cwd=str(repo_root), env=env,
                               capture_output=True, text=True, timeout=_int("WINDOWS_AGENT_CLI_TIMEOUT_SECONDS", 600))
    except subprocess.TimeoutExpired as e:
        return {"code": -1, "out": e.stdout or "", "err": e.stderr or "", "json": None,
                "launchError": "plan_agent не ответил в отведённое время (таймаут)"}
    except OSError as e:
        return {"code": -1, "out": "", "err": str(e), "json": None,
                "launchError": f"не удалось запустить {sys.executable}: {e}"}
    out = proc.stdout or ""
    line = next((ln for ln in reversed(out.strip().splitlines()) if ln.startswith("{")), None)
    try:
        report = json.loads(line) if line else None
    except json.JSONDecodeError:
        report = None
    launch_error = None if report else (f"plan_agent завершился без JSON-отчёта (код {proc.returncode}): "
                                         f"{(proc.stderr or '').strip()[-500:]}")
    return {"code": proc.returncode, "out": out, "err": proc.stderr or "", "json": report, "launchError": launch_error}


# ------------------------------------------------------------------------------------- job handlers

def handle_check(settings: AgentSettings, job: dict) -> dict:
    path = wait_for_stable_file(settings)
    sha256, size, mtime, name = sha256_of(path), path.stat().st_size, iso_mtime(path), path.name
    month = job.get("month") or "auto"
    log.info("running `plan_agent analyze` on %s (месяц %s)", name, month)
    r = run_cli(["analyze", str(path), "--month", month, "--name", name, "--json"])
    return {"type": "analyze", "sha256": sha256, "size": size, "mtime": mtime, "name": name, "month": month,
            "analyzeJson": r["json"], "launchError": r["launchError"]}


def handle_sync(settings: AgentSettings, job: dict) -> dict:
    path = wait_for_stable_file(settings)
    name = path.name
    month = job.get("month") or "auto"
    auto_safe = job.get("trigger") != "approve"

    if not auto_safe:
        # A human approved this mapping in «Импорт плана»; the mapping-approval step itself (plan_agent's
        # own fingerprint-bound proposal store) only exists here, on the laptop - Railway never has it -
        # so it must run before `sync`, exactly as the local, non-auto-safe path already does
        # (server/routes/planImports.js's approveAndSync()). Without it, `sync` just repeats the
        # analysis ("Approval required") and never actually writes anything.
        fingerprint = job.get("fingerprint") or ""
        by = job.get("requested_by") or "windows_agent"
        log.info("running `plan_agent approve` (fingerprint %s) before sync", fingerprint or "<none>")
        appr = run_cli(["approve", "--by", by, "--fingerprint", fingerprint, "--json"])
        approved = bool(appr["json"] and appr["json"].get("approved"))
        if not approved:
            reason = appr["launchError"] or (appr["json"] and appr["json"].get("error")) or "подтверждение отклонено"
            log.warning("approve failed, not running sync: %s", reason)
            return {"type": "sync", "syncJson": None, "launchError": f"подтверждение сопоставления не удалось: {reason}"}

    args = ["sync", str(path), "--month", month, "--name", name, "--json"]
    if auto_safe:
        args.append("--auto-safe")
    log.info("running `plan_agent sync%s` on %s (месяц %s)", " --auto-safe" if auto_safe else "", name, month)
    r = run_cli(args)
    return {"type": "sync", "syncJson": r["json"], "launchError": r["launchError"], "autoApply": auto_safe}


# ----------------------------------------------------------------------------- crash-safe submission
# "Never lose an acknowledged import": the result is written to disk BEFORE it is submitted, and is
# only cleared once the server has given a definitive answer (success, or a clear "this job no
# longer accepts a result" rejection). A crash or a lost connection between computing the report and
# submitting it just means the next start (or the next loop tick) retries the *same* submission -
# plan_agent's `analyze`/`sync` is never re-run just because the network blinked.

def _pending_path(settings: AgentSettings) -> Path:
    return settings.state_dir / "windows_agent_pending_result.json"


def save_pending(settings: AgentSettings, job_id: str, body: dict) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    tmp = _pending_path(settings).with_suffix(".tmp")
    tmp.write_text(json.dumps({"job_id": job_id, "body": body}), encoding="utf-8")
    tmp.replace(_pending_path(settings))


def clear_pending(settings: AgentSettings) -> None:
    try:
        _pending_path(settings).unlink()
    except FileNotFoundError:
        pass


def submit_with_retry(client: JobQueueClient, settings: AgentSettings, job_id: str, body: dict) -> None:
    save_pending(settings, job_id, body)
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = client.submit_result(job_id, body)
        except requests.RequestException as e:
            wait = min(60, 2 ** min(attempt, 6))
            log.warning("could not submit result for job %s (attempt %s, %s) - retrying in %ss", job_id, attempt, e, wait)
            time.sleep(wait)
            continue
        if resp.status_code == 200:
            log.info("job %s result accepted: %s", job_id, resp.json().get("result", {}).get("status"))
            clear_pending(settings)
            return
        if resp.status_code in (404, 409):
            # The job no longer exists or is no longer "running" (its lease already expired and was
            # reconciled server-side, or something else already completed/claimed it). Nothing to
            # retry - the server already moved on; a fresh check will be enqueued on the next trigger.
            log.warning("job %s no longer accepts a result (%s): %s - dropping it", job_id, resp.status_code, resp.text[:300])
            clear_pending(settings)
            return
        wait = min(60, 2 ** min(attempt, 6))
        log.warning("server rejected result for job %s (%s): %s - retrying in %ss", job_id, resp.status_code, resp.text[:300], wait)
        time.sleep(wait)


def flush_pending_submission(client: JobQueueClient, settings: AgentSettings) -> None:
    p = _pending_path(settings)
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("pending result file %s is unreadable - discarding it", p)
        clear_pending(settings)
        return
    log.info("resuming a result submission left over from a previous run (job %s)", data["job_id"])
    submit_with_retry(client, settings, data["job_id"], data["body"])


# ------------------------------------------------------------------------------------------- main

def handle_job(client: JobQueueClient, settings: AgentSettings, job: dict) -> None:
    log.info("claimed job %s type=%s trigger=%s", job["id"], job["type"], job.get("trigger"))
    try:
        with Heartbeats(client, job["id"], settings.heartbeat_seconds):
            if job["type"] == "check":
                body = handle_check(settings, job)
            elif job["type"] == "sync":
                body = handle_sync(settings, job)
            else:
                client.report_fail(job["id"], f"агент не понимает тип задачи «{job['type']}»")
                return
    except AgentGiveUp as e:
        log.warning("job %s cannot be completed this attempt: %s", job["id"], e)
        client.report_fail(job["id"], str(e))
        return
    submit_with_retry(client, settings, job["id"], body)


def _configure_logging(settings: AgentSettings) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(settings.log_dir / "windows_agent.log",
                                                     maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(console)


def main(argv=None) -> int:
    settings = AgentSettings.from_env()
    _configure_logging(settings)
    log.info("Windows agent starting: url=%s workbook=%s", settings.api_url,
             settings.workbook_path or f"{settings.workbook_folder}/{settings.workbook_pattern}")

    lock = SingleInstanceLock(settings.lock_path)
    if not lock.acquire():
        log.error("another windows_agent instance already holds %s - exiting", settings.lock_path)
        return 1

    keep_system_awake()
    client = JobQueueClient(settings)

    stop = threading.Event()

    def _signal_handler(signum, frame):
        log.info("received signal %s, shutting down after the current poll", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    flush_pending_submission(client, settings)

    while not stop.is_set():
        try:
            job = client.next_job()
        except requests.RequestException as e:
            log.warning("could not poll for jobs (network): %s - retrying in %ss", e, settings.poll_seconds)
            stop.wait(settings.poll_seconds)
            continue
        if job:
            try:
                handle_job(client, settings, job)
            except Exception:
                log.exception("unexpected error handling job %s - will be retried after its lease expires", job.get("id"))
        else:
            stop.wait(settings.poll_seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
