"""Deterministic FastMCP safety tools, executed concurrently with asyncio.gather()."""
import asyncio
import time

from fastmcp import FastMCP

from schemas import (
    AllergyItem,
    BillingAuditOutput,
    InsuranceClaimOutput,
    MedicationItem,
    PharmacyCheckOutput,
    PharmacyConflict,
    ReconciledLine,
    SupplyDiscrepancy,
    SupplyItem,
    VerifyRequest,
)

mcp = FastMCP("clearpath-safety")

# Allergen group -> drug name fragments that cross-react.
_PENICILLIN_CLASS = (
    "amoxicillin", "ampicillin", "penicillin", "piperacillin", "nafcillin",
    "oxacillin", "dicloxacillin", "augmentin", "clavulanate",
)
_OTHER_BETA_LACTAM = (  # lower, but real, cross-reactivity
    "cefazolin", "cephalexin", "cefuroxime", "ceftriaxone", "cefepime",
    "cefdinir", "cefpodoxime", "meropenem", "imipenem", "aztreonam",
)
_PENICILLIN_ALLERGENS = ("penicillin", "beta-lactam", "beta lactam", "amoxicillin", "ampicillin")


async def check_pharmacy_safety(
    medications: list[MedicationItem], allergies: list[AllergyItem]
) -> PharmacyCheckOutput:
    """Flag beta-lactam cross-reactivity against a documented penicillin allergy."""
    conflicts: list[PharmacyConflict] = []
    for allergy in allergies:
        allergen = allergy.allergen.lower()
        if not any(a in allergen for a in _PENICILLIN_ALLERGENS):
            continue
        for med in medications:
            drug = med.drug_name.lower()
            if any(d in drug for d in _PENICILLIN_CLASS):
                conflicts.append(
                    PharmacyConflict(
                        drug_name=med.drug_name,
                        allergen=allergy.allergen,
                        severity="critical",
                        reason=(
                            f"{med.drug_name} is a penicillin-class beta-lactam; patient has a "
                            f"documented {allergy.allergen} allergy ({allergy.reaction or 'reaction not recorded'}, "
                            f"{allergy.criticality} criticality). Contraindicated."
                        ),
                    )
                )
            elif any(d in drug for d in _OTHER_BETA_LACTAM):
                conflicts.append(
                    PharmacyConflict(
                        drug_name=med.drug_name,
                        allergen=allergy.allergen,
                        severity="warning",
                        reason=(
                            f"{med.drug_name} is a beta-lactam with possible cross-reactivity to "
                            f"{allergy.allergen} allergy. Pharmacist review required."
                        ),
                    )
                )
    if conflicts:
        crit = sum(c.severity == "critical" for c in conflicts)
        return PharmacyCheckOutput(
            status="flagged",
            conflicts=conflicts,
            summary=f"{len(conflicts)} allergy conflict(s) found ({crit} critical).",
        )
    return PharmacyCheckOutput(status="clear", summary="No allergy conflicts found.")


async def audit_surgical_billing(supplies: list[SupplyItem]) -> BillingAuditOutput:
    """Flag every item where surgical_qty_used exceeds billed_qty."""
    found = [
        SupplyDiscrepancy(
            item_name=s.item_name,
            surgical_qty_used=s.surgical_qty_used,
            billed_qty=s.billed_qty,
            discrepancy_qty=s.surgical_qty_used - s.billed_qty,
        )
        for s in supplies
        if s.surgical_qty_used > s.billed_qty
    ]
    if found:
        detail = ", ".join(f"+{d.discrepancy_qty} {d.item_name}" for d in found)
        return BillingAuditOutput(
            status="flagged",
            discrepancies=found,
            summary=f"Unbilled supplies: {detail}.",
        )
    return BillingAuditOutput(status="clear", summary="Supply log matches billing.")


# Register as MCP tools without rebinding the plain coroutines (keeps them directly callable).
mcp.tool(name="check_pharmacy_safety")(check_pharmacy_safety)
mcp.tool(name="audit_surgical_billing")(audit_surgical_billing)


# Aliases captured at import so the insurance worker is independent of the other two workers.
_pharmacy_impl = check_pharmacy_safety
_billing_impl = audit_surgical_billing

# keywords -> (ICD-10, description, CPT, description). Small demo table; unmapped -> coder review.
_PROCEDURE_CODES = [
    (("appendectomy", "appendicitis"), "K35.80", "Unspecified acute appendicitis",
     "44970", "Laparoscopic appendectomy"),
    (("total knee", "knee replacement", "knee arthroplasty"), "M17.9", "Osteoarthritis of knee, unspecified",
     "27447", "Total knee arthroplasty"),
    (("cholecystectomy", "gallbladder"), "K80.20", "Calculus of gallbladder without obstruction",
     "47562", "Laparoscopic cholecystectomy"),
    (("hernia",), "K40.90", "Unilateral inguinal hernia without obstruction or gangrene",
     "49650", "Laparoscopic inguinal hernia repair"),
]


