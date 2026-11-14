# ClearPath Discharge — PLAN.md (Odoo 20, 4 phases, HITL gated)

## Context
Hackathon project: AI-assisted surgical discharge with human sign-off. Odoo 20 holds the record + UI. A FastAPI/FastMCP **sidecar** drafts the summary with an LLM and runs two deterministic safety checks (pharmacy, billing). Humans approve at two gates. Edits after lock auto-invalidate approvals (Stale-Write Guard).
Demo patient: John Doe → `PT-402`, Bed 402, Penicillin anaphylaxis, appendectomy note prescribing Amoxicillin 500mg, gauze 4 used/2 billed, skin stapler 1 used/0 billed.

`PLAN.md` does not exist yet. First action after approval: write this file to the project root as `PLAN.md` (phase prompts say "Read PLAN.md").

## Format rules (copied from `odoo-server/workshop/Workshop/*`)
- Module root: `__init__.py`, `__manifest__.py`, `models/`, `security/`, `views/`, `data/`.
- Manifest = flat dict: `name`, `version`, `depends`, `data` (ordered: security → views → menus → data), `application: True`, `installable: True`.
- One model per file in `models/`, imported in `models/__init__.py`.
- Views per model: `<model>_views.xml` with `ir.actions.act_window` (`view_mode` = `list,form`), form, **`<list>`** (not `<tree>`). Menus in a separate `*_menus.xml`. Inherit views in `res_*_views.xml` style.
- Access: `security/ir.model.access.csv`, header `id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink`.
- Naming: `access_<model_underscored>`, view ids `<model>_view_form|list`, action ids `<model>_action`.

## Odoo 20 notes
- Local source in `odoo-server/` is **19.0**; no 20.0 source on disk. Plan targets 20.0 syntax = 19 syntax: `<list>`, `<chatter/>`, `invisible="expr"` / `readonly="expr"` attributes (no `attrs`, no `states`), `models.Constraint`-free simple ORM, `@api.depends`, `mail.thread` + `mail.activity.mixin`.
- Risk: Docker image `odoo:20.0` may not be published. Fallback: build from `odoo-server/` source or use `odoo:19.0` with `version` bumped later. Verify with `docker pull odoo:20.0` at Phase 3.
- Module manifest `version` = `'20.0.1.0.0'`.

## Cross-check vs Gemini share (read via its share RPC, ~115k chars)
Verdict: **same plan.** Same file tree, same 4 phases, same stages, same demo case, same HITL idea. Differences, all merged below:
| Item | Gemini original | Final (this plan) |
|---|---|---|
| Odoo | 17/18 (later switched to 20 by user) | **20.0**, `<list>` views |
| AI vars / client | `AMD_*`, `amd_llm_client.py` | `AI_*`, `ai_llm_client.py` (user asked for a different, generic AI key) |
| Python | 3.11+ | 3.12+ |
| Sidecar deps | fastapi, uvicorn, pydantic, openai, fastmcp, aiosqlite, httpx, pydantic-settings | same; add `openai` + `pydantic-settings` to `requirements.txt` |
| Verify call | async: `BackgroundTasks` + JSON-RPC callback so Odoo UI stays snappy | adopt. `/draft` is sync with 30s timeout (~17s LLM) |
| Record rules | `ir.rule` limiting writes by stage | adopt in `clearpath_security.xml` |
| Edit-after-lock | `state` back to `draft`, statuses `stale`, gate locked | adopt (both) |
| HITL checkpoints | 4 | adopt all 4, see below |
| Stretch | token-savings badge ("2,140 → 820 tokens, 61.7% saved"), QWeb PDF packet | badge is Phase 4 must-have (user prompt); PDF = optional stretch, cut first |
| Demo convenience | Administrator sees all role buttons | adopt |
Builder checkpoints (agent STOPS and waits): **CP1** env + AI endpoint (keys, JSON-mode support, latency); **CP2** MCP rule output + stale guard + snapshot hash format `adb6cd43_Rev1`; **CP3** Odoo form UX + RBAC (single admin view vs 4 logins), plus localhost launch; **CP4** live latency + scope cut (enable mock fallback if call >20s; drop PDF first).

## Localhost run (explicit requirement)
Not covered before; now a hard requirement. Everything runs on this Windows machine via Docker Desktop (already installed).
| Service | Container port | Localhost URL |
|---|---|---|
| Odoo | 8069 | http://localhost:8069 |
| Sidecar (FastAPI + MCP) | 8000 | http://localhost:8000/health , /docs |
| Postgres | 5432 | localhost:5432 (dev only) |
- `docker-compose.yml` publishes these with `ports:`; services talk internally by name (`odoo`, `sidecar_service`, `db`), never via `localhost`.
- Healthchecks on `db` and `sidecar_service`; Odoo `depends_on` db healthy.
- Pre-flight in Phase 3, before writing compose: `docker --version`, `docker pull odoo:20.0`. **If `odoo:20.0` does not exist, fall back to `odoo:19.0`** (matches local `odoo-server`; module syntax is 20-compatible) and report it. No silent switch.
- Sidecar can also run alone without Docker: `uvicorn main:app --port 8000` with `USE_MOCK_LLM=true` (Phase 1 tests and demo need no API key).
- Phase 3 checkpoint proves localhost: `curl localhost:8000/health` → ok, `http://localhost:8069` login page loads, module installs, one full draft→verify round trip from the Odoo button.

## Tech / UX stack
| Layer | Choice |
|---|---|
| ERP/UI | Odoo 20: form with `statusbar`, `ribbon`, `alert` banners, `badge` widgets, notebook tabs, chatter |
| Sidecar | Python 3.12, FastAPI, FastMCP, Pydantic v2, aiosqlite, httpx |
| LLM | OpenAI-compatible endpoint (`AI_API_KEY`, `AI_BASE_URL`, `AI_MODEL_NAME`) + deterministic mock (`USE_MOCK_LLM=true`) |
| Infra | Docker Compose: `postgres:15`, `odoo:20.0`, `sidecar_service`, shared network |
| Test | pytest + pytest-asyncio |
| UI polish | Colour-coded statusbar, red/amber/green decoration on flag lines (`decoration-danger`), `badge` for Revision + Snapshot Hash, `oe_button_box` smart buttons, chatter audit trail |

