# 🏥 ClearPath — AI-assisted hospital discharge, with a human on every gate

> Discharge delays cost hospitals beds, money and patient trust. ClearPath lets AI **draft and flag** in seconds, while **people approve** every risky decision. The AI never signs anything off.

Built on **Odoo 20** + a **FastAPI AI sidecar**. Runs fully offline in demo mode, no API keys needed.

## ✨ What it does

| | |
|---|---|
| 🎙️ **Voice → draft** | Doctor dictates. Speech-to-text (NVIDIA Riva / Whisper) becomes a structured discharge summary. |
| 📸 **Photo → supplies** | Snap the nurse's handwritten supply sheet. A vision model turns it into billable quantities. |
| ⚡ **3 safety checks in parallel** | Pharmacy (drug vs allergy), Billing (supplies used vs billed) and TPA Insurance (ICD-10 / CPT claim) run at the same time via `asyncio.gather()` as FastMCP tools. |
| 🔒 **Revision lock + stale-write guard** | Every approval is tied to one revision hash. If the doctor edits late, old results and approvals are invalidated automatically. |
| ✅ **Human-in-the-loop** | Pharmacist, billing officer and insurance officer each sign off in a pop-up. Release only opens when all three approve the **same** revision. |
| 🧹 **Token pruning + de-identification** | Odoo ORM bloat is stripped (~80% fewer tokens) and the patient name never reaches the LLM. |
| 📱 **Live patient tracker** | The patient gets an SMS with a login-free progress page and co-pay, with no clinical data exposed. |
| 🧾 **Contacts + invoicing** | Patients are Odoo Contacts. Final invoice and a PDF audit packet are generated in one click. |

## 🏗️ Architecture

```
 Doctor / Nurse / Pharmacist ──► Odoo 20 addon (UI, workflow, approvals, DB)
                                      │  HTTP                    ▲
                                      ▼                          │ JSON-RPC (odoo_rpc.py)
                              FastAPI Sidecar ───────────────────┘
        ┌───────────────┬──────────────┼───────────────┬───────────────┐
  token_pruner     ai_llm_client   multimodal_ingestor   mcp_tools    sms_notifier
  (strip + anon)   (AI draft)      (voice + photo)       (3 parallel  (Twilio)
                                                          checks)
                              SQLite: revision + snapshot guard
```

```
sidecar_service/                 AI + safety checks (FastAPI, Pydantic v2, FastMCP, SQLite)
odoo_addons/clearpath_discharge/ Odoo 20 module: models, wizards (approval pop-ups), public tracker, tests
```

## 🚀 Run it

```bash
cp .env.example .env          # USE_MOCK_LLM=true works with no keys
docker compose up -d
```

- Odoo: <http://localhost:8069> (`admin` / `admin`) → **ClearPath Discharge** → **PT-402**
- Sidecar health: <http://localhost:8000/health>
- Optional: `.\demo-mode.ps1 live` for real AI, `.\demo-mode.ps1 mock` for instant offline mode.
- Full 3-minute walkthrough: [DEMO.md](DEMO.md)

## 🧪 Tests

```bash
cd sidecar_service && pytest              # sidecar pipeline
# Odoo addon tests run inside the Odoo container (tests/test_discharge_flow.py)
```

## 🛠️ Stack

Odoo 20 · Python · FastAPI · FastMCP · asyncio · Pydantic v2 · SQLite · NVIDIA Riva · OpenAI-compatible LLMs · Twilio · Docker

---
*Built for a hackathon. Demo data only. This is not a certified medical device.*
