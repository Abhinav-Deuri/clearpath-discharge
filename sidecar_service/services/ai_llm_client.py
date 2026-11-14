"""Async client for any OpenAI-compatible endpoint, with a deterministic mock fallback."""
import json
import logging
import re

import httpx

from config import settings
from schemas import DraftResponse, MedicationItem, TokenStats

log = logging.getLogger("clearpath.llm")

_SYSTEM_PROMPT = (
    "You are a clinical documentation assistant. Write a discharge summary for the patient's ACTUAL "
    "condition or procedure as described in the handoff note; do not assume any particular procedure. "
    "Use ONLY facts present in the provided data (handoff note, medications, allergies, supplies). "
    "Never invent vitals, dates, doses, test results, or instructions that are not in the data. "
    "Omit any item with nothing documented; do not write placeholders. Format strictly as: a first line "
    "**Discharge Summary**, then one bullet per item written as '- **Label**: value'. Use these labels "
    "when documented: Patient, Procedure, Hospital Course, Incisions, Plan, Medications, Allergies, "
    "Items for clinician review, Follow-up; add other labels only if this condition needs them. Keep it "
    "under 250 words. No tables, no horizontal rules (---), no nested bullets, no empty bullets. If a medication seems to conflict with a documented "
    "allergy, list it under 'Items for clinician review' and do NOT resolve, approve or override it; "
    "a human will decide. Also give 3 to 5 short review questions specific to this case. "
    "Also list proposed_medications: only medications explicitly stated in the handoff note, "
    "or an empty list if none. "
    'Respond ONLY with JSON: {"summary": "<markdown text>", "review_prompts": ["<question>", ...], '
    '"proposed_medications": [{"drug_name": "<name>", "dosage": "<dose>", "frequency": "<freq>"}]}'
)


_MED_RE = re.compile(
    r"\b([A-Z][a-z]{3,})\s+(\d+(?:\.\d+)?\s?(?:mg|mcg|g|ml))\b"
    r"(?:\s+(TID|BID|QID|QD|daily|three times daily|twice daily|once daily|four times daily))?"
)
_FREQ = {"three times daily": "TID", "twice daily": "BID", "once daily": "daily",
         "four times daily": "QID"}


def extract_medications(note: str) -> list[dict]:
    """Deterministic 'Drug 500mg TID' extraction used by the mock path."""
    out, seen = [], set()
    for name, dose, freq in _MED_RE.findall(note or ""):
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({"drug_name": name, "dosage": dose.replace(" ", ""),
                    "frequency": _FREQ.get(freq, freq)})
    return out


def _mock_draft(context: dict) -> tuple[str, list[str], list[dict]]:
    note = context.get("raw_handoff_note", "")
    meds = context.get("medications", [])
    med_text = "; ".join(
        f"{m.get('drug_name', '?')} {m.get('dosage', '')} {m.get('frequency', '')}".strip() for m in meds
    ) or "None listed"
    allergies = ", ".join(
        f"{a.get('allergen', '?')} ({a.get('reaction', '?')})"
        for a in context.get("allergies", [])
    ) or "None documented"
    first = note.strip().split(".")[0][:120] if note.strip() else "See handoff note"
    summary = (
        "**Discharge Summary**\n"
        f"- **Patient**: {context.get('patient_code', 'PT-???')}, Bed {context.get('bed_number', '?')}\n"
        f"- **Procedure**: {first}\n"
        f"- **Hospital Course**: {note.strip()[:400]}\n"
        f"- **Medications**: {med_text}\n"
        f"- **Allergies**: {allergies}\n"
        "- **Follow-up**: per clinician; see handoff note."
    )
    prompts = [
        "Confirm each proposed discharge medication is compatible with the documented allergies.",
        "Confirm all surgical supplies used are reflected in the billing record.",
        "Confirm follow-up timing and return precautions are appropriate for this patient.",
    ]
    return summary, prompts, extract_medications(note)


def _clean_meds(raw) -> list[MedicationItem]:
    meds = []
    for m in raw or []:
        try:
            if str(m.get("drug_name", "")).strip():
                meds.append(MedicationItem(drug_name=str(m["drug_name"]).strip(),
                                           dosage=str(m.get("dosage", "") or ""),
                                           frequency=str(m.get("frequency", "") or "")))
        except Exception:
            continue
    return meds


def _parse_json_content(content: str) -> dict:
    """Accept raw JSON or JSON wrapped in a markdown code fence."""
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    return json.loads(text)


async def _call_endpoint(context: dict) -> dict:
    url = settings.ai_base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": settings.ai_model_name,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(context)},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {settings.ai_api_key}"}
    async with httpx.AsyncClient(timeout=settings.ai_timeout_seconds) as client:
        resp = await client.post(url, json=body, headers=headers)
        if resp.status_code == 400:  # endpoint rejects JSON mode -> retry as plain text
            body.pop("response_format")
            resp = await client.post(url, json=body, headers=headers)
        resp.raise_for_status()
    return _parse_json_content(resp.json()["choices"][0]["message"]["content"])


async def generate_discharge_summary(
    context: dict, discharge_id: int, stats: TokenStats
) -> DraftResponse:
    use_mock = settings.use_mock_llm or not (settings.ai_base_url and settings.ai_api_key)
    if not use_mock:
        try:
            data = await _call_endpoint(context)
            return DraftResponse(
                discharge_id=discharge_id,
                ai_draft_summary=str(data["summary"]),
                review_prompts=[str(p) for p in data.get("review_prompts", [])],
                model_used=settings.ai_model_name,
                used_mock=False,
                token_stats=stats,
                proposed_medications=_clean_meds(data.get("proposed_medications")),
            )
        except Exception as exc:  # network down, bad JSON, 5xx -> never hang the demo
            log.warning("Live LLM failed (%s); falling back to mock draft", exc)
    summary, prompts, meds = _mock_draft(context)
    return DraftResponse(
        discharge_id=discharge_id,
        ai_draft_summary=summary,
        review_prompts=prompts,
        model_used="mock-deterministic",
        used_mock=True,
        token_stats=stats,
        proposed_medications=_clean_meds(meds),
    )