## Directory layout (final)
```text
clearpath-hackathon/
├── PLAN.md
├── docker-compose.yml
├── .env.example
├── odoo_addons/clearpath_discharge/
│   ├── __init__.py
│   ├── __manifest__.py
│   ├── security/{clearpath_security.xml, ir.model.access.csv}
│   ├── models/{__init__.py, clearpath_patient.py, clearpath_lines.py, clearpath_discharge.py}
│   ├── views/{clearpath_discharge_views.xml, clearpath_patient_views.xml, clearpath_menus.xml}
│   └── data/demo_patient_pt402.xml
└── sidecar_service/
    ├── Dockerfile, requirements.txt, main.py, config.py, schemas.py, database.py
    ├── services/{__init__.py, token_pruner.py, ai_llm_client.py, mcp_tools.py, odoo_rpc.py}
    └── tests/test_pipeline.py
```
Note: `project_structure.md` says `amd_llm_client.py`; Phase 1 says `ai_llm_client.py`. **Use `ai_llm_client.py`** (provider-neutral).

## Human-in-the-loop design
| Gate | Who | Action | Unlocks |
|---|---|---|---|
| HITL #0 | Doctor | Reviews AI draft + review prompts, clicks Lock Revision & Verify | Verification run |
| HITL #1 | Pharmacist | `action_pharmacist_approve` (only if no unresolved flag, same revision) | `pharmacy_status=approved` |
| HITL #1 | Billing Officer | `action_billing_approve` (same revision) | `billing_status=approved` |
| HITL #2 | Coordinator | `action_coordinator_release` — only when both approved on current `revision_id` | `released`, locked packet |
AI never approves anything. Flags only block; humans clear them. Any edit post-lock → revision bump → both approvals reset to `stale`, gate re-locks.
Dev checkpoints (for you, the builder): CP1 pytest output, CP2 `.env` keys, CP3 stack launch commands, plus Phase 2 stop to inspect ORM.

State machine: `draft → locked → verifying → flagged | cleared → released`.

---
## Phase 1 — Sidecar, schemas, SQLite  (STOP at HITL CP 1 & 2)
Files (all under `sidecar_service/` + root `.env.example`):
- `requirements.txt`, `Dockerfile` (python:3.12-slim, uvicorn).
- `config.py`: env loader → `AI_API_KEY, AI_BASE_URL, AI_MODEL_NAME, SQLITE_PATH, ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD, USE_MOCK_LLM`.
- `schemas.py`: Pydantic v2 strict models — `DraftRequest, DraftResponse, VerifyRequest, MedicationItem, AllergyItem, SupplyItem, PharmacyCheckOutput, BillingAuditOutput, AuditPacketSnapshot`.
- `database.py`: async SQLite; table of snapshots keyed `<hash8>_Rev<N>` (e.g. `adb6cd43_Rev1`); table of active revision per `discharge_id`; `verify_not_stale(discharge_id, revision_id)`.
- `services/token_pruner.py`: strip Odoo ORM metadata (`id`, `create_uid`, `write_date`, `__last_update`…), replace `John Doe`→`PT-402`, return `{before_tokens, after_tokens, saved, pct}` (also feeds Phase 4 `token_savings_summary`).
- `services/ai_llm_client.py`: async httpx call; structured appendectomy summary + review prompts; deterministic mock when `USE_MOCK_LLM=true`.
- `services/mcp_tools.py`: FastMCP tools run via `asyncio.gather()`:
  - `check_pharmacy_safety` → beta-lactam cross-reactivity (Amoxicillin vs Penicillin anaphylaxis).
  - `audit_surgical_billing` → used vs billed (+2 gauze, +1 stapler).
- `services/odoo_rpc.py`: `push_findings_to_odoo()` JSON-RPC → `clearpath.discharge.apply_mcp_findings`.
- `main.py`: `/health`, `POST /api/v1/draft`, `POST /api/v1/verify`, `GET /api/v1/snapshot/{discharge_id}/{revision_id}`; verify rejects stale revision (409).
- `tests/test_pipeline.py`: parallel execution (timing/overlap), stale-write guard, pruner PII + savings, flag detection.
Exit: run `pytest`, show output, wait for `.env` keys.

## Phase 2 — Odoo models, revision-lock ORM, demo data  (STOP to inspect ORM)
- `__manifest__.py`: `depends: ['base','mail']`, data order security → views → menus → demo data.
- `clearpath_patient.py`: `clearpath.patient` (name, mrn `PT-402`, bed) + `clearpath.allergy` (allergen, reaction, criticality).
- `clearpath_lines.py`: `clearpath.medication.line` (drug_name, dosage, frequency, is_flagged, conflict_reason); `clearpath.supply.line` (item_name, surgical_qty_used, billed_qty, computed `discrepancy_qty`, is_flagged).
- `clearpath_discharge.py` (`mail.thread`): fields `state, revision_id(int=1), revision_hash, raw_handoff_note, ai_draft_summary, review_prompts, pharmacy_status, billing_status, coordinator_gate_locked, audit_snapshot_json`.
  - `write()` override = **Stale-Write Guard**: if locked and `raw_handoff_note`/`ai_draft_summary`/medication lines change → `revision_id += 1`, recompute `revision_hash` (sha256 of notes+meds), statuses → `'stale'`, gate locked, chatter message.
  - Methods: `action_generate_ai_draft`, `action_lock_revision_and_verify`, `apply_mcp_findings`, `action_pharmacist_approve`, `action_billing_approve`, `action_coordinator_release`. Each approve checks group + current revision + no open flag.
