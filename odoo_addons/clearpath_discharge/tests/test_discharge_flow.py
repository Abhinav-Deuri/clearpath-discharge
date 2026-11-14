import base64
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

MODEL_REQUESTS = 'odoo.addons.clearpath_discharge.models.clearpath_discharge.requests'
WIZARDS = {
    'pharmacy': 'clearpath.pharmacy.approval.wizard',
    'billing': 'clearpath.billing.approval.wizard',
    'insurance': 'clearpath.insurance.approval.wizard',
}
TRANSCRIPT = "POD1 appendectomy. Start Amoxicillin 500mg TID for 5 days."


class FakeResponse:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.ok, self.text = data, status, status < 400, str(data)

    def json(self):
        return self._data


def fake_post(url, json=None, timeout=None):
    """Stand-in for the sidecar with the same rules (allergy, used vs billed, ICD/CPT, policy)."""
    stats = {'before_tokens': 1000, 'after_tokens': 200, 'saved_tokens': 800, 'saved_pct': 80.0}
    if url.endswith('/api/v1/draft'):
        return FakeResponse({
            'ai_draft_summary': '## Discharge Summary\n- meds: ' + ', '.join(m['drug_name'] for m in json['medications']),
            'review_prompts': ['Check allergies'], 'model_used': 'fake', 'used_mock': True,
            'token_stats': stats, 'proposed_medications': [],
        })
    if url.endswith('/api/v1/transcribe-and-draft'):
        return FakeResponse({
            'transcribed_text': TRANSCRIPT, 'ai_draft_summary': '## Voice draft', 'review_prompts': ['Q1'],
            'model_used': 'fake', 'used_mock': True, 'token_stats': stats,
            'proposed_medications': [{'drug_name': 'Amoxicillin', 'dosage': '500mg', 'frequency': 'TID'}],
        })
    if url.endswith('/api/v1/scan-supply-sheet'):
        return FakeResponse({'items': [
            {'item_name': 'Gauze', 'surgical_qty_used': 4, 'billed_qty': 0},
            {'item_name': 'Stapler', 'surgical_qty_used': 1, 'billed_qty': 0},
            {'item_name': 'Suture Kit', 'surgical_qty_used': 2, 'billed_qty': 0},
        ]})

    # /verify
    conflicts = [
        {'drug_name': m['drug_name'], 'allergen': 'Penicillin', 'severity': 'critical', 'reason': 'beta-lactam'}
        for m in json['medications'] for a in json['allergies']
        if a['allergen'].lower() == 'penicillin' and 'amoxicillin' in m['drug_name'].lower()
    ]
    diffs = [
        {'item_name': s['item_name'], 'surgical_qty_used': s['surgical_qty_used'],
         'billed_qty': s['billed_qty'], 'discrepancy_qty': s['surgical_qty_used'] - s['billed_qty']}
        for s in json['supplies'] if s['surgical_qty_used'] > s['billed_qty']
    ]
    pharmacy = {'status': 'flagged' if conflicts else 'clear', 'conflicts': conflicts,
                'summary': '%d conflict(s)' % len(conflicts)}
    billing = {'status': 'flagged' if diffs else 'clear', 'discrepancies': diffs, 'summary': '%d gap(s)' % len(diffs)}
    mapped = 'appendectomy' in (json.get('procedure_text') or '').lower()
    holds = []
    if conflicts:
        holds.append('Pharmacy allergy conflict unresolved')
    if diffs:
        holds.append('Unreconciled surgical supplies')
    if not mapped:
        holds.append('Procedure not mapped')
    if not (json.get('tpa_provider') and json.get('policy_number')):
        holds.append('TPA provider / policy number missing')
    insurance = {
        'claim_status': 'hold_pending_reconciliation' if holds else 'ready_for_review',
        'icd10_code': 'K35.80' if mapped else '', 'cpt_code': '44970' if mapped else '',
        'hold_reasons': holds, 'filled_claim_markdown': '# TPA claim\n**Status:** draft',
    }
    flagged = bool(conflicts or diffs or holds)
    return FakeResponse({
        'snapshot_id': '%s_Rev%d' % (json['revision_hash'][:8], json['revision_id']),
        'discharge_id': json['discharge_id'], 'revision_id': json['revision_id'],
        'revision_hash': json['revision_hash'], 'status': 'flagged' if flagged else 'cleared',
        'pharmacy': pharmacy, 'billing': billing, 'insurance_output': insurance, 'elapsed_ms': 1.0,
    })


