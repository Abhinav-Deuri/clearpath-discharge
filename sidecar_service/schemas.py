"""Strict Pydantic v2 schemas shared by every sidecar endpoint and service."""
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MedicationItem(_Strict):
    drug_name: str
    dosage: str = ""
    frequency: str = ""


class AllergyItem(_Strict):
    allergen: str
    reaction: str = ""
    criticality: Literal["low", "medium", "high"] = "high"


class SupplyItem(_Strict):
    item_name: str
    surgical_qty_used: int = Field(ge=0)
    billed_qty: int = Field(ge=0)


class TokenStats(_Strict):
    before_tokens: int
    after_tokens: int
    saved_tokens: int
    saved_pct: float

    def summary(self) -> str:
        return (
            f"Raw Odoo payload: {self.before_tokens:,} tokens -> "
            f"Pruned payload: {self.after_tokens:,} tokens "
            f"({self.saved_pct:.1f}% saved)"
        )


class DraftRequest(BaseModel):
    """Odoo may send raw ORM metadata as extra keys; the pruner strips it."""

    model_config = ConfigDict(extra="allow")

    discharge_id: int
    patient_code: str
    patient_name: str = ""
    bed_number: str = ""
    raw_handoff_note: str
    medications: list[MedicationItem] = []
    allergies: list[AllergyItem] = []
    supplies: list[SupplyItem] = []


class DraftResponse(_Strict):
    discharge_id: int
    ai_draft_summary: str
    review_prompts: list[str]
    model_used: str
    used_mock: bool
    token_stats: TokenStats
    proposed_medications: list[MedicationItem] = []


class VoiceDraftRequest(BaseModel):
    """Doctor voice note + patient context (name is pruned before any AI call)."""

    model_config = ConfigDict(extra="allow")

    discharge_id: int
    audio_base64: str
    filename: str = "voice_note.wav"
    patient_code: str
    patient_name: str = ""
    bed_number: str = ""
    medications: list[MedicationItem] = []
    allergies: list[AllergyItem] = []
    supplies: list[SupplyItem] = []


class VoiceDraftResponse(DraftResponse):
    transcribed_text: str


class SupplyScanRequest(_Strict):
    image_base64: str
    discharge_id: int


class SupplyScanResponse(_Strict):
    items: list[SupplyItem]


class VerifyRequest(_Strict):
    discharge_id: int
    revision_id: int = Field(ge=1)
    revision_hash: str
    patient_code: str = ""
    medications: list[MedicationItem]
    allergies: list[AllergyItem]
    supplies: list[SupplyItem]
    push_to_odoo: bool = True
    procedure_text: str = ""  # handoff note / diagnosis text used for ICD-10 / CPT mapping
    tpa_provider: str = ""
    policy_number: str = ""
    # Patient SMS: sent only when a tracking token is supplied (the doctor's lock, not internal re-verifies).
    patient_phone: str | None = None
    tracking_token: str | None = None
    copay_amount: float = 150.0


class SmsNotifyRequest(_Strict):
    tracking_token: str = Field(min_length=8)
    to_number: str = ""
    stage_event: Literal["started", "ready_for_payment", "released"] = "started"
    copay_amount: float = Field(default=150.0, ge=0)


class SmsDispatchResult(_Strict):
    sent: bool
    mode: str  # twilio | mock_logged | twilio_failed_logged
    to_number: str
    tracking_url: str
    message_preview: str
    twilio_sid: str | None = None
    stage_event: str = "started"
    error: str = ""


class PharmacyConflict(_Strict):
    drug_name: str
    allergen: str
    severity: Literal["warning", "critical"]
    reason: str


class PharmacyCheckOutput(_Strict):
    status: Literal["clear", "flagged"]
    conflicts: list[PharmacyConflict] = []
    summary: str


class SupplyDiscrepancy(_Strict):
    item_name: str
    surgical_qty_used: int
    billed_qty: int
    discrepancy_qty: int


class BillingAuditOutput(_Strict):
    status: Literal["clear", "flagged"]
    discrepancies: list[SupplyDiscrepancy] = []
    summary: str


class ReconciledLine(_Strict):
    item_name: str
    qty_used: int
    qty_billed: int
    qty_claimable: int
    note: str = ""


class InsuranceClaimOutput(_Strict):
    claim_status: Literal["ready_for_review", "hold_pending_reconciliation"]
    tpa_provider: str = ""
    policy_number: str = ""
    icd10_code: str = ""
    cpt_code: str = ""
    reconciled_line_items: list[ReconciledLine] = []
    medical_necessity_summary: str = ""
    hold_reasons: list[str] = []
    filled_claim_markdown: str = ""


class AuditPacketSnapshot(_Strict):
    snapshot_id: str  # e.g. adb6cd43_Rev1
    discharge_id: int
    revision_id: int
    revision_hash: str
    status: Literal["flagged", "cleared"]
    pharmacy: PharmacyCheckOutput
    billing: BillingAuditOutput
    insurance_output: InsuranceClaimOutput | None = None  # None for pre-Phase-5 snapshots
    sms: SmsDispatchResult | None = None  # response only; never stored with the audit snapshot
    elapsed_ms: float
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