- `data/demo_patient_pt402.xml`: John Doe, Penicillin/Anaphylaxis/high, Amoxicillin 500mg note, gauze 4/2, stapler 1/0.

## Phase 3 — Security, views, Docker  (STOP at HITL CP 3)
- `clearpath_security.xml`: 4 groups `group_clearpath_doctor|pharmacist|billing|coordinator`; admin (`base.group_system`) implied into all four for demo.
- `ir.model.access.csv`: rows per model × group (doctor rw discharge, pharmacist/billing rw own checks, coordinator release, all read patient).
- `clearpath_discharge_views.xml`: header with `statusbar` + stage buttons (Generate Draft, Lock & Verify, Pharmacist Approve, Billing Approve, Release); `alert` banners (pharmacy / billing flagged, stale revision); badges for Revision ID + Snapshot Hash; notebook tabs: Clinical Draft, Pharmacy Safety Check, Surgical Billing Audit, Raw Audit Snapshot JSON; `<chatter/>`; `<list>` + search views.
- `clearpath_patient_views.xml`, `clearpath_menus.xml` (root app menu → Discharges, Patients).
- `docker-compose.yml`: `db: postgres:15`, `odoo: odoo:20.0` (mount `./odoo_addons`), `sidecar_service` (build `./sidecar_service`, env from `.env`), shared `clearpath_net`; Odoo calls `http://sidecar_service:8000`, sidecar calls `http://odoo:8069`.
- Deliver exact commands: `docker compose up -d --build`, then install: `docker compose exec odoo odoo -d clearpath -i clearpath_discharge --stop-after-init` (+ restart).

## Phase 4 — Demo polish  (final)
- `clearpath_discharge.py`: button method `action_simulate_late_doctor_edit()`: Amoxicillin 500mg → Azithromycin 500mg, bump Rev 1→2 via the guard, mark Rev 1 checklist/snapshot invalid, post chatter audit note.
- `token_savings_summary` computed/stored char field (from pruner stats) shown on form.
- `clearpath_discharge_views.xml`: demo button "Simulate Late Doctor Edit (Trigger Stale-Write Guard)", savings field.
- Verify: Pharmacist + Billing both sign same revision → gate unlocks → `action_coordinator_release()` builds final locked packet.

## Verification (end-to-end)
1. `pytest sidecar_service/tests` green.
2. `docker compose up`; `curl http://localhost:8000/health` ok; `http://localhost:8069` loads; install module, no errors in log.
3. Demo script: open PT-402 → Generate Draft → Lock & Verify → both flags raised → Simulate Late Edit → Rev 2, approvals stale, banner → re-verify clean → Pharmacist + Billing approve → Coordinator releases → packet locked, chatter trail complete.
4. Negative tests: approve on stale revision rejected; non-coordinator cannot release.

---
# Phase 5 — Multimodal inputs + TPA insurance agent (upgrade of the built prototype)

## Context
Phases 1-4 are built and passing (sidecar 11 tests, Odoo 12 tests). Phase 5 adds three capabilities from `updated_architecture_flow.md` and `gemini-code-1794574640002.md`: doctor voice dictation, nurse supply-sheet photo scan, and a third concurrent worker that pre-fills a TPA insurance claim with a third sign-off (Admin). The Revision Lock / Stale-Write Guard stays the backbone: every approval is tied to one revision and invalidated by later edits.

**User decisions (confirmed):**
- Supply guard: bump revision on item / `surgical_qty_used` changes only; editing `billed_qty` does NOT bump (billing keeps fixing discrepancies after lock).
- ASR + vision: **mock by default, live-ready config.** Separate `ASR_*` and `VISION_*` settings (OpenAI-compatible formats); any live failure falls back to the deterministic mock.

## Design decisions (my recommendations, flag if you disagree)
1. **Gate #2 becomes 3-way:** `cleared` only when pharmacy, billing AND insurance are `approved` on the same revision. State flow unchanged (`flagged` while any flag or any sign-off is outstanding).
2. **Approval pattern reused:** `action_admin_approve_insurance` mirrors billing: blocked while a hold is unresolved, computed locally from current lines (no unresolved medication flag, no supply discrepancy, ICD-10/CPT mapped). No sidecar call needed.
3. **Voice must feed the pharmacy check.** A transcript alone creates no medication lines, so the pharmacy worker would see nothing. The draft call also returns `proposed_medications` (LLM JSON; mock regex for "Drug 500mg TID"); Odoo fills `medication_line_ids` only when it is empty. The doctor still reviews before locking (HITL).
4. **Scan semantics:** the photo is a *used* tally. `action_scan_nurse_checklist` matches lines by item name (case-insensitive), updates `surgical_qty_used`, keeps existing `billed_qty`, creates unseen items with `billed_qty=0`. Draft state only.
5. **ICD-10/CPT mapping** is a small keyword table (appendectomy -> K35.80 / 44970, plus a few common cases such as TKA M17.11 / 27447, cholecystectomy K80.20 / 47562). Unmapped procedure -> `claim_status="hold_pending_reconciliation"` with hold reason "procedure not mapped; coder review". Fits the earlier "adapt to the actual condition" request.
6. **Backward compatible:** `AuditPacketSnapshot.insurance_output` and `apply_mcp_findings(insurance_result=None)` are optional, so snapshots saved before Phase 5 still load.
7. **Privacy flag:** audio and photos cannot be de-identified like text. Fine for the synthetic PT-402 demo and for mock mode; real patients would need an on-prem or contracted ASR/vision provider. Sidecar must never log audio/image bytes.

