"""Strip Odoo ORM bloat, de-identify the patient, and measure token savings."""
import json
import math
from typing import Any

from schemas import DraftRequest, HistoryItem, TokenStats

# ORM / framework keys with no clinical value for the LLM.
_DROP_KEYS = {
    "id",
    "display_name",
    "create_uid",
    "create_date",
    "write_uid",
    "write_date",
    "__last_update",
    "message_ids",
    "message_follower_ids",
    "message_partner_ids",
    "message_main_attachment_id",
    "activity_ids",
    "website_message_ids",
    "access_token",
    "discharge_id",
    "patient_name",
}


def estimate_tokens(obj: Any) -> int:
    """Cheap, deterministic token estimate (~4 chars/token)."""
    text = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    return math.ceil(len(text) / 4)


def _is_m2o_pair(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and isinstance(value[0], int)
        and isinstance(value[1], str)
    )


def _clean(value: Any, name: str, code: str) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in _DROP_KEYS:
                continue
            cleaned = _clean(v, name, code)
            if cleaned in (None, False, "", [], {}):
                continue
            out[k] = cleaned
        return out
    if _is_m2o_pair(value):  # Odoo many2one [id, "Display Name"]
        return _clean(value[1], name, code)
    if isinstance(value, (list, tuple)):
        return [c for c in (_clean(v, name, code) for v in value) if c not in (None, False, "", [], {})]
    if isinstance(value, str) and name:
        return value.replace(name, code)
    return value


def history_from_orm(rows: Any) -> list[dict]:
    """Raw Odoo history rows (full ORM dumps) -> the lightweight HistoryItem shape."""
    out = []
    for row in rows if isinstance(rows, (list, tuple)) else []:
        if not isinstance(row, dict) or not str(row.get("condition_name") or "").strip():
            continue
        try:
            out.append(HistoryItem(
                condition_name=str(row["condition_name"]).strip(),
                history_type=str(row.get("history_type") or "medical"),
                status=str(row.get("status") or "active"),
                diagnosis_date=str(row.get("diagnosis_date") or ""),
            ).model_dump())
        except Exception:
            continue
    return out


def prune_and_anonymize(req: DraftRequest) -> tuple[dict, TokenStats]:
    """Return (pruned_context, stats). PII (patient_name) never leaves the sidecar."""
    raw = req.model_dump()
    # Undeclared extra keys are raw Odoo ORM dumps: counted as "before", never sent on.
    declared = {k: v for k, v in raw.items() if k in DraftRequest.model_fields}
    if not declared.get("past_medical_history"):  # only the raw ORM dump was sent: map it
        declared["past_medical_history"] = history_from_orm(raw.get("orm_history"))
    pruned = _clean(declared, req.patient_name, req.patient_code)
    before = estimate_tokens(raw)
    after = estimate_tokens(pruned)
    saved = max(before - after, 0)
    pct = round(saved / before * 100, 1) if before else 0.0
    return pruned, TokenStats(
        before_tokens=before, after_tokens=after, saved_tokens=saved, saved_pct=pct
    )