async def generate_tpa_insurance_claim(verify_req: VerifyRequest) -> InsuranceClaimOutput:
    """Pre-fill a TPA pre-authorization claim; hold it while any discrepancy is unresolved."""
    text = (verify_req.procedure_text or "").lower()
    match = next((m for m in _PROCEDURE_CODES if any(k in text for k in m[0])), None)
    icd, icd_desc, cpt, cpt_desc = match[1:] if match else ("", "", "", "")

    pharmacy = await _pharmacy_impl(verify_req.medications, verify_req.allergies)
    billing = await _billing_impl(verify_req.supplies)

    holds: list[str] = []
    if pharmacy.status == "flagged":
        holds.append("Pharmacy allergy conflict unresolved: " + ", ".join(c.drug_name for c in pharmacy.conflicts) + ".")
    if billing.status == "flagged":
        holds.append("Unreconciled surgical supplies: " + billing.summary.removeprefix("Unbilled supplies: "))
    if not match:
        holds.append("Procedure not mapped to ICD-10 / CPT; coder review required.")
    if not (verify_req.tpa_provider and verify_req.policy_number):
        holds.append("TPA provider / policy number missing for this patient.")

    lines = [
        ReconciledLine(
            item_name=s.item_name, qty_used=s.surgical_qty_used, qty_billed=s.billed_qty,
            qty_claimable=s.surgical_qty_used,
            note=f"+{s.surgical_qty_used - s.billed_qty} unbilled" if s.surgical_qty_used > s.billed_qty else "",
        )
        for s in verify_req.supplies
    ]
    necessity = (
        f"Patient underwent {cpt_desc.lower()} for {icd_desc.lower()}. The procedure and supplies are "
        "documented in the clinical handoff note and reconciled against the surgical supply log."
        if match else ""
    )
    status = "hold_pending_reconciliation" if holds else "ready_for_review"

    rows = "\n".join(
        f"| {l.item_name} | {l.qty_used} | {l.qty_billed} | {l.qty_claimable} | {l.note} |" for l in lines
    ) or "| (none) | | | | |"
    hold_md = "\n".join(f"- {h}" for h in holds) or "- None"
    markdown = (
        "# TPA Pre-Authorization / Claim Form (draft)\n"
        f"**Status:** {'HOLD - pending reconciliation' if holds else 'READY for administration review'}\n\n"
        "| Field | Value |\n|---|---|\n"
        f"| Patient | {verify_req.patient_code or '-'} |\n"
        f"| TPA provider | {verify_req.tpa_provider or '-'} |\n"
        f"| Policy number | {verify_req.policy_number or '-'} |\n"
        f"| Diagnosis (ICD-10) | {(icd + ' - ' + icd_desc) if icd else 'UNMAPPED'} |\n"
        f"| Procedure (CPT) | {(cpt + ' - ' + cpt_desc) if cpt else 'UNMAPPED'} |\n\n"
        "## Itemised supplies\n"
        "| Item | Used | Billed | Claimable | Note |\n|---|---|---|---|---|\n"
        f"{rows}\n\n"
        f"## Medical necessity\n{necessity or 'Not available until the procedure is mapped.'}\n\n"
        f"## Hold reasons\n{hold_md}"
    )
    return InsuranceClaimOutput(
        claim_status=status, tpa_provider=verify_req.tpa_provider, policy_number=verify_req.policy_number,
        icd10_code=icd, cpt_code=cpt, reconciled_line_items=lines,
        medical_necessity_summary=necessity, hold_reasons=holds, filled_claim_markdown=markdown,
    )


mcp.tool(name="generate_tpa_insurance_claim")(generate_tpa_insurance_claim)


async def run_parallel_mcp_checks(
    req: VerifyRequest,
) -> tuple[PharmacyCheckOutput, BillingAuditOutput, InsuranceClaimOutput, float]:
    """Run all three workers concurrently. Returns (pharmacy, billing, insurance, elapsed_ms)."""
    start = time.perf_counter()
    pharmacy, billing, insurance = await asyncio.gather(
        check_pharmacy_safety(req.medications, req.allergies),
        audit_surgical_billing(req.supplies),
        generate_tpa_insurance_claim(req),
    )
    return pharmacy, billing, insurance, round((time.perf_counter() - start) * 1000, 2)