## Part A - Sidecar (`sidecar_service/`)
| File | Change |
|---|---|
| `schemas.py` | Add `VoiceDraftRequest` (`audio_base64`, `filename`, plus `DraftRequest` context fields), `VoiceDraftResponse` (`transcribed_text` + `DraftResponse` fields + `proposed_medications`), `SupplyScanRequest` (`image_base64`, `discharge_id`), `SupplyScanResponse` (`items: list[SupplyItem]`, `used_mock`), `InsuranceClaimOutput` (`claim_status` hold/ready, `tpa_provider`, `policy_number`, `icd10_code`, `cpt_code`, `reconciled_line_items`, `medical_necessity_summary`, `hold_reasons`, `filled_claim_markdown`). `VerifyRequest` gains `procedure_text`, `tpa_provider`, `policy_number` (optional). `AuditPacketSnapshot` gains `insurance_output: InsuranceClaimOutput \| None`. Snapshot status = `flagged` if any of the three flags. |
| `config.py` + `.env.example` | `ASR_BASE_URL/ASR_API_KEY/ASR_MODEL_NAME`, `VISION_BASE_URL/VISION_API_KEY/VISION_MODEL_NAME`; each falls back to mock when unset or `USE_MOCK_LLM=true`; timeouts reuse `AI_TIMEOUT_SECONDS`. |
| `services/multimodal_ingestor.py` (new) | `transcribe_doctor_voice_note(audio_base64, filename)`: live = `POST {ASR_BASE_URL}/audio/transcriptions` multipart (Whisper-style); mock/stub/failure = deterministic PT-402 post-op transcript proposing Amoxicillin 500mg TID. `extract_supply_checklist_from_image(image_base64)`: live = vision chat/completions with `image_url` data URI, JSON output; mock/failure = Hemostatic Gauze Pad used 4 / billed 2, Skin Stapler used 1 / billed 0. Validate size/type; never log payloads. |
| `services/ai_llm_client.py` | Draft JSON gains `proposed_medications` (live prompt + mock regex); reused by voice path after transcription (transcript -> existing `prune_and_anonymize` -> `generate_discharge_summary`, so PII rules are unchanged). |
| `services/mcp_tools.py` | New `generate_tpa_insurance_claim(verify_req)`: mapping table, hold logic (reuses `check_pharmacy_safety`/`audit_surgical_billing` results or recomputes), builds `filled_claim_markdown`. Registered with `mcp.tool(name=...)(fn)` like the others. `run_parallel_mcp_checks` returns `(pharmacy, billing, insurance, elapsed_ms)` via one `asyncio.gather` of all three. |
| `main.py` | `POST /api/v1/transcribe-and-draft`, `POST /api/v1/scan-supply-sheet`; `/verify` puts insurance in the snapshot (stale guard unchanged: register, run, re-check, save). |
| `services/odoo_rpc.py` | `push_findings_to_odoo` also sends `insurance_result`. |
| `tests/test_pipeline.py` | Update existing gather/verify tests for the 4-tuple; add: transcribe endpoint (mock), scan endpoint (mock), 3-way parallel timing (3 x 0.3s < 0.55s), insurance hold vs ready, unmapped-procedure hold, stale Rev1 rejected so no insurance written. |

## Part B - Odoo addon (`odoo_addons/clearpath_discharge/`)
| File | Change |
|---|---|
| `security/clearpath_security.xml` | `group_clearpath_nurse` ("Nurse - Bedside Supply Upload"), `group_clearpath_admin` ("Hospital Admin / Insurance Officer"); same pattern as existing groups, `base.user_admin` + `user_root` in `user_ids` so admin keeps one-screen demo access. |
| `security/ir.access.csv` (**Odoo 20 `ir.access` format**, `operation` = crud letters) | Nurse: discharge `ru`, supply line `cru`, patient/medication `r`. Admin: discharge `ru`, all others `r`. Existing groups: read on new fields is implicit. |
| `models/clearpath_patient.py` + `data/demo_patient_pt402.xml` | `insurance_provider`, `policy_number` on the patient (fabricated demo values, e.g. "Demo TPA Health" / "POL-402-DEMO"). |
| `models/clearpath_discharge.py` | New fields: `voice_note_audio` (Binary, attachment=True), `voice_note_filename`, `nurse_checklist_image` (Binary, attachment=True), `insurance_status` (pending/flagged/approved/stale), `tpa_claim_document` (Text), `icd10_cpt_codes` (Char). `_bump_revision` also resets `insurance_status='stale'`, clears claim/codes and Admin approval. Guard trigger: add supply-line hook limited to item / `surgical_qty_used` changes (`clearpath_lines.py` create/write/unlink, mirroring the medication hook; `billed_qty` excluded). New buttons (all return notification + `soft_reload`, single-flight advisory lock like the draft button): `action_transcribe_voice_and_draft`, `action_scan_nurse_checklist`, `action_admin_approve_insurance`. `apply_mcp_findings(..., insurance_result=None)` sets insurance status, codes, claim text. `_update_gate` and `action_coordinator_release` require all three approvals. Release packet + `action_reset_demo` include insurance fields. `_compute_revision_hash` also covers supply `item/used` so the hash reflects the guard. |
| `models/clearpath_lines.py` | Supply-line create/write/unlink guard as above (still refused when released). |
| `views/clearpath_discharge_views.xml` | **Multimodal Bedside Inputs** group at the top of the form: audio uploader (`widget="binary"` + filename) with button "🎙️ Transcribe Voice & Draft"; image preview (`widget="image"`, small size) with button "📷 Scan Nurse Supply Sheet"; both visible only in Draft. New notebook tab **Insurance & TPA Pre-Auth (Admin)**: `insurance_status` badge, `icd10_cpt_codes`, formatted `tpa_claim_document`, hold reasons. Header button "Admin: Approve Insurance Claim" (`groups=group_clearpath_admin`). List view gets an insurance badge; alert banners for insurance hold. |
| `views/clearpath_report.xml` | Audit card + PDF: insurance card (status, ICD-10/CPT, hold reasons), fourth signature line (Admin), timeline row. |
| `tests/test_discharge_flow.py` | Extend `fake_post` with `/transcribe-and-draft`, `/scan-supply-sheet`, and `insurance_output` in `/verify`; update `test_06` (3 approvals); add: insurance flagged then approved, gate stays locked until all three, late edit stales insurance, `billed_qty` edit does NOT bump but `surgical_qty_used` edit does, scan updates lines, voice fills note/draft/meds only when meds empty, nurse and admin role gates. |
| `DEMO.md`, `PLAN.md` | New demo steps (voice -> draft, photo scan, insurance tab, admin sign-off) and a Phase 5 section in the root `PLAN.md`. |

