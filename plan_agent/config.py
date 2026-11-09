"""Runtime configuration: vocabulary, ownership policy and environment settings.

Secrets (the dashboard API key) are read from environment variables only and
are never written to reports, logs or the mapping file.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .normalize import norm_header

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DIR = REPO_ROOT / "config"


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


class Phrasebook:
    """Matches free text against lists of synonyms.

    Short tokens (<= 3 chars, symbols) only match exactly; longer phrases also
    match as whole words inside a longer text.  The longest matching phrase
    wins, so "статус проекта" beats "статус".
    """

    def __init__(self, entries: dict[str, list[str]]):
        self.entries = {k: sorted({norm_header(str(s)) for s in v}, key=len, reverse=True)
                        for k, v in entries.items()}

    def match(self, text: str, allow_partial: bool = True) -> tuple[Optional[str], float, Optional[str]]:
        """Return (key, confidence, matched_phrase)."""
        t = norm_header(text)
        if not t:
            return None, 0.0, None
        best: tuple[Optional[str], float, Optional[str]] = (None, 0.0, None)
        best_len = 0
        tied = False
        for key, phrases in self.entries.items():
            for ph in phrases:
                if not ph:
                    continue
                if t == ph:
                    conf, ln = 1.0, len(ph) + 1000
                elif allow_partial and len(ph) >= 4 and re.search(
                        r"(?<![\w])" + re.escape(ph) + r"(?![\w])", t):
                    conf, ln = 0.75, len(ph)
                else:
                    continue
                if ln > best_len:
                    best, best_len, tied = (key, conf, ph), ln, False
                elif ln == best_len and key != best[0]:
                    tied = True
                break  # phrases are sorted longest first
        if tied:
            return None, 0.0, None   # ambiguous: two keys matched equally well
        return best

    def all_matches(self, text: str) -> list[str]:
        t = norm_header(text)
        out = []
        for key, phrases in self.entries.items():
            for ph in phrases:
                if t == ph or (len(ph) >= 4 and re.search(r"(?<![\w])" + re.escape(ph) + r"(?![\w])", t)):
                    out.append(key)
                    break
        return out


@dataclass
class Vocabulary:
    raw: dict
    column_roles: Phrasebook = field(init=False)
    stage_subheaders: Phrasebook = field(init=False)
    stages: Phrasebook = field(init=False)
    additional_stages: Phrasebook = field(init=False)
    statuses: Phrasebook = field(init=False)
    priorities: Phrasebook = field(init=False)
    stage_order: list[str] = field(init=False)
    stage_labels: dict[str, str] = field(init=False)
    project_status_map: dict[str, str] = field(init=False)
    numeric_priority: dict[int, str] = field(init=False)
    known_manufacturers: list[str] = field(init=False)

    def __post_init__(self):
        r = self.raw
        self.column_roles = Phrasebook(r["column_roles"])
        self.stage_subheaders = Phrasebook(r["stage_subheaders"])
        self.stages = Phrasebook({s["code"]: s["synonyms"] + [s["label"]] for s in r["workflow_stages"]})
        self.additional_stages = Phrasebook(r.get("additional_stages", {}))
        self.statuses = Phrasebook(r["statuses"])
        self.priorities = Phrasebook(r["priorities"])
        self.stage_order = [s["code"] for s in r["workflow_stages"]]
        self.stage_labels = {s["code"]: s["label"] for s in r["workflow_stages"]}
        self.project_status_map = r["project_status_map"]
        self.numeric_priority = {int(k): v for k, v in r.get("numeric_priority", {}).items()}
        self.known_manufacturers = sorted({norm_header(m) for m in r.get("known_manufacturers", [])},
                                          key=len, reverse=True)


@dataclass
class OwnershipPolicy:
    default_owner: str
    fields: dict[str, str]
    on_manual_edit: str
    identity_fields: list[str]

    def owner(self, field_name: str) -> str:
        return self.fields.get(field_name, self.default_owner)


@dataclass
class Settings:
    """Environment-driven settings (see docs/CONFIGURATION.md)."""
    api_url: Optional[str]
    api_key: Optional[str]
    state_dir: Path
    config_dir: Path
    timeout: float = 20.0
    max_retries: int = 4
    target_month: Optional[str] = None      # "YYYY-MM" for monthly workbooks; None = profile default
    dashboard_url: Optional[str] = None     # dashboard page, if served elsewhere than the API
    require_verified_deployment: bool = True  # real sync only after verify-deployment reported "ready"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            api_url=(os.environ.get("PM_API_URL") or "").rstrip("/") or None,
            api_key=os.environ.get("PM_AGENT_API_KEY") or None,
            state_dir=Path(os.environ.get("PLAN_AGENT_STATE_DIR", REPO_ROOT / "state")),
            config_dir=Path(os.environ.get("PLAN_AGENT_CONFIG_DIR", DEFAULT_CONFIG_DIR)),
            timeout=float(os.environ.get("PM_API_TIMEOUT", "20")),
            max_retries=int(os.environ.get("PM_API_MAX_RETRIES", "4")),
            target_month=os.environ.get("PLAN_TARGET_MONTH") or None,
            dashboard_url=(os.environ.get("PM_DASHBOARD_URL") or "").rstrip("/") or None,
            require_verified_deployment=os.environ.get("PLAN_AGENT_REQUIRE_VERIFIED_DEPLOYMENT", "1") != "0",
        )

    def __repr__(self) -> str:  # never leak the key through logging/repr
        return (f"Settings(api_url={self.api_url!r}, api_key={'***' if self.api_key else None}, "
                f"state_dir={str(self.state_dir)!r})")


def load_vocabulary(config_dir: Path = DEFAULT_CONFIG_DIR) -> Vocabulary:
    return Vocabulary(_load_yaml(Path(config_dir) / "vocabulary.yaml"))


def load_policy(config_dir: Path = DEFAULT_CONFIG_DIR) -> OwnershipPolicy:
    d = _load_yaml(Path(config_dir) / "ownership_policy.yaml")
    if d.get("on_manual_edit", "report_and_keep") not in ("report_and_keep", "overwrite_and_report"):
        raise ValueError("ownership_policy.on_manual_edit must be report_and_keep or overwrite_and_report")
    return OwnershipPolicy(
        default_owner=d.get("default_owner", "planning_file"),
        fields=d.get("fields", {}),
        on_manual_edit=d.get("on_manual_edit", "report_and_keep"),
        identity_fields=d.get("identity_fields", ["external_project_id", "article_number", "manufacturer",
                                                   "vehicle_model", "product_type"]),
    )
