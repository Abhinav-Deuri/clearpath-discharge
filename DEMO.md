# ClearPath - 3-minute demo script (with voice, photo scan and TPA insurance)

**Before you start**
1. Docker Desktop running; `docker compose up -d` from this folder.
2. Pick the AI mode: `.\demo-mode.ps1 live` (real AI, ~12 s per draft) or `.\demo-mode.ps1 mock` (instant, safe on bad Wi-Fi).
3. Open http://localhost:8069 (admin / admin) -> ClearPath Discharge -> Discharges -> **PT-402 Discharge**.
4. If it is not a clean Rev 1 draft: click **Reset Demo**.
5. Voice and photo scan are LIVE when `.env` has the `ASR_*` / `VISION_*` values (NVIDIA Riva Parakeet + Nemotron VL).
   The voice field has a **Record voice note** button (browser microphone, works on localhost) and an **Upload audio file** button; any audio format is converted to WAV in the browser. Any image works for the scan.
   With `.\demo-mode.ps1 mock`, or blank `ASR_*` / `VISION_*`, any file returns the PT-402 demo transcript and counts.

The admin login holds every role (Doctor, Pharmacist, Billing Officer, Nurse, Insurance Officer, Coordinator), so every button is on one screen.

| # | Click | What to say / point at | ~Time |
|---|-------|------------------------|-------|
| 1 | In **Multimodal Bedside Inputs**: upload the audio, click **Transcribe Voice & Draft** | The doctor dictates; ASR text becomes the handoff note, the AI drafts the summary, and the proposed Amoxicillin line is added. Blue banner: ~80% of tokens pruned, and "Arabith" never reaches the AI (PT-402 only). | 0:35 |
| 2 | Upload the image, click **Scan Nurse Supply Sheet** | The nurse's bedside tally photo sets the surgical quantities (gauze 4, stapler 1). Billing data is untouched. | 0:20 |
| 3 | **Lock Revision & Run Checks** (human gate: the doctor reviews and locks) | Rev 1 + hash. Three workers run in parallel: red = Amoxicillin vs Penicillin anaphylaxis; amber = +2 gauze, +1 stapler unbilled; yellow = TPA claim on hold. Three **smart buttons** appear top right (Pharmacy, Billing, Insurance) with colour-coded counts: "1 Allergy Flag", "2 Unbilled Items", "2 Holds". Click **Insurance** to see ICD-10 K35.80 / CPT 44970 and the auto-filled claim in a pop-up card. AI never approves anything. | 0:40 |
| 4 | Click the **Pharmacy** smart button | Pop-up card shows the conflict in red; **Approve & Resolve** is hidden while it is unresolved. Humans resolve; software only flags. | 0:10 |
| 5 | **Simulate Late Doctor Edit (Trigger Stale-Write Guard)** | Antibiotic becomes Azithromycin; Rev 1 -> **Rev 2**; Pharmacy, Billing AND Insurance all go STALE; old claim and snapshot are discarded; gate re-locks; chatter keeps the audit trail. | 0:30 |
| 6 | **Lock Revision & Run Checks** again | Rev 2: pharmacy clean; billing and insurance still on hold. | 0:15 |
| 7 | **Pharmacy** button -> **Approve & Resolve**; **Billing** button -> type billed 4 and 1 in the Billed column -> **Approve & Resolve**; **Insurance** button -> **Approve & Resolve** | Three departments, the SAME revision. Only now the green "Release gate open" banner appears. (human gate #2) | 0:40 |
| 8 | **Release Discharge Packet** | Final locked packet; edits are refused. Open **Audit Packet**, then **Download PDF Report** (includes ICD-10/CPT and four signature lines). | 0:25 |

**Bonus (Invoicing + Contacts):** after the Billing sign-off, type a **Unit Price** for each item in the Billing pop-up, then click **Generate Final Invoice** (header). The new **Invoice** smart button opens a draft customer invoice for Arabith's Contact record (Contacts app).

**What the icons mean:** each department button shows a spinning grey gear (checks pending), a pulsing red exclamation (needs attention), a green shield (approved) or an amber refresh (stale). Ribbons: red *Allergy Hold*, green *Cleared*, green *Released* (the status bar hides once released). Icons use Odoo 20's Material Symbols (`<i class="oi" data-icon="name"/>`), not Font Awesome.

**After the demo:** click **Reset Demo** to go back to a clean Rev 1.

**Rules worth saying out loud**
- Changing the note, the medications, or a supply item / used quantity after locking creates a new revision.
- Changing a *billed* quantity does not: billing fixes discrepancies without forcing a new revision.
- Release needs Pharmacy + Billing + Insurance approved on the same revision.

**If something breaks**
- AI slow or down: falls back to the mock draft after 20 s (`used_mock` in the response), or run `.\demo-mode.ps1 mock`.
- Voice/photo: files over 10 MB (audio) / 5 MB (image) are rejected with a clear message.
- Sidecar check: http://localhost:8000/health
- Odoo log: `docker compose logs odoo --tail 50`
- Python edits: `docker compose restart odoo`; view edits: refresh the page.

**URLs**: Odoo http://localhost:8069 | Sidecar docs http://localhost:8000/docs

**Live ASR / vision:** set `ASR_API_KEY` + `ASR_RIVA_FUNCTION_ID` (hosted Riva gRPC, WAV only; `ASR_RIVA_SERVER`,
`ASR_LANGUAGE_CODE` optional) or a Whisper-style `ASR_BASE_URL/ASR_MODEL_NAME`; and `VISION_BASE_URL/VISION_API_KEY/VISION_MODEL_NAME`
(vision chat/completions). Then `docker compose up -d --build sidecar_service`. Any live failure falls back to the demo result.
Audio and photos are real patient data: use a provider you are cleared to send it to.

**Role logins (optional):** Settings -> Users, give a user one ClearPath role. Each only sees their own buttons.

**Reception flow:** Contacts -> create a Contact (name, phone, email, address). On that Contact click **Admit as Patient**, add the patient code and bed. The Patient form shows the Contact details on top and "Clinical Admission Details" below; the Contact shows a **Patient** smart button back.
**Billing fixes keep the claim in sync:** fixing a Billed quantity (Billing pop-up or the supply list) re-runs the Insurance worker for the same revision and rebuilds the TPA claim. If the claim was already approved, that approval is withdrawn.

**Audit log:** the chatter (history of state changes, sign-offs, invoices) is hidden by default. Click **Toggle Audit Log** in the header to show or hide it, for example when showing the judges the trail.

## Phase 12: patient SMS + live tracker

1. Open the discharge and click **Live Patient Tracker** in the header. A phone-style page opens in a new tab; keep it next to the Odoo window. It refreshes itself every 4 seconds.
2. **Lock Revision & Run Checks**: step 1 turns green on the tracker, and the SMS text plus the tracker link are logged in the chatter (Toggle Audit Log) and on the **Patient Tracker & SMS** tab.
3. Approve Pharmacy, Billing and Insurance: each step turns green. After the third, the **Co-pay due** card appears and a second SMS is logged.
4. On the tracker click **Pay by card**: the banner badge in Odoo flips to **Co-Pay: Paid**.
5. **Release Discharge Packet**: the tracker shows the green **Cleared to Go Home** exit pass with a QR code.

SMS runs in mock mode by default (`USE_MOCK_SMS=true`): nothing is sent to a phone. To send real texts, set
`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` and `USE_MOCK_SMS=false` in `.env`, then
`docker compose up -d sidecar_service`. The link in the SMS uses `PUBLIC_ODOO_BASE_URL`; `localhost` only
opens on this machine, so use the machine's LAN address or a tunnel URL for a real phone.

## Phase 13/14: history + role dashboard

1. Open **ClearPath Discharge**: the kanban dashboard is the landing page. Cards are grouped Draft, Locked / Verifying, Flagged, Cleared, Released.
2. On PT-402 the banner shows the **Past history** pills (Type 2 Diabetes, Open Cholecystectomy). Click **Generate AI Draft**: the summary lists both histories and the plan tells the patient to monitor blood glucose closely.
3. As Administrator every card shows its flags and the **Review Meds / Review Billing / Review Claim** buttons. Click one: the approval pop-up opens straight from the card.
4. Log in as a user with only the Pharmacist group: only Pharmacy tags, the Pharmacy button, the **Pharmacy Queue** menu and the Review Meds button are visible. Billing and Insurance are hidden everywhere (form, alerts, kanban).