## Build order (each step verifiable alone)
1. Sidecar schemas + config + `multimodal_ingestor.py` + endpoints (mock) + tests -> `pytest` green.
2. Insurance worker + 3-way gather + snapshot/odoo_rpc updates + tests.
3. Odoo security groups + access rows + patient insurance fields.
4. Odoo model fields, supply guard, three new actions, 3-way gate; update `fake_post` and tests; run Odoo tests in a scratch DB (`MSYS_NO_PATHCONV=1`, drop DB after).
5. Views + report + DEMO.md; upgrade the real DB (`-u clearpath_discharge`), rebuild sidecar, restart Odoo, run the shell flow end to end, render the PDF.
HITL checkpoints for you: after step 2 (review the generated TPA claim text), after step 5 (click through voice, scan, insurance sign-off in the browser).

## Verification
- `pytest` (sidecar) and Odoo `--test-tags /clearpath_discharge` both green.
- Browser: upload any small audio file named like the demo -> Transcribe fills note + draft + Amoxicillin line; upload any image -> Scan fills gauze 4 / stapler 1; Lock -> three flags (pharmacy, billing, insurance hold); late edit -> Rev 2 and all three STALE; re-lock, fix billed qty, three sign-offs -> gate opens -> release -> PDF shows ICD-10/CPT and four signature lines.
- Negative: non-admin cannot approve insurance; release blocked with only two approvals; `billed_qty` edit after lock leaves the revision unchanged; stale Rev 1 verify returns 409.

## Known risks
- Live ASR/vision are untested until real endpoints exist; mock + fallback keep the demo safe.
- Large audio via Odoo Binary -> base64 -> sidecar adds latency; cap upload size (e.g. 10 MB audio, 5 MB image) with a clear error.
- Extending `_compute_revision_hash` changes hashes of existing records; harmless (recomputed on next lock).
- Spec's emoji in button labels kept as requested.

---
# Phase 6 — UI refactor: Smart Buttons + approval Wizards (pop-up cards)

## Context
The discharge form has 6 notebook tabs plus 3 header sign-off buttons. Pharmacy, Billing and Insurance review are buried in tabs on the clinical form. Goal: a clean clinical form (Clinical Draft + Audit Packet only) with a stat-button row (Pharmacy / Billing / Insurance) at top right. Each stat button opens a focused modal (`target="new"`) showing only that department's evidence and an "Approve & Resolve" footer. Gates, HITL rules and the Stale-Write Guard stay exactly as built; this is a UX move, not a logic change.

## Decisions (flag if you disagree)
1. **Approval logic moves into the wizards; shared helpers stay on the model.** `_require_group`, `_assert_signable`, `_insurance_blockers`, `_update_gate`, `_soft_reload` stay on `clearpath.discharge` and are reused. `action_pharmacist_approve`, `action_billing_approve`, `action_admin_approve_insurance` are removed from the model and re-implemented as `action_approve` on each wizard (same checks, same chatter text, same fields written, then `_update_gate()` and `soft_reload`).
2. **Pharmacy wizard is read-only on medications.** Editing a medication bumps the revision (Stale-Write Guard), so the pharmacist reviews and approves; changes are made on the Clinical Draft. The Billing wizard lets billing edit `billed_qty` only (does not bump), as today.
3. **Surgical supplies stay reachable from the main form.** Removing the Billing tab would hide the lines the nurse scan creates and the doctor edits. A compact "Surgical Supplies Used" list (item + used qty, editable in draft) goes under the medications on the Clinical Draft page. Billed qty is edited only in the Billing wizard.
4. **Stat buttons show icon + count/label + coloured status badge**: "1 Allergy Flag" (red), "Clear" (green), "Stale" (amber), "Not run" (grey).

## Part A — Wizards (`wizard/`, new)
- `wizard/__init__.py` imports the three files; root `__init__.py` adds `from . import wizard`.
- Common fields: `discharge_id` (Many2one, required, default from `context['default_discharge_id']`), related read-only `patient_id`, `bed_number`, `revision_id`, status; `can_approve` (Boolean compute) and `blocker_html` (Html compute) explain why approval is blocked.
- `pharmacy_approval_wizard.py` (`clearpath.pharmacy.approval.wizard`): related `ai_draft_summary_html`, `medication_line_ids` (readonly), `allergy_ids` (readonly), `conflict_html` built from flagged lines (drug + `conflict_reason`), approver / approved-on when already signed. `action_approve`: `_require_group(GROUP_PHARMACIST)`, `_assert_signable`, stale check, unresolved-flag check, write `pharmacy_status/pharmacist_id/pharmacy_approved_on`, chatter, `_update_gate`, `soft_reload`.
- `billing_approval_wizard.py` (`clearpath.billing.approval.wizard`): `supply_line_ids` = `related='discharge_id.supply_line_ids', readonly=False` (editable `billed_qty`; item / used readonly in the wizard list), computed used-vs-billed totals and open items. `action_approve` = existing billing logic. Fallback if a writable related One2many misbehaves in the web client: small transient line model (item, used, billed) written back on approve.
- `insurance_approval_wizard.py` (`clearpath.insurance.approval.wizard`): `icd10_cpt_codes`, `tpa_provider`, `policy_number`, `insurance_hold_reasons`, `tpa_claim_html`, blockers from `_insurance_blockers()`. `action_approve` = existing insurance logic (clears hold reasons).
- Transient models need no `ir.access.csv` rows; approval is gated in Python via `_require_group` and in the view via `groups=`.

