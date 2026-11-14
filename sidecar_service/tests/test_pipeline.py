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
        patient_name="John Doe",
        bed_number="402",
        raw_handoff_note="John Doe tolerated appendectomy well. Start Amoxicillin 500mg.",
        medications=[MedicationItem(drug_name="Amoxicillin", dosage="500mg", frequency="TID")],
        create_uid=[2, "Administrator"],
        write_date="2026-11-13 10:00:00",
        __last_update="2026-11-13 10:00:00",
        message_ids=[1, 2, 3, 4, 5, 6, 7, 8],
        message_follower_ids=[9, 10, 11],
        display_name="John Doe / Bed 402",
    )
    pruned, stats = prune_and_anonymize(req)
    flat = str(pruned)
    assert "John Doe" not in flat
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
            "patient_name": "John Doe",
            "bed_number": "402",
            "raw_handoff_note": "John Doe post-op appendectomy, start Amoxicillin 500mg.",
            "medications": [{"drug_name": "Amoxicillin", "dosage": "500mg", "frequency": "TID"}],
            "create_uid": [2, "Administrator"],
            "message_ids": list(range(30)),
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["used_mock"] is True
    assert "John Doe" not in body["ai_draft_summary"]
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
        "patient_code": "PT-402", "patient_name": "John Doe", "bed_number": "402",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert "Amoxicillin 500mg" in body["transcribed_text"]
    assert "John Doe" not in body["ai_draft_summary"] and "PT-402" in body["ai_draft_summary"]
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
