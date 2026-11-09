"""Match extracted records to existing dashboard projects.

Order of evidence (strongest first):
1. external project id  (exact, normalised)
2. article number       (exact, normalised)
3. manufacturer + model + product type (exact, normalised)
Fuzzy similarity is used ONLY to raise duplicate *candidates* for review; it
never causes an automatic merge or update.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional

from .models import ProjectRecord
from .normalize import norm_key

FUZZY_THRESHOLD = 0.85


@dataclass
class MatchResult:
    kind: str                       # matched | new | duplicate | review
    project: Optional[dict] = None
    candidates: list[dict] = field(default_factory=list)
    reason: str = ""
    method: str = ""


def _cmp_key(p) -> Optional[str]:
    get = p.get if isinstance(p, dict) else lambda k: getattr(p, k)
    vals = [get("manufacturer"), get("vehicle_model"), get("product_type")]
    if all(vals):
        cat = (get("record_category") if isinstance(p, dict) else getattr(p, "record_category", None)) \
            or "main_project"
        return cat + ":" + "|".join(norm_key(v) for v in vals)
    return None


def _brief(p: dict) -> dict:
    return {k: p.get(k) for k in ("id", "external_project_id", "article_number", "project_name")}


class ProjectMatcher:
    def __init__(self, existing: list[dict]):
        self.existing = existing
        self.by_ext: dict[str, list[dict]] = {}
        self.by_art: dict[str, list[dict]] = {}
        self.by_cmp: dict[str, list[dict]] = {}
        for p in existing:
            if p.get("external_project_id"):
                self.by_ext.setdefault(norm_key(p["external_project_id"]), []).append(p)
            if p.get("article_number"):
                self.by_art.setdefault(norm_key(p["article_number"]), []).append(p)
            k = _cmp_key(p)
            if k:
                self.by_cmp.setdefault(k, []).append(p)

    def match(self, rec: ProjectRecord) -> MatchResult:
        ext = self.by_ext.get(norm_key(rec.external_project_id)) if rec.external_project_id else None
        art = self.by_art.get(norm_key(rec.article_number)) if rec.article_number else None
        cmp_ = self.by_cmp.get(_cmp_key(rec)) if _cmp_key(rec) else None

        if ext:
            if len(ext) > 1:
                return MatchResult("duplicate", candidates=[_brief(p) for p in ext],
                                   reason="several dashboard projects share this project id")
            target = ext[0]
            if rec.article_number and target.get("article_number") and \
                    norm_key(rec.article_number) != norm_key(target["article_number"]):
                return MatchResult("review", target, [_brief(target)],
                                   f"project id matches but article differs (dashboard "
                                   f"'{target['article_number']}', file '{rec.article_number}')")
            if art and any(p["id"] != target["id"] for p in art):
                return MatchResult("review", target, [_brief(p) for p in art + [target]],
                                   "project id and article number point to different dashboard projects")
            return MatchResult("matched", target, method="external_project_id")

        if art:
            if len(art) > 1:
                return MatchResult("duplicate", candidates=[_brief(p) for p in art],
                                   reason="several dashboard projects share this article number")
            target = art[0]
            if rec.external_project_id and target.get("external_project_id"):
                return MatchResult("review", target, [_brief(target)],
                                   f"article matches project '{target['external_project_id']}' but the file "
                                   f"uses id '{rec.external_project_id}'")
            return MatchResult("matched", target, method="article_number")

        if cmp_:
            if len(cmp_) > 1:
                return MatchResult("duplicate", candidates=[_brief(p) for p in cmp_],
                                   reason="several dashboard projects have the same manufacturer/model/product")
            target = cmp_[0]
            conflicts = []
            if rec.external_project_id and target.get("external_project_id"):
                conflicts.append("project id")
            if rec.article_number and target.get("article_number"):
                conflicts.append("article number")
            if conflicts:
                return MatchResult("duplicate", target, [_brief(target)],
                                   f"same manufacturer/model/product but different {', '.join(conflicts)}: "
                                   "separate project or duplicate?")
            return MatchResult("matched", target, method="manufacturer+model+product")

        # Fuzzy: candidates only.  Two records that both have a complete
        # identity (manufacturer + model + product) which differs are different
        # projects by definition (e.g. "AUDI A5 long kit" vs "AUDI Q5 kit"), so
        # name similarity is only used when one side lacks a complete identity.
        cands = []
        name = norm_key(rec.project_name)
        rec_full = _cmp_key(rec) is not None
        for p in self.existing:
            if rec_full and _cmp_key(p) is not None:
                continue
            score = SequenceMatcher(None, name, norm_key(p.get("project_name"))).ratio() if name else 0
            if score >= FUZZY_THRESHOLD:
                cands.append({**_brief(p), "similarity": round(score, 2)})
        if cands:
            return MatchResult("duplicate", candidates=cands,
                               reason="no exact identifier match, but similar project name(s) exist")
        return MatchResult("new", reason="no existing project with this id, article or manufacturer/model/product")