## Part B — Wizard views (`wizard/clearpath_wizards_views.xml`, new)
- One form `ir.ui.view` + one `ir.actions.act_window` with `target="new"` per wizard (`pharmacy_approval_wizard_action`, ...).
- Look: hero header (patient, bed, Rev badge, status badge) → prominent alert (`alert-danger` conflicts / `alert-warning` discrepancies / `alert-success` "All clear") → evidence card(s) → footer. Footer: **Approve & Resolve** (`btn-primary`, `invisible="not can_approve"`, `groups=` for that department) and **Cancel** (`special="cancel"`; reads "Close" once approved).
- `static/src/scss/clearpath_wizards.scss` in `web.assets_backend`: rounded cards, soft shadow, colour-coded left border per department, tidy hero row. Built on Odoo bootstrap variables so light/dark follow the theme.

## Part C — Main model (`models/clearpath_discharge.py`)
- New non-stored computes: `pharmacy_conflict_count` (flagged med lines), `unbilled_item_count` (supply lines with `discrepancy_qty > 0`), `insurance_hold_count` (non-empty lines of `insurance_hold_reasons`), plus `pharmacy_button_text`, `billing_button_text`, `insurance_button_text` ("1 Allergy Flag", "2 Unbilled", "Clear", "Approved", "Stale", "Not run"). `@api.depends` on lines, statuses, hold reasons.
- Opener methods `action_open_pharmacy_wizard`, `action_open_billing_wizard`, `action_open_insurance_wizard`: load the `target="new"` act_window via `self.env['ir.actions.act_window']._for_xml_id(...)`, set `context={'default_discharge_id': self.id}`.
- Update messages that say "see the X tab" to point at the matching button.

## Part D — Main form (`views/clearpath_discharge_views.xml`)
- Remove header buttons: Pharmacist Sign-off, Billing Sign-off, Admin: Approve Insurance. Keep Generate Draft, Lock & Run Checks, Release, Simulate Late Edit, Reset Demo, PDF.
- Remove notebook pages: Pharmacy Safety Check, Surgical Billing Audit, Insurance & TPA Pre-Auth. Keep Clinical Draft (+ supplies list) and Audit Packet.
- Add `<div class="oe_button_box" name="button_box">` at the top of `<sheet>` with three `oe_stat_button`s (`type="object"`, `invisible="state == 'draft'"`): `fa-medkit` Pharmacy, `fa-calculator` Billing, `fa-shield` Insurance; each shows count/label, `o_stat_text`, and the coloured status badge.
- The red/amber banners stay as a summary strip, with copy updated to "Open the Pharmacy / Billing / Insurance button". Multimodal Bedside Inputs, hero badges and token-savings banner unchanged.

## Part E — Manifest, tests, docs
- `__manifest__.py` data order: security, `security/ir.access.csv`, `views/clearpath_report.xml`, **`wizard/clearpath_wizards_views.xml`**, discharge views, patient views, menus, demo data; scss added to `assets`.
- `tests/test_discharge_flow.py` (12 call sites of the old approve methods): route sign-offs through the wizards (`create({'discharge_id': ...}).action_approve()`). New tests: openers return `target == 'new'`; smart-button counts/labels (flag, unbilled, clear, stale); `can_approve` false while flagged; non-department users blocked; billed-qty edit in billing wizard does not bump revision.
- `DEMO.md` click path ("click Pharmacy / Billing / Insurance, then Approve & Resolve") and `PLAN.md` updated.

## Verification
1. Upgrade DB: `docker compose run --rm odoo odoo -d clearpath -u clearpath_discharge --stop-after-init --no-http`; no XML/validation errors.
2. Odoo tests in a scratch DB (`MSYS_NO_PATHCONV=1 ... --test-enable --test-tags /clearpath_discharge`, then drop it); sidecar `pytest` still 21 pass.
3. Shell sanity check: `get_view` on the form and each wizard succeeds; each opener returns `ir.actions.act_window`, `target=new`, right `res_model` and context; full flow (lock → three wizard approvals → release) passes.
4. Browser pass on PT-402: stat buttons appear after locking, each opens a floating card, Approve & Resolve closes it and refreshes badges. The Chrome extension was not connected last time; if it still isn't, I'll say the visual pass is unverified and list what to click.

## Risks
- Writable related One2many in the billing wizard (fallback above).
- Odoo 20 view quirks (`<list>`, `invisible=` expressions, no `attrs`) — caught by steps 1 and 3.
- Removing the old model methods: only tests and the three form buttons call them.

---
# Phases 7 + 8 (+ summary look) — native Contacts/Invoicing, animated UI, readable summary

## Context
Phases 1-6 are built, tested (27 Odoo tests, 21 sidecar tests) and pushed. Three requests are open: (7) make `clearpath.patient` a real Odoo Contact and generate a draft invoice from the reconciled supply log; (8) animated, icon-driven state cues; and a readability pass on the AI discharge summary, which currently shows raw markdown (`**Discharge Summary**`, bullets) in a plain text box.
Findings from exploring the live stack: `account` and `contacts` are already installed in the `clearpath` DB, a Sales journal (INV) and generic chart (50 accounts) exist, so draft invoices can be created. `clearpath_patient` has 2 rows and `name` is NOT NULL, so the field removal needs a migration. No product records exist. `models/res_partner.py` in the working tree is the user's own empty stub (not imported; leave it alone).

## User decisions (confirmed)
- Animated alerts live **both** places: one slim animated strip on the form (most urgent issue only) + full animated cards in the Review Alerts pop-up.
- Invoice price comes from a new **Unit Price** column on supply lines (billing fills it in the Billing pop-up); invoice lines are free-text.
- Smart buttons are icon-first with a **tiny department label** and a hover tooltip.