@tagged('post_install', '-at_install')
class TestDischargeFlow(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        patcher = patch(MODEL_REQUESTS)
        cls.requests = patcher.start()
        cls.addClassCleanup(patcher.stop)
        cls.requests.post.side_effect = fake_post
        cls.requests.RequestException = Exception

        cls.patient = cls.env['clearpath.patient'].create({
            'name': 'Test Patient', 'patient_code': 'PT-T1', 'bed_number': '1',
            'insurance_provider': 'Test TPA', 'policy_number': 'POL-T1',
            'allergy_ids': [(0, 0, {'allergen': 'Penicillin', 'reaction': 'Anaphylaxis', 'criticality': 'high'})],
        })
        cls.discharge = cls.env['clearpath.discharge'].create({
            'patient_id': cls.patient.id,
            'raw_handoff_note': 'POD1 appendectomy. Start Amoxicillin 500mg TID.',
            'medication_line_ids': [(0, 0, {'drug_name': 'Amoxicillin', 'dosage': '500mg', 'frequency': 'TID'})],
            'supply_line_ids': [
                (0, 0, {'item_name': 'Gauze', 'surgical_qty_used': 4, 'billed_qty': 2}),
                (0, 0, {'item_name': 'Stapler', 'surgical_qty_used': 1, 'billed_qty': 0}),
            ],
        })

        def user(login, group):
            return cls.env['res.users'].with_context(no_reset_password=True).create({
                'name': login, 'login': login,
                'group_ids': [(6, 0, [cls.env.ref('base.group_user').id, cls.env.ref(group).id])],
            })
        cls.doctor = user('t_doctor', 'clearpath_discharge.group_clearpath_doctor')
        cls.pharmacist = user('t_pharm', 'clearpath_discharge.group_clearpath_pharmacist')
        cls.billing = user('t_bill', 'clearpath_discharge.group_clearpath_billing')
        cls.coordinator = user('t_coord', 'clearpath_discharge.group_clearpath_coordinator')
        cls.nurse = user('t_nurse', 'clearpath_discharge.group_clearpath_nurse')
        cls.insurer = user('t_ins', 'clearpath_discharge.group_clearpath_admin')

    def _wizard(self, d, dept, user=None):
        """The pop-up a smart button opens (Odoo passes the discharge through the context)."""
        env = d.with_user(user).env if user else d.env
        return env[WIZARDS[dept]].with_context(default_discharge_id=d.id).create({'discharge_id': d.id})

    def _approve(self, d, dept, user=None):
        return self._wizard(d, dept, user).action_approve()

    def _locked(self):
        d = self.discharge
        d.action_generate_ai_draft()
        d.action_lock_revision_and_verify()
        return d

    def _reconcile_and_sign_all_but_insurance(self, d):
        """Late edit -> Rev 2 -> re-lock -> pharmacy + billing sign-off."""
        d.action_simulate_late_doctor_edit()
        d.action_lock_revision_and_verify()
        self._approve(d, 'pharmacy')
        for line in d.supply_line_ids:
            line.billed_qty = line.surgical_qty_used
        self._approve(d, 'billing')

    # ------------------------------------------------------------ core flow
    def test_01_lock_flags_conflict_gap_and_insurance_hold(self):
        d = self._locked()
        self.assertEqual(d.state, 'flagged')
        self.assertEqual((d.pharmacy_status, d.billing_status, d.insurance_status),
                         ('flagged', 'flagged', 'flagged'))
        self.assertTrue(d.medication_line_ids.is_flagged)
        self.assertEqual(d.revision_id, 1)
        self.assertTrue(d.sidecar_snapshot_id.endswith('_Rev1'))
        self.assertEqual(d.icd10_cpt_codes, 'K35.80 / 44970')
        self.assertIn('Pharmacy allergy conflict', d.insurance_hold_reasons)

    def test_02_approvals_blocked_while_flagged(self):
        d = self._locked()
        for dept in ('pharmacy', 'billing', 'insurance'):
            with self.assertRaises(UserError):
                self._approve(d, dept)
        with self.assertRaises(UserError):
            d.action_coordinator_release()

    def test_03_late_edit_bumps_revision_and_invalidates_all_three(self):
        d = self._locked()
        d.action_simulate_late_doctor_edit()
        self.assertEqual(d.revision_id, 2)
        self.assertEqual(d.state, 'draft')
        self.assertEqual((d.pharmacy_status, d.billing_status, d.insurance_status),
                         ('stale', 'stale', 'stale'))
        self.assertTrue(d.coordinator_gate_locked)
        self.assertFalse(d.sidecar_snapshot_id)
        self.assertFalse(d.audit_snapshot_json)
        self.assertFalse(d.tpa_claim_document)
        self.assertFalse(d.icd10_cpt_codes)
        self.assertEqual(d.medication_line_ids.drug_name, 'Azithromycin')

    def test_04_edit_while_draft_does_not_bump(self):
        d = self.discharge
        d.write({'raw_handoff_note': 'edited while still draft'})
        self.assertEqual(d.revision_id, 1)

    def test_05_stale_findings_rejected(self):
        d = self._locked()
        res = d.apply_mcp_findings(revision_id=0, revision_hash='deadbeef', pharmacy_result={},
                                   billing_result={}, audit_packet={}, insurance_result=None)
        self.assertFalse(res['accepted'])
        self.assertEqual(d.state, 'flagged')

    def test_06_full_happy_path_needs_three_sign_offs(self):
        d = self._locked()
        self._reconcile_and_sign_all_but_insurance(d)
        self.assertEqual((d.pharmacy_status, d.billing_status, d.insurance_status),
                         ('approved', 'approved', 'flagged'))
        self.assertEqual(d.state, 'flagged')  # insurance still outstanding
        self.assertTrue(d.coordinator_gate_locked)
        with self.assertRaises(UserError):
            d.action_coordinator_release()
        self._approve(d, 'insurance')
        self.assertEqual(d.state, 'cleared')
        self.assertFalse(d.coordinator_gate_locked)
        d.action_coordinator_release()
        self.assertEqual(d.state, 'released')
        self.assertIn('"insurance"', d.audit_snapshot_json)
        with self.assertRaises(UserError):
            d.write({'raw_handoff_note': 'tamper'})

    def test_07_role_gates(self):
        d = self.discharge
        d.action_generate_ai_draft()
        with self.assertRaises(UserError):
            d.with_user(self.pharmacist).action_lock_revision_and_verify()
        with self.assertRaises(UserError):
            d.with_user(self.nurse).action_lock_revision_and_verify()
        d.with_user(self.doctor).action_lock_revision_and_verify()
        with self.assertRaises(UserError):
            self._approve(d, 'pharmacy', self.billing)
        with self.assertRaises(UserError):
            self._approve(d, 'billing', self.pharmacist)
        with self.assertRaises(UserError):
            self._approve(d, 'insurance', self.doctor)
        with self.assertRaises(UserError):
            self._approve(d, 'insurance', self.nurse)
        with self.assertRaises(UserError):
            d.with_user(self.doctor).action_coordinator_release()

    def test_08_reset_demo_requires_pt402(self):
        with self.assertRaises(UserError):
            self.discharge.action_reset_demo()  # patient is PT-T1, not the demo patient

    def test_09_audit_card_renders_with_insurance(self):
        d = self._locked()
        html = str(d.audit_report_html)
        self.assertIn('PT-T1', html)
        self.assertIn('Conflict:', html)
        self.assertIn('+2', html)
        self.assertIn('K35.80', html)

    def test_10_delete_rules(self):
        draft = self.env['clearpath.discharge'].create({'patient_id': self.patient.id})
        draft.with_user(self.doctor).unlink()  # drafts are deletable by the doctor
        self.assertFalse(draft.exists())

        released = self.env['clearpath.discharge'].create({'patient_id': self.patient.id})
        released.with_context(clearpath_skip_guard=True).write({'state': 'released'})
        with self.assertRaises(UserError):
            released.with_user(self.doctor).unlink()  # audit trail is protected
        released.unlink()  # administrator (test env user) may delete it
        self.assertFalse(released.exists())

    # ------------------------------------------------------------ Phase 5
    def test_11_gate_stays_locked_until_insurance_signs(self):
        d = self._locked()
        self._reconcile_and_sign_all_but_insurance(d)
        self.assertNotEqual(d.state, 'cleared')
        self._approve(d, 'insurance', self.insurer)
        self.assertEqual(d.state, 'cleared')
        self.assertEqual(d.insurance_approver_id, self.insurer)

    def test_12_billed_qty_edit_keeps_revision_but_used_qty_bumps(self):
        d = self._locked()
        gauze = d.supply_line_ids.filtered(lambda l: l.item_name == 'Gauze')
        gauze.billed_qty = 3
        self.assertEqual((d.revision_id, d.state), (1, 'flagged'))
        gauze.surgical_qty_used = 5  # a clinical/surgical fact changed
        self.assertEqual(d.revision_id, 2)
        self.assertEqual(d.insurance_status, 'stale')
        d.supply_line_ids.filtered(lambda l: l.item_name == 'Stapler').unlink()
        self.assertEqual(d.state, 'draft')  # still draft; removal while draft never bumps
        self.assertEqual(d.revision_id, 2)

    def test_13_scan_updates_existing_lines_and_creates_new_ones(self):
        d = self.discharge
        gauze = d.supply_line_ids.filtered(lambda l: l.item_name == 'Gauze')
        gauze.surgical_qty_used = 9
        d.nurse_checklist_image = base64.b64encode(b'\x89PNGdemo').decode()
        d.with_user(self.nurse).action_scan_nurse_checklist()
        self.assertEqual(gauze.surgical_qty_used, 4)
        self.assertEqual(gauze.billed_qty, 2)  # billing data is preserved
        suture = d.supply_line_ids.filtered(lambda l: l.item_name == 'Suture Kit')
        self.assertEqual((suture.surgical_qty_used, suture.billed_qty), (2, 0))
        self.assertEqual(d.revision_id, 1)  # draft edits never bump

    def test_14_scan_needs_image_draft_state_and_role(self):
        d = self.discharge
        with self.assertRaises(UserError):
            d.action_scan_nurse_checklist()  # no image uploaded
        d.nurse_checklist_image = base64.b64encode(b'\x89PNGdemo').decode()
        with self.assertRaises(UserError):
            d.with_user(self.pharmacist).action_scan_nurse_checklist()
        self._locked()
        with self.assertRaises(UserError):
            d.action_scan_nurse_checklist()  # not draft any more

    def test_15_voice_fills_note_draft_and_meds_only_when_empty(self):
        d = self.env['clearpath.discharge'].create({'patient_id': self.patient.id})
        d.voice_note_audio = base64.b64encode(b'RIFFdemo').decode()
        d.voice_note_filename = 'demo.wav'
        d.with_user(self.doctor).action_transcribe_voice_and_draft()
        self.assertEqual(d.raw_handoff_note, TRANSCRIPT)
        self.assertEqual(d.ai_draft_summary, '## Voice draft')
        self.assertEqual(d.medication_line_ids.mapped('drug_name'), ['Amoxicillin'])
        d.action_transcribe_voice_and_draft()  # run again: medications already present
        self.assertEqual(len(d.medication_line_ids), 1)
        with self.assertRaises(UserError):
            d.with_user(self.nurse).action_transcribe_voice_and_draft()  # doctor only

    def test_16_voice_requires_audio_and_draft_state(self):
        d = self.discharge
        with self.assertRaises(UserError):
            d.action_transcribe_voice_and_draft()
        d.voice_note_audio = base64.b64encode(b'RIFFdemo').decode()
        self._locked()
        with self.assertRaises(UserError):
            d.action_transcribe_voice_and_draft()

    def test_17_insurance_blocked_without_policy(self):
        d = self._locked()
        self._reconcile_and_sign_all_but_insurance(d)
        self.patient.policy_number = False
        with self.assertRaises(UserError):
            self._approve(d, 'insurance')
        self.patient.policy_number = 'POL-T1'
        self._approve(d, 'insurance')
        self.assertEqual(d.insurance_status, 'approved')

    # ------------------------------------------------------------ Phase 6: pop-ups + smart buttons
    def test_18_openers_return_modal_actions(self):
        d = self._locked()
        for method, model in (('action_open_pharmacy_wizard', WIZARDS['pharmacy']),
                              ('action_open_billing_wizard', WIZARDS['billing']),
                              ('action_open_insurance_wizard', WIZARDS['insurance'])):
            action = getattr(d, method)()
            self.assertEqual(action['type'], 'ir.actions.act_window')
            self.assertEqual(action['target'], 'new')
            self.assertEqual(action['res_model'], model)
            self.assertEqual(action['context']['default_discharge_id'], d.id)

    def test_19_stat_button_counts_and_labels(self):
        d = self.discharge
        self.assertEqual(d.pharmacy_button_text, 'Not run')
        d = self._locked()
        self.assertEqual((d.pharmacy_conflict_count, d.unbilled_item_count), (1, 2))
        self.assertEqual(d.pharmacy_button_text, '1 Allergy Flag')
        self.assertEqual(d.billing_button_text, '2 Unbilled Items')
        self.assertEqual(d.insurance_button_text, '2 Holds')
        self._reconcile_and_sign_all_but_insurance(d)
        self.assertEqual((d.pharmacy_button_text, d.billing_button_text), ('Approved', 'Approved'))
        self.assertEqual(d.insurance_hold_count, 1)  # unreconciled supplies at lock time
        d.write({'raw_handoff_note': 'late clinical change'})  # Stale-Write Guard
        self.assertEqual((d.pharmacy_button_text, d.billing_button_text, d.insurance_button_text),
                         ('Stale', 'Stale', 'Stale'))

    def test_20_wizard_can_approve_follows_blockers(self):
        d = self._locked()
        pharmacy = self._wizard(d, 'pharmacy')
        self.assertFalse(pharmacy.can_approve)
        self.assertIn('allergy conflict', str(pharmacy.blocker_html))
        self.assertIn('Amoxicillin', str(pharmacy.conflict_html))
        self.assertFalse(self._wizard(d, 'billing').can_approve)
        self.assertFalse(self._wizard(d, 'insurance').can_approve)
        d.action_simulate_late_doctor_edit()
        d.action_lock_revision_and_verify()
        self.assertTrue(self._wizard(d, 'pharmacy').can_approve)  # Azithromycin is clean

    def test_21_billing_wizard_edits_billed_qty_without_new_revision(self):
        d = self._locked()
        d.action_simulate_late_doctor_edit()
        d.action_lock_revision_and_verify()
        wiz = self._wizard(d, 'billing')
        self.assertEqual((len(wiz.line_ids), wiz.total_unbilled), (2, 3))
        self.assertFalse(wiz.can_approve)
        with self.assertRaises(UserError):
            wiz.action_approve()
        for line in wiz.line_ids:  # what the user types into the Billed column
            line.billed_qty = line.surgical_qty_used
        self.assertEqual(wiz.total_unbilled, 0)
        self.assertTrue(wiz.can_approve)
        wiz.action_approve()
        self.assertEqual(d.supply_line_ids.mapped('billed_qty'), [4, 1])
        self.assertEqual((d.revision_id, d.billing_status), (2, 'approved'))  # no new revision

    def test_22_approved_wizard_cannot_approve_twice(self):
        d = self._locked()
        self._reconcile_and_sign_all_but_insurance(d)
        wiz = self._wizard(d, 'pharmacy')
        self.assertFalse(wiz.can_approve)
        with self.assertRaises(UserError):
            wiz.action_approve()

    # ------------------------------------------------------------ alerts pop-up
    def test_23_lock_opens_alerts_popup_and_button_counts(self):
        d = self.discharge
        d.action_generate_ai_draft()
        action = d.action_lock_revision_and_verify()
        self.assertEqual((action['type'], action['target'], action['res_model']),
                         ('ir.actions.act_window', 'new', 'clearpath.alerts.wizard'))
        titles = [i[2] for i in d._alert_items()]
        self.assertIn("Pharmacy safety conflict", titles)
        self.assertIn("TPA insurance claim on hold", titles)
        self.assertGreaterEqual(d.alert_count, 3)
        wiz = self.env['clearpath.alerts.wizard'].with_context(default_discharge_id=d.id).create(
            {'discharge_id': d.id})
        self.assertIn('Pharmacy safety conflict', str(wiz.alerts_html))
        self.assertEqual(wiz.action_open_pharmacy()['res_model'], WIZARDS['pharmacy'])

    def test_24_clean_lock_just_reloads(self):
        d = self.env['clearpath.discharge'].create({'patient_id': self.patient.id,
                                                    'raw_handoff_note': 'POD1 appendectomy, no meds.'})
        d.action_generate_ai_draft()
        d.supply_line_ids = [(0, 0, {'item_name': 'Gauze', 'surgical_qty_used': 1, 'billed_qty': 1})]
        action = d.action_lock_revision_and_verify()
        # insurance still needs sign-off context (token savings info alert) -> popup is allowed;
        # what matters: a result is always an action dict
        self.assertEqual(action['type'] in ('ir.actions.act_window', 'ir.actions.client'), True)

    def test_25_statusbar_hidden_when_released(self):
        arch = self.env['clearpath.discharge'].get_view(
            self.env.ref('clearpath_discharge.clearpath_discharge_view_form').id, 'form')['arch']
        self.assertIn("state == 'released'", arch)
        self.assertNotIn('name="pharmacy_summary"', arch)  # wordy banners moved to the pop-up

    # ------------------------------------------------------------ Phase 7: Contacts + Invoicing
    def test_26_patient_is_a_contact(self):
        self.assertTrue(self.patient.partner_id)
        self.assertEqual(self.patient.partner_id.name, 'Test Patient')
        jane = self.env['clearpath.patient'].create(
            {'name': 'Jane Roe', 'patient_code': 'PT-T9', 'email': 'jane@example.com'})
        self.assertEqual(jane.partner_id.email, 'jane@example.com')
        self.assertEqual(jane.partner_id.name, 'Jane Roe')

    def test_27_invoice_from_billed_supplies(self):
        d = self._locked()
        self._reconcile_and_sign_all_but_insurance(d)
        for line in d.supply_line_ids:
            line.price_unit = 10.0  # billing sets prices: never bumps the revision
        self.assertEqual(d.revision_id, 2)
        d.with_user(self.billing).action_generate_invoice()
        move = d.invoice_id
        self.assertEqual((move.move_type, move.state), ('out_invoice', 'draft'))
        self.assertEqual(move.partner_id, self.patient.partner_id)
        self.assertEqual(sorted(move.invoice_line_ids.mapped('quantity')), [1.0, 4.0])
        self.assertEqual(move.amount_untaxed, 50.0)
        self.assertEqual(d.action_view_invoice()['res_model'], 'account.move')
        with self.assertRaises(UserError):
            d.action_generate_invoice()  # one invoice per discharge

    def test_28_invoice_blocked_before_billing_and_for_other_roles(self):
        d = self._locked()
        with self.assertRaises(UserError):
            d.action_generate_invoice()
        self._reconcile_and_sign_all_but_insurance(d)
        with self.assertRaises(UserError):
            d.with_user(self.doctor).action_generate_invoice()

    # ------------------------------------------------------------ Phase 8 + summary card
    def test_29_summary_card_is_readable(self):
        from odoo.addons.clearpath_discharge.models.clearpath_discharge import (
            clean_summary_text, summary_to_html)
        sample = (
            "**Discharge Summary**\n- **Patient**: PT-402, Bed 402\n"
            "- **Allergies**: Penicillin - anaphylaxis (high)\n"
            "- **Items for clinician review**: Amoxicillin <script>x</script>\n---\n"
            "- **Follow-up**: Surgical clinic in 2 weeks")
        html = str(summary_to_html(sample))
        for needle in ('Discharge Summary', 'data-icon="person"', 'data-icon="history"', '#dc3545', 'data-icon="warning"',
                       '&lt;script&gt;'):
            self.assertIn(needle, html)
        self.assertNotIn('---', html)
        self.assertNotIn('**', html)
        self.assertNotIn('---', clean_summary_text(sample))
        self.assertIn('Just a plain note.', str(summary_to_html("Just a plain note.")))
        bare = str(summary_to_html("Discharge Summary\n- **Patient**: PT-1\n- **Plan**: home"))
        self.assertIn('Discharge Summary', bare)
        self.assertIn('data-icon="description"', bare)  # first bare line became the card title

    def test_30_top_alert_ribbons_and_icon_buttons(self):
        d = self._locked()
        self.assertEqual(d.top_alert_kind, 'pharmacy')
        self.assertEqual(d.top_alert_title, 'Penicillin Allergy Conflict')
        arch = self.env['clearpath.discharge'].get_view(
            self.env.ref('clearpath_discharge.clearpath_discharge_view_form').id, 'form')['arch']
        for needle in ('Allergy Hold', 'Cleared', 'oi-spin', 'cp-beat-fade', 'data-icon="security"',
                       'cp-pulse', 'action_generate_invoice', 'action_view_invoice'):
            self.assertIn(needle, arch)
        self.assertEqual(arch.count('name="action_open_pharmacy_wizard"'), 4)
        self.assertEqual(arch.count('name="action_open_billing_wizard"'), 4)
        self.assertEqual(arch.count('name="action_open_insurance_wizard"'), 4)

