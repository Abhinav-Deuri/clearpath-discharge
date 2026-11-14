"""ClearPath sidecar: draft (AI), verify (parallel MCP checks), snapshot export."""
import logging
from contextlib import asynccontextmanager

import aiosqlite
from fastapi import BackgroundTasks, FastAPI, HTTPException

import database
from config import settings
from schemas import (
    AuditPacketSnapshot,
    DraftRequest,
    DraftResponse,
    SmsDispatchResult,
    SmsNotifyRequest,
    SupplyScanRequest,
    SupplyScanResponse,
    VerifyRequest,
    VoiceDraftRequest,
    VoiceDraftResponse,
)
from services import mcp_tools
from services.ai_llm_client import generate_discharge_summary
from services.multimodal_ingestor import (
    MAX_AUDIO_BYTES,
    MAX_IMAGE_BYTES,
    decode_payload,
    extract_supply_checklist_from_image,
    transcribe_doctor_voice_note,
)
from services.odoo_rpc import push_findings_to_odoo
from services.sms_notifier import send_patient_tracker_sms, twilio_enabled
from services.token_pruner import prune_and_anonymize

log = logging.getLogger("clearpath")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await database.init_db()
    yield


app = FastAPI(title="ClearPath Sidecar", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    try:
        async with aiosqlite.connect(settings.sqlite_path) as db:
            await db.execute("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "sqlite": db_ok,
        "mock_llm": settings.use_mock_llm or not (settings.ai_base_url and settings.ai_api_key),
        "mock_sms": not twilio_enabled(),
    }


@app.post("/api/v1/draft", response_model=DraftResponse)
async def draft(req: DraftRequest) -> DraftResponse:
    pruned, stats = prune_and_anonymize(req)
    return await generate_discharge_summary(pruned, req.discharge_id, stats)


@app.post("/api/v1/transcribe-and-draft", response_model=VoiceDraftResponse)
async def transcribe_and_draft(req: VoiceDraftRequest) -> VoiceDraftResponse:
    try:
        decode_payload(req.audio_base64, MAX_AUDIO_BYTES, "Audio")
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    text = await transcribe_doctor_voice_note(req.audio_base64, req.filename)
    draft_req = DraftRequest(
        discharge_id=req.discharge_id, patient_code=req.patient_code, patient_name=req.patient_name,
        bed_number=req.bed_number, raw_handoff_note=text, medications=req.medications,
        allergies=req.allergies, supplies=req.supplies,
    )
    pruned, stats = prune_and_anonymize(draft_req)
    draft_resp = await generate_discharge_summary(pruned, req.discharge_id, stats)
    return VoiceDraftResponse(**draft_resp.model_dump(), transcribed_text=text)


@app.post("/api/v1/scan-supply-sheet", response_model=SupplyScanResponse)
async def scan_supply_sheet(req: SupplyScanRequest) -> SupplyScanResponse:
    try:
        decode_payload(req.image_base64, MAX_IMAGE_BYTES, "Image")
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return SupplyScanResponse(items=await extract_supply_checklist_from_image(req.image_base64))


async def _push(snapshot: AuditPacketSnapshot) -> None:
    # Re-check right before sending: a newer revision may have arrived meanwhile.
    if not await database.verify_not_stale(snapshot.discharge_id, snapshot.revision_id):
        log.warning("Skip Odoo push: %s is stale", snapshot.snapshot_id)
        return
    try:
        await push_findings_to_odoo(snapshot)
    except Exception as exc:
        log.error("Odoo push failed for %s: %s", snapshot.snapshot_id, exc)


@app.post("/api/v1/verify", response_model=AuditPacketSnapshot)
async def verify(req: VerifyRequest, background: BackgroundTasks) -> AuditPacketSnapshot:
    if not await database.register_revision(req.discharge_id, req.revision_id, req.revision_hash):
        raise HTTPException(409, f"Stale revision: Rev {req.revision_id} superseded by a newer one")

    pharmacy, billing, insurance, elapsed_ms = await mcp_tools.run_parallel_mcp_checks(req)

    # Stale-Write Guard: a newer revision may have registered while workers ran.
    if not await database.verify_not_stale(req.discharge_id, req.revision_id):
        raise HTTPException(409, f"Stale revision: Rev {req.revision_id} invalidated mid-run")

    flagged = "flagged" in (pharmacy.status, billing.status) or insurance.claim_status != "ready_for_review"
    status = "flagged" if flagged else "cleared"
    snapshot = AuditPacketSnapshot(
        snapshot_id=database.make_snapshot_id(req.revision_hash, req.revision_id),
        discharge_id=req.discharge_id,
        revision_id=req.revision_id,
        revision_hash=req.revision_hash,
        status=status,
        pharmacy=pharmacy,
        billing=billing,
        insurance_output=insurance,
        elapsed_ms=elapsed_ms,
    )
    try:
        await database.save_revision_snapshot(
            req.discharge_id, req.revision_id, req.revision_hash, status, snapshot.model_dump()
        )
    except database.StaleRevisionError as exc:
        raise HTTPException(409, str(exc))

    if req.push_to_odoo:
        background.add_task(_push, snapshot)
    if req.tracking_token:
        # The doctor just locked the revision: text the patient their live tracker link. The result rides
        # on the response only (the snapshot saved above stays free of phone numbers and tracker links).
        sms = await send_patient_tracker_sms(
            req.patient_phone or "", req.tracking_token, "started", req.copay_amount)
        return snapshot.model_copy(update={"sms": sms})
    return snapshot


@app.post("/api/v1/notify-sms", response_model=SmsDispatchResult)
async def notify_sms(req: SmsNotifyRequest) -> SmsDispatchResult:
    """Odoo-triggered patient SMS: co-pay ready, or packet released."""
    return await send_patient_tracker_sms(req.to_number, req.tracking_token, req.stage_event, req.copay_amount)


@app.get("/api/v1/snapshot/{discharge_id}/{revision_id}", response_model=AuditPacketSnapshot)
async def snapshot(discharge_id: int, revision_id: int) -> AuditPacketSnapshot:
    data = await database.get_snapshot(discharge_id, revision_id)
    if not data:
        raise HTTPException(404, "Snapshot not found")
    return AuditPacketSnapshot(**data)


@app.delete("/api/v1/discharge/{discharge_id}/state")
async def reset_discharge_state(discharge_id: int) -> dict:
    """Demo helper: wipe sidecar revision state so a discharge can restart at Rev 1."""
    await database.clear_discharge_state(discharge_id)
    return {"discharge_id": discharge_id, "cleared": True}