## Part 1 — Phase 7: delegation inheritance + invoicing
1. `models/clearpath_patient.py`: `_inherits = {'res.partner': 'partner_id'}`; add `partner_id = Many2one('res.partner', required=True, ondelete='cascade')`; delete `name`; keep `patient_code`, `bed_number`, `allergy_ids`, `discharge_ids`. `insurance_provider` / `policy_number` stay on the patient (clinical/insurance data, not contact data). `_order` stays `patient_code`. Existing code using `patient_id.name` keeps working through delegation.
2. **Migration** `migrations/20.0.1.1.0/pre-migration.py` (bump manifest version to `20.0.1.1.0`): add `partner_id` column; for every `clearpath_patient` row insert a `res_partner` (name copied) and set `partner_id`; `DROP NOT NULL` / drop the old `name` column so new patients can be created. Without this, `-u` fails on the 2 existing rows (PT-402 and another).
3. `models/clearpath_discharge.py`: `invoice_id = Many2one('account.move', readonly=True, copy=False)`; `action_generate_invoice()`: group check (billing/admin), requires `billing_status == 'approved'` or `state in ('cleared','released')`, refuses if an invoice exists, creates `account.move` (`move_type='out_invoice'`, `partner_id=patient_id.partner_id`, `invoice_origin=name`, `ref` = patient code + revision) with one `invoice_line_ids` per supply line (`name=item_name`, `quantity=billed_qty` = the reconciled quantity, `price_unit=unit price`), links `invoice_id`, posts chatter, returns a notification + `soft_reload`. Lines with quantity 0 are skipped. The invoice stays **draft** (a human posts it in Invoicing; consistent with HITL).
4. `models/clearpath_lines.py` + billing pop-up: add `price_unit` Float to `clearpath.supply.line` (edit does NOT bump the revision, like `billed_qty`); add the column to `clearpath.billing.approval.line` and copy it back in `_apply_billed_quantities`. Add Unit Price to the Clinical Draft supply list? No: billing-only.
5. `__manifest__.py`: `depends` += `contacts`, `account`; `security/clearpath_security.xml`: `group_clearpath_billing` and `group_clearpath_admin` `implied_ids` += `account.group_account_invoice` so billing users may create invoices.
6. Views: header button **Generate Final Invoice** (`icon fa-file-text-o`, visible when `(billing_status == 'approved' or state == 'cleared') and not invoice_id`); smart button **Invoice** (`fa-money`, shows invoice number/state, `invisible="not invoice_id"`) opening the `account.move` via `action_view_invoice` (`res_model='account.move'`, `res_id`, form). Patient form: show the delegated contact fields (email, phone, mobile) so the Contacts integration is visible; patient list gets an email column. Contacts app shows the same partner.
7. `data/demo_patient_pt402.xml`: no change needed (`name` is passed to the delegated create, Odoo creates the partner). Existing DB record is covered by the migration.
8. Tests: new patient creates a partner; migration-style check that `patient.partner_id.name == patient.name`; invoice creation (lines, quantity from billed_qty, price, partner, draft); blocked before billing approval and for non-billing users; second click refused; `price_unit` edit after lock keeps the revision.

