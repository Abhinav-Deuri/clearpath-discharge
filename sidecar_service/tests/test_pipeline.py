"""Pipeline tests: PT-402 flags, parallel MCP execution, token pruning, Stale-Write Guard."""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient

import database
import main
from config import settings
from schemas import (
    AllergyItem,
    DraftRequest,
    MedicationItem,
    SupplyItem,
    TokenStats,
    VerifyRequest,
)
from services import mcp_tools
from services.token_pruner import prune_and_anonymize

HASH1 = "adb6cd43" + "0" * 56
HASH2 = "9f3e21c7" + "1" * 56


def pt402_verify(rev=1, rev_hash=HASH1, drug="Amoxicillin", push=False) -> dict:
    return {
        "discharge_id": 402,
        "revision_id": rev,
        "revision_hash": rev_hash,
        "patient_code": "PT-402",
        "medications": [{"drug_name": drug, "dosage": "500mg", "frequency": "TID"}],
        "allergies": [{"allergen": "Penicillin", "reaction": "Anaphylaxis", "criticality": "high"}],
        "supplies": [
            {"item_name": "Hemostatic Gauze Pad", "surgical_qty_used": 4, "billed_qty": 2},
            {"item_name": "Skin Stapler", "surgical_qty_used": 1, "billed_qty": 0},
        ],
        "push_to_odoo": push,
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "sqlite_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings, "use_mock_llm", True)
    with TestClient(main.app) as c:
        yield c


# ---------- deterministic rules ----------
async def test_pharmacy_flags_amoxicillin_vs_penicillin():
    out = await mcp_tools.check_pharmacy_safety(
        [MedicationItem(drug_name="Amoxicillin", dosage="500mg")],
        [AllergyItem(allergen="Penicillin", reaction="Anaphylaxis", criticality="high")],
    )
    assert out.status == "flagged"
    assert out.conflicts[0].severity == "critical"
    assert "Anaphylaxis" in out.conflicts[0].reason


async def test_pharmacy_clears_azithromycin():
    out = await mcp_tools.check_pharmacy_safety(
        [MedicationItem(drug_name="Azithromycin", dosage="500mg")],
        [AllergyItem(allergen="Penicillin", reaction="Anaphylaxis", criticality="high")],
    )
    assert out.status == "clear" and not out.conflicts


async def test_billing_catches_gauze_and_stapler():
    out = await mcp_tools.audit_surgical_billing(
        [
            SupplyItem(item_name="Hemostatic Gauze Pad", surgical_qty_used=4, billed_qty=2),
            SupplyItem(item_name="Skin Stapler", surgical_qty_used=1, billed_qty=0),
            SupplyItem(item_name="Suture", surgical_qty_used=3, billed_qty=3),
        ]
    )
    assert out.status == "flagged"
    assert {d.item_name: d.discrepancy_qty for d in out.discrepancies} == {
        "Hemostatic Gauze Pad": 2,
        "Skin Stapler": 1,
    }


# ---------- parallel execution ----------
async def test_checks_run_in_parallel(monkeypatch):
    async def slow_pharm(meds, allergies):
        await asyncio.sleep(0.3)
        return await orig_pharm(meds, allergies)

    async def slow_bill(supplies):
        await asyncio.sleep(0.3)
        return await orig_bill(supplies)

    orig_pharm, orig_bill = mcp_tools.check_pharmacy_safety, mcp_tools.audit_surgical_billing
    monkeypatch.setattr(mcp_tools, "check_pharmacy_safety", slow_pharm)
    monkeypatch.setattr(mcp_tools, "audit_surgical_billing", slow_bill)

    start = time.perf_counter()
    pharm, bill, _ins, _ms = await mcp_tools.run_parallel_mcp_checks(VerifyRequest(**pt402_verify()))
    elapsed = time.perf_counter() - start

    assert elapsed < 0.55, f"sequential would be ~0.6s, got {elapsed:.2f}s"
    assert pharm.status == "flagged" and bill.status == "flagged"


# ---------- token pruner ----------
def test_pruner_strips_orm_metadata_and_pii():
    req = DraftRequest(
        discharge_id=402,
        patient_code="PT-402",
        patient_name="Arabith",
        bed_number="402",
        raw_handoff_note="Arabith tolerated appendectomy well. Start Amoxicillin 500mg.",
        medications=[MedicationItem(drug_name="Amoxicillin", dosage="500mg", frequency="TID")],
        create_uid=[2, "Administrator"],
        write_date="2026-11-13 10:00:00",
        __last_update="2026-11-13 10:00:00",
        message_ids=[1, 2, 3, 4, 5, 6, 7, 8],
        message_follower_ids=[9, 10, 11],
        display_name="Arabith / Bed 402",
    )
    pruned, stats = prune_and_anonymize(req)
    flat = str(pruned)
    assert "Arabith" not in flat
    assert "PT-402" in flat
    for junk in ("create_uid", "write_date", "__last_update", "message_ids", "display_name"):
        assert junk not in flat
    assert stats.after_tokens < stats.before_tokens
    assert stats.saved_pct > 0
    assert stats.saved_tokens == stats.before_tokens - stats.after_tokens


# ---------- endpoints ----------
def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["sqlite"] is True and body["mock_llm"] is True


def test_draft_endpoint_mock(client):
    r = client.post(
        "/api/v1/draft",
        json={
            "discharge_id": 402,
            "patient_code": "PT-402",
            "patient_name": "Arabith",
            "bed_number": "402",
            "raw_handoff_note": "Arabith post-op appendectomy, start Amoxicillin 500mg.",
            "medications": [{"drug_name": "Amoxicillin", "dosage": "500mg", "frequency": "TID"}],
            "create_uid": [2, "Administrator"],
            "message_ids": list(range(30)),
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["used_mock"] is True
    assert "Arabith" not in body["ai_draft_summary"]
    assert "PT-402" in body["ai_draft_summary"]
    assert body["review_prompts"]
    assert body["token_stats"]["saved_pct"] > 0


def test_verify_flags_and_snapshot_roundtrip(client):
    r = client.post("/api/v1/verify", json=pt402_verify())
    assert r.status_code == 200
    snap = r.json()
    assert snap["snapshot_id"] == "adb6cd43_Rev1"
    assert snap["status"] == "flagged"
    assert snap["pharmacy"]["status"] == "flagged"
    assert snap["billing"]["summary"] == "Unbilled supplies: +2 Hemostatic Gauze Pad, +1 Skin Stapler."

    got = client.get("/api/v1/snapshot/402/1")
    assert got.status_code == 200 and got.json()["snapshot_id"] == "adb6cd43_Rev1"
    assert client.get("/api/v1/snapshot/402/9").status_code == 404


# ---------- Stale-Write Guard ----------
def test_stale_revision_rejected_after_rev2(client):
    assert client.post("/api/v1/verify", json=pt402_verify(rev=1)).status_code == 200
    # Late doctor edit: Rev 2 (Azithromycin) registers and clears pharmacy.
    r2 = client.post("/api/v1/verify", json=pt402_verify(rev=2, rev_hash=HASH2, drug="Azithromycin"))
    assert r2.status_code == 200 and r2.json()["pharmacy"]["status"] == "clear"
    # A straggling Rev 1 worker must be rejected.
    stale = client.post("/api/v1/verify", json=pt402_verify(rev=1))
    assert stale.status_code == 409
    assert "Stale" in stale.json()["detail"]


async def test_verify_not_stale_and_mid_run_invalidation(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "sqlite_path", str(tmp_path / "g.db"))
    await database.init_db()
    assert await database.register_revision(402, 1, HASH1)
    assert await database.verify_not_stale(402, 1)
    assert await database.register_revision(402, 2, HASH2)
    assert not await database.verify_not_stale(402, 1)
    assert not await database.register_revision(402, 1, HASH1)  # cannot go backwards
    with pytest.raises(database.StaleRevisionError):
        await database.save_revision_snapshot(402, 1, HASH1, "flagged", {})
    assert await database.save_revision_snapshot(402, 2, HASH2, "cleared", {"ok": 1}) == "9f3e21c7_Rev2"


def test_reset_endpoint_allows_fresh_rev1(client):
    assert client.post("/api/v1/verify", json=pt402_verify(rev=1)).status_code == 200
    assert client.post("/api/v1/verify", json=pt402_verify(rev=2, rev_hash=HASH2)).status_code == 200
    assert client.post("/api/v1/verify", json=pt402_verify(rev=1)).status_code == 409
    assert client.delete("/api/v1/discharge/402/state").json()["cleared"] is True
    assert client.post("/api/v1/verify", json=pt402_verify(rev=1)).status_code == 200
    assert client.get("/api/v1/snapshot/402/2").status_code == 404


# ---------------------------------------------------------------- Phase 5
import base64

AUDIO_B64 = base64.b64encode(b"RIFF....WAVEdemo").decode()
IMAGE_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\ndemo").decode()


def clean_verify(**over) -> dict:
    """Azithromycin, fully billed, mapped procedure, policy on file -> insurance ready."""
    body = pt402_verify(drug="Azithromycin")
    body["supplies"] = [
        {"item_name": "Hemostatic Gauze Pad", "surgical_qty_used": 4, "billed_qty": 4},
        {"item_name": "Skin Stapler", "surgical_qty_used": 1, "billed_qty": 1},
    ]
    body.update({"procedure_text": "POD1 laparoscopic appendectomy", "tpa_provider": "Demo TPA",
                 "policy_number": "POL-402-DEMO"})
    body.update(over)
    return body


async def test_three_workers_run_in_parallel(monkeypatch):
    async def slow_pharm(meds, allergies):
        await asyncio.sleep(0.3)
        return await orig_pharm(meds, allergies)

    async def slow_bill(supplies):
        await asyncio.sleep(0.3)
        return await orig_bill(supplies)

    async def slow_ins(req):
        await asyncio.sleep(0.3)
        return await orig_ins(req)

    orig_pharm, orig_bill = mcp_tools.check_pharmacy_safety, mcp_tools.audit_surgical_billing
    orig_ins = mcp_tools.generate_tpa_insurance_claim
    monkeypatch.setattr(mcp_tools, "check_pharmacy_safety", slow_pharm)
    monkeypatch.setattr(mcp_tools, "audit_surgical_billing", slow_bill)
    monkeypatch.setattr(mcp_tools, "generate_tpa_insurance_claim", slow_ins)

    start = time.perf_counter()
    pharm, bill, ins, _ = await mcp_tools.run_parallel_mcp_checks(VerifyRequest(**pt402_verify()))
    elapsed = time.perf_counter() - start

    assert elapsed < 0.55, f"sequential would be ~0.9s, got {elapsed:.2f}s"
    assert pharm.status == "flagged" and bill.status == "flagged"
    assert ins.claim_status == "hold_pending_reconciliation"


async def test_insurance_ready_when_everything_reconciled():
    ins = await mcp_tools.generate_tpa_insurance_claim(VerifyRequest(**clean_verify()))
    assert ins.claim_status == "ready_for_review" and not ins.hold_reasons
    assert (ins.icd10_code, ins.cpt_code) == ("K35.80", "44970")
    assert "44970" in ins.filled_claim_markdown and "POL-402-DEMO" in ins.filled_claim_markdown


async def test_insurance_hold_lists_pharmacy_and_billing_reasons():
    body = clean_verify()
    body["medications"] = pt402_verify()["medications"]  # Amoxicillin again
    body["supplies"] = pt402_verify()["supplies"]  # 4/2 and 1/0
    ins = await mcp_tools.generate_tpa_insurance_claim(VerifyRequest(**body))
    assert ins.claim_status == "hold_pending_reconciliation"
    joined = " ".join(ins.hold_reasons)
    assert "Amoxicillin" in joined and "+2 Hemostatic Gauze Pad" in joined and "+1 Skin Stapler" in joined
    assert any(l.note == "+2 unbilled" for l in ins.reconciled_line_items)


async def test_insurance_holds_on_unmapped_procedure_or_missing_policy():
    unmapped = await mcp_tools.generate_tpa_insurance_claim(
        VerifyRequest(**clean_verify(procedure_text="routine chest xray")))
    assert unmapped.claim_status == "hold_pending_reconciliation"
    assert any("not mapped" in h for h in unmapped.hold_reasons)

    no_policy = await mcp_tools.generate_tpa_insurance_claim(
        VerifyRequest(**clean_verify(policy_number="")))
    assert any("policy number missing" in h for h in no_policy.hold_reasons)


def test_verify_includes_insurance_and_clears_when_all_reconciled(client):
    flagged = client.post("/api/v1/verify", json=pt402_verify()).json()
    assert flagged["status"] == "flagged"
    assert flagged["insurance_output"]["claim_status"] == "hold_pending_reconciliation"

    cleared = client.post("/api/v1/verify", json=clean_verify(revision_id=2, revision_hash=HASH2)).json()
    assert cleared["status"] == "cleared"
    assert cleared["insurance_output"]["claim_status"] == "ready_for_review"
    assert client.get("/api/v1/snapshot/402/2").json()["insurance_output"]["icd10_code"] == "K35.80"


def test_stale_rev1_cannot_overwrite_insurance(client):
    assert client.post("/api/v1/verify", json=clean_verify()).status_code == 200  # Rev 1
    assert client.post("/api/v1/verify", json=clean_verify(revision_id=2, revision_hash=HASH2)).status_code == 200
    stale = client.post("/api/v1/verify", json=pt402_verify(rev=1, rev_hash=HASH1))
    assert stale.status_code == 409
    saved = client.get("/api/v1/snapshot/402/1").json()
    assert saved["insurance_output"]["claim_status"] == "ready_for_review"  # untouched by the stale call


def test_old_snapshot_without_insurance_still_parses():
    from schemas import AuditPacketSnapshot
    snap = AuditPacketSnapshot(
        snapshot_id="adb6cd43_Rev1", discharge_id=1, revision_id=1, revision_hash=HASH1, status="cleared",
        pharmacy={"status": "clear", "summary": "ok"}, billing={"status": "clear", "summary": "ok"},
        elapsed_ms=1.0)
    assert snap.insurance_output is None


def test_transcribe_and_draft_mock(client):
    r = client.post("/api/v1/transcribe-and-draft", json={
        "discharge_id": 402, "audio_base64": AUDIO_B64, "filename": "demo_voice.wav",
        "patient_code": "PT-402", "patient_name": "Arabith", "bed_number": "402",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert "Amoxicillin 500mg" in body["transcribed_text"]
    assert "Arabith" not in body["ai_draft_summary"] and "PT-402" in body["ai_draft_summary"]
    meds = {(m["drug_name"], m["dosage"], m["frequency"]) for m in body["proposed_medications"]}
    assert ("Amoxicillin", "500mg", "TID") in meds
    assert body["used_mock"] is True


def test_transcribe_rejects_bad_or_oversized_audio(client, monkeypatch):
    base = {"discharge_id": 1, "patient_code": "PT-1"}
    assert client.post("/api/v1/transcribe-and-draft", json={**base, "audio_base64": "!!notbase64!!"}).status_code == 422
    monkeypatch.setattr(main, "MAX_AUDIO_BYTES", 10)
    assert client.post("/api/v1/transcribe-and-draft", json={**base, "audio_base64": AUDIO_B64}).status_code == 422


def test_scan_supply_sheet_mock(client, monkeypatch):
    r = client.post("/api/v1/scan-supply-sheet", json={"image_base64": IMAGE_B64, "discharge_id": 402})
    assert r.status_code == 200, r.text
    got = {i["item_name"]: i["surgical_qty_used"] for i in r.json()["items"]}
    assert got == {"Hemostatic Gauze Pad": 4, "Skin Stapler": 1}
    assert client.post("/api/v1/scan-supply-sheet", json={"image_base64": "???", "discharge_id": 1}).status_code == 422
    monkeypatch.setattr(main, "MAX_IMAGE_BYTES", 5)
    assert client.post("/api/v1/scan-supply-sheet", json={"image_base64": IMAGE_B64, "discharge_id": 1}).status_code == 422


# ---------------------------------------------------------------- Phase 12: patient SMS + tracker link
TOKEN = "tok_demo_1234567890"


def test_sms_mock_mode_returns_text_and_link(monkeypatch):
    from services import sms_notifier
    monkeypatch.setattr(settings, "use_mock_sms", True)
    monkeypatch.setattr(settings, "public_odoo_base_url", "http://hospital.test/")
    res = asyncio.run(sms_notifier.send_patient_tracker_sms("+971501234567", TOKEN))
    assert (res.sent, res.mode) == (False, "mock_logged")
    assert res.tracking_url == "http://hospital.test/discharge/track/" + TOKEN
    assert res.tracking_url in res.message_preview and "discharge review" in res.message_preview


def test_sms_copay_message_and_demo_phone_fallback(monkeypatch):
    from services import sms_notifier
    monkeypatch.setattr(settings, "use_mock_sms", True)
    monkeypatch.setattr(settings, "patient_demo_phone", "+971500000000")
    res = asyncio.run(sms_notifier.send_patient_tracker_sms("", TOKEN, "ready_for_payment", 150))
    assert res.to_number == "+971500000000"
    assert "AED 150.00 co-pay" in res.message_preview


def _twilio_on(monkeypatch):
    for key, value in (("use_mock_sms", False), ("twilio_account_sid", "ACtest"),
                       ("twilio_auth_token", "secret"), ("twilio_from_number", "+15005550006")):
        monkeypatch.setattr(settings, key, value)


def test_sms_twilio_path_posts_form_and_reports_sid(monkeypatch):
    from services import sms_notifier
    _twilio_on(monkeypatch)
    seen = {}

    class FakeResponse:
        status_code = 201

        def json(self):
            return {"sid": "SM123"}

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, auth=None, data=None):
            seen.update(url=url, auth=auth, data=data)
            return FakeResponse()

    monkeypatch.setattr(sms_notifier.httpx, "AsyncClient", FakeClient)
    res = asyncio.run(sms_notifier.send_patient_tracker_sms("+971501234567", TOKEN))
    assert (res.sent, res.mode, res.twilio_sid) == (True, "twilio", "SM123")
    assert seen["url"].endswith("/Accounts/ACtest/Messages.json") and seen["auth"] == ("ACtest", "secret")
    assert seen["data"]["To"] == "+971501234567" and seen["data"]["From"] == "+15005550006"


def test_sms_twilio_failure_falls_back_without_raising(monkeypatch):
    from services import sms_notifier
    _twilio_on(monkeypatch)

    class Boom:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            raise RuntimeError("network down")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(sms_notifier.httpx, "AsyncClient", Boom)
    res = asyncio.run(sms_notifier.send_patient_tracker_sms("+971501234567", TOKEN))
    assert (res.sent, res.mode) == (False, "twilio_failed_logged")
    assert "network down" in res.error and res.tracking_url in res.message_preview


def test_verify_sends_sms_only_with_a_tracking_token(client, monkeypatch):
    monkeypatch.setattr(settings, "use_mock_sms", True)
    plain = client.post("/api/v1/verify", json=pt402_verify()).json()
    assert plain["sms"] is None
    body = pt402_verify(rev=2, rev_hash=HASH2)
    body.update(tracking_token=TOKEN, patient_phone="+971501234567")
    with_sms = client.post("/api/v1/verify", json=body).json()
    assert with_sms["sms"]["mode"] == "mock_logged" and TOKEN in with_sms["sms"]["tracking_url"]
    # the stored audit snapshot never carries the phone number or tracker link
    stored = client.get("/api/v1/snapshot/402/2").json()
    assert stored["sms"] is None


def test_notify_sms_endpoint(client, monkeypatch):
    monkeypatch.setattr(settings, "use_mock_sms", True)
    res = client.post("/api/v1/notify-sms", json={
        "tracking_token": TOKEN, "to_number": "+971501234567", "stage_event": "released"})
    assert res.status_code == 200 and "cleared to go home" in res.json()["message_preview"]
    assert client.post("/api/v1/notify-sms", json={"tracking_token": "x"}).status_code == 422


# ---------------------------------------------------------------- Phase 13: past medical / surgical history
HISTORY = [
    {"condition_name": "Type 2 Diabetes", "history_type": "medical", "status": "active", "diagnosis_date": "2015"},
    {"condition_name": "Open Cholecystectomy", "history_type": "surgical", "status": "resolved",
     "diagnosis_date": "2018"},
]


def _draft_context(history=None) -> dict:
    return {
        "patient_code": "PT-402", "bed_number": "402",
        "raw_handoff_note": "POD1 laparoscopic appendectomy. Start Amoxicillin 500mg TID.",
        "medications": [{"drug_name": "Amoxicillin", "dosage": "500mg", "frequency": "TID"}],
        "allergies": [{"allergen": "Penicillin", "reaction": "Anaphylaxis", "criticality": "high"}],
        "past_medical_history": history or [],
    }


def test_mock_draft_lists_history_and_adapts_plan_for_diabetes():
    from services.ai_llm_client import _mock_draft
    summary, prompts, _ = _mock_draft(_draft_context(HISTORY))
    assert "**Past Medical History**: Type 2 Diabetes (2015, active)" in summary
    assert "**Past Surgical History**: Open Cholecystectomy (2018, resolved)" in summary
    plan = next(ln for ln in summary.splitlines() if ln.startswith("- **Plan**"))
    assert "monitor blood glucose closely" in plan and "post-operative stress" in plan
    assert "Type 2 Diabetes control" in summary.splitlines()[-1]  # follow-up line
    assert any("active conditions" in p for p in prompts)


def test_mock_draft_without_history_has_no_history_sections():
    from services.ai_llm_client import _mock_draft
    summary, prompts, _ = _mock_draft(_draft_context())
    assert "Past Medical History" not in summary and "Past Surgical History" not in summary
    assert "**Plan**" not in summary
    assert not any("active conditions" in p for p in prompts)


def test_resolved_or_surgical_history_adds_no_condition_advice():
    from services.ai_llm_client import _mock_draft
    resolved = [dict(HISTORY[0], status="resolved"), HISTORY[1]]
    summary, _, _ = _mock_draft(_draft_context(resolved))
    assert "blood glucose" not in summary and "**Plan**" not in summary
    assert "Type 2 Diabetes (2015, resolved)" in summary


def test_other_chronic_conditions_get_their_own_advice():
    from services.ai_llm_client import _mock_draft
    hx = [{"condition_name": "Hypertension", "history_type": "medical", "status": "active"}]
    summary, _, _ = _mock_draft(_draft_context(hx))
    assert "check blood pressure regularly" in summary


def test_system_prompt_requires_history_and_plan_adaptation():
    from services.ai_llm_client import _SYSTEM_PROMPT
    for needle in ("Past Medical History", "Past Surgical History", "monitor blood glucose closely",
                   "do not invent medication changes"):
        assert needle in _SYSTEM_PROMPT


def test_history_schema_is_lightweight_and_strict():
    from pydantic import ValidationError
    from schemas import HistoryItem
    item = HistoryItem(condition_name="Asthma")
    assert (item.history_type, item.status) == ("medical", "active")
    with pytest.raises(ValidationError):
        HistoryItem(condition_name="Asthma", create_uid=1)


def test_pruner_keeps_declared_history_and_drops_nothing_clinical():
    req = DraftRequest(discharge_id=1, patient_code="PT-402", patient_name="Arabith", raw_handoff_note="note",
                       past_medical_history=HISTORY)
    pruned, _ = prune_and_anonymize(req)
    assert [h["condition_name"] for h in pruned["past_medical_history"]] == [
        "Type 2 Diabetes", "Open Cholecystectomy"]


def test_pruner_maps_raw_odoo_history_rows_to_history_items():
    raw_rows = [
        {"id": 7, "create_uid": [1, "Admin"], "write_date": "2026-01-01", "patient_id": [1, "Arabith"],
         "display_name": "Type 2 Diabetes", "condition_name": "Type 2 Diabetes", "history_type": "medical",
         "diagnosis_date": "2015", "status": "active"},
        {"id": 8, "condition_name": "", "history_type": "medical"},  # blank rows are ignored
    ]
    req = DraftRequest(discharge_id=1, patient_code="PT-402", patient_name="Arabith", raw_handoff_note="note",
                       orm_history=raw_rows)
    pruned, stats = prune_and_anonymize(req)
    assert pruned["past_medical_history"] == [
        {"condition_name": "Type 2 Diabetes", "history_type": "medical", "status": "active",
         "diagnosis_date": "2015"}]
    assert "orm_history" not in pruned and "create_uid" not in str(pruned) and "Arabith" not in str(pruned)
    assert stats.before_tokens > stats.after_tokens


def test_draft_endpoint_returns_history_and_glucose_advice(client):
    body = {"discharge_id": 402, "patient_code": "PT-402", "patient_name": "Arabith", "bed_number": "402",
            "raw_handoff_note": "POD1 laparoscopic appendectomy. Start Amoxicillin 500mg TID.",
            "past_medical_history": HISTORY}
    data = client.post("/api/v1/draft", json=body).json()
    summary = data["ai_draft_summary"]
    assert "Past Medical History" in summary and "Past Surgical History" in summary
    assert "monitor blood glucose closely" in summary


def test_live_llm_receives_the_history_in_its_context(monkeypatch):
    from services import ai_llm_client
    monkeypatch.setattr(settings, "use_mock_llm", False)
    monkeypatch.setattr(settings, "ai_base_url", "http://llm.test")
    monkeypatch.setattr(settings, "ai_api_key", "k")
    seen = {}

    async def fake_call(context):
        seen.update(context)
        return {"summary": "**Discharge Summary**\n- **Patient**: x", "review_prompts": [],
                "proposed_medications": []}

    monkeypatch.setattr(ai_llm_client, "_call_endpoint", fake_call)
    stats = TokenStats(before_tokens=1, after_tokens=1, saved_tokens=0, saved_pct=0.0)
    asyncio.run(ai_llm_client.generate_discharge_summary(_draft_context(HISTORY), 1, stats))
    assert [h["condition_name"] for h in seen["past_medical_history"]] == [
        "Type 2 Diabetes", "Open Cholecystectomy"]