## Part 2 — Phase 8: animated visual cues (`views/clearpath_discharge_views.xml`, `static/src/scss/clearpath_wizards.scss`)
1. **Ribbons**: red `Allergy Hold` (`bg_color="text-bg-danger"`, `invisible="pharmacy_status != 'flagged'"`), green `Cleared` (`text-bg-success`, `invisible="coordinator_gate_locked or state == 'released'"`, so it does not overlap the existing Released ribbon). Ribbons are mutually exclusive.
2. **Animated strip + pop-up cards**: on the form, one slim `alert` strip chosen by priority (pharmacy conflict > billing discrepancy > insurance hold) with `<i class="fa fa-exclamation-triangle fa-2x fa-pulse me-3"/>` and `<h3 class="mb-0"><strong>CRITICAL:</strong> Penicillin Allergy Conflict</h3>`-style one-liner (text from `pharmacy_summary`-derived conflict). Billing uses `fa-shake`, insurance uses `fa-beat`. Same animated icons on the cards inside the Review Alerts pop-up (`_alert_items()` icon classes extended). Clicking the strip opens the Review Alerts pop-up.
3. **Conditional icon smart buttons**: for Pharmacy, Billing, Insurance, four `oe_stat_button`s each, switched by `invisible`: pending `fa-cog fa-spin text-muted`; flagged `fa-exclamation-circle fa-beat-fade text-danger`; approved `fa-shield text-success`; plus a fourth `stale` state (`fa-refresh text-warning`) because the brief's three states would leave a stale record blank. All 12 call the existing `action_open_*_wizard`. Each has a tiny `o_stat_text` label and `title` tooltip. Hidden in draft. Replaces the current badge-text buttons (the count/label computes stay for the tooltips and tests).
4. **Animation safety**: the bundled Font Awesome may be 4.7, which has `fa-spin`/`fa-pulse` but not `fa-beat`, `fa-beat-fade`, `fa-shake` (the Odoo image's CSS could not be inspected). Add small `@keyframes` polyfills for those three classes in the scss (harmless if FA6 already defines them) and a `prefers-reduced-motion` rule that disables all animation.
5. **Pop-up behaviour check**: `action_open_pharmacy_wizard` / `billing` / `insurance` already return `type: ir.actions.act_window`, `target: new` (covered by `test_18`); keep that test and add the same assertion for the new states.

## Part 3 — Readable AI discharge summary
Problem: `ai_draft_summary` is stored as markdown and shown raw in a plain `Text` field (see the pasted example).
1. New renderer `summary_to_html()` in `models/clearpath_discharge.py` (next to `md_to_html`, which stays as the fallback): turns a heading plus `- **Label**: value` bullets into a card: title row with a document icon, then one row per label with an icon, the label in small caps and the value beside it (table layout with inline styles, so it also renders in the wkhtmltopdf PDF). Icons by label: Patient `fa-user`, Procedure `fa-scissors`, Hospital Course `fa-heartbeat`, Incisions `fa-bandage`-style `fa-medkit`, Plan `fa-home`, Medications `fa-medkit`/pills, Allergies `fa-exclamation-circle` (red text), Follow-up `fa-calendar`. The **Items for clinician review** row becomes an amber callout with a warning icon. Unknown labels use a neutral dot; text without bullets falls back to `md_to_html`. Dark-theme safe (no hard-coded backgrounds except tinted callouts with alpha).
2. Wire it: `_compute_ai_draft_summary_html` calls it (used by the form, the pharmacy pop-up and the PDF audit body).
3. Form (Clinical Draft page): show the rendered card by default, with the raw text in a collapsed "Edit summary text" area so doctors can still edit (edits still trigger the Stale-Write Guard). The note and review prompts keep their current fields.
4. Sidecar prompt (`ai_llm_client.py`): keep the strict `- **Label**: value` bullet format and the fixed label set so the renderer's icons match; update `_mock_draft` to the same labels. No schema change.
5. Tests: renderer output for the pasted sample (labels, icon classes, review callout, allergy red), fallback for plain text, escaping of `<script>` in values.

## Verification
1. `-u clearpath_discharge` on the live DB (migration runs on the 2 existing patients) with no errors; Odoo HTTP 200 after restart.
2. Odoo tests in a scratch DB (`MSYS_NO_PATHCONV=1 ... --test-enable --test-tags /clearpath_discharge`, drop it after) all green, sidecar `pytest` green.
3. Odoo shell: patient -> partner link; PT-402 keeps its name; full flow lock -> 3 approvals -> Generate Final Invoice creates a **draft** `account.move` with the right partner, lines and totals; `invoice_id` set; smart-button action opens the move; PDF still renders (summary card + insurance section).
4. Views: `get_view` compiles for form, wizards, patient; check ribbons, strip, 12 conditional buttons and the summary card in the arch.
5. Browser pass on PT-402 if the Claude Chrome extension is connected (it has not been); otherwise state that the animations and card look are unverified and list what to look at.

## Risks
- Delegation inheritance migration on existing rows (NOT NULL `name`, FK `partner_id`): handled by the pre-migration script; take a `pg_dump` of the `clearpath` DB first.
- Invoice creation needs access to `account.move`: depends on the implied `account.group_account_invoice`; admin passes.
- Animation classes missing in the bundled Font Awesome: polyfilled.
- The Phase 7 spec says "billing_status approved or state cleared"; both are honoured, but an invoice before insurance/pharmacy sign-off is possible by design.

### Phases 7 + 8 as built
- `clearpath.patient` delegates to `res.partner` (`_inherits`); pre-migration `20.0.1.1.0` creates a Contact for each existing patient and drops the old `name` column (dry-run on a restored copy of the live DB first). `price_unit` on supply lines (editable in the Billing pop-up, never bumps the revision); `action_generate_invoice` creates a DRAFT `account.move` from billed quantities; Invoice smart button.
- Animated cues: Allergy Hold / Cleared ribbons, animated strip, 4-state icon buttons per department; `fa-beat`, `fa-beat-fade`, `fa-shake` polyfilled in scss; reduced-motion respected.
- AI summary renders as a card (`summary_to_html`), `---` rules stripped, sidecar prompt/mock use `- **Label**: value`.
- Icons: Odoo 20 has no Font Awesome (it ships Material Symbols + odoo_ui_icons), so `fa fa-*` rendered blank. All icons are now `<i class="oi" data-icon="material_name"/>` (names verified against the font subset); button `icon="..."` takes the Material name; `oi-spin` for the gear; `cp-pulse` / `cp-beat` / `cp-beat-fade` / `cp-shake` are our own keyframes.

### Phase 9 as built
- `models/res_partner.py` (Contacts side): `patient_ids`, `patient_count`, `action_open_clearpath_patient`; `views/res_partner_views.xml` adds a **Patient** / **Admit as Patient** smart button on the Contact form. Patient form: Contact details on top, "Clinical Admission Details" group below.
- Flagged smart buttons show the count next to a pulsing red warning triangle, driven by `pharmacy_conflict_count`, `unbilled_item_count`, `insurance_hold_count`; ready/approved/stale keep their icon states.
- Decision: a billed-quantity fix does NOT bump the revision (that would wipe the pharmacy sign-off). Instead `_refresh_insurance_claim` re-runs the Insurance worker on the same revision, rebuilds the claim, and withdraws an existing insurance approval (gate re-locks). Item / used-quantity edits still bump the revision and stale all three.
- Reset Demo now drops the invoice link (draft invoice deleted; a posted one stays in Invoicing).

### Duplicate supply lines + Phase 10 as built
- Root cause of repeated supply lines: Reset Demo deleted the demo lines, then every module upgrade re-created them from the `noupdate` demo XML (deleted XML ids come back). Demo content now lives only in `DEMO_NOTE_TEMPLATE` / `DEMO_MEDICATIONS` / `DEMO_SUPPLIES` (Python), seeded by `_seed_demo_content()` (install hook + Reset Demo, idempotent, single-flight locked). Demo XML keeps only patient, allergy and discharge. A supply item can appear once per discharge (case-insensitive constraint).
- The demo patient is "Arabith"; the note is built from the patient's real name and bed, so a rename can never leave a stale name.
- Phase 10 layout: prioritized header (primary Generate AI Draft / Lock / Invoice / Release, muted utility buttons), Patient & Governance banner (short `#hash` pill, gate locked/unlocked pill), boxed cards for Multimodal Bedside Inputs (handoff note inside the voice card) and the Structured AI Discharge Summary (single heading), department smart buttons with their own icon and state colour.
