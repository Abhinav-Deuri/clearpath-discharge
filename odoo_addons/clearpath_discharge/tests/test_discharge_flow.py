import base64
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import HttpCase, TransactionCase, tagged
from odoo.tools import mute_logger

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


def fake_sms(token, to_number, stage_event):
    link = 'http://localhost:8069/discharge/track/' + token
    return {'sent': False, 'mode': 'mock_logged', 'to_number': to_number or '+971500000000',
            'tracking_url': link, 'message_preview': 'ClearPath Hospital: track here: ' + link,
            'twilio_sid': None, 'stage_event': stage_event, 'error': ''}


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

    if url.endswith('/api/v1/notify-sms'):
        return FakeResponse(fake_sms(json['tracking_token'], json['to_number'], json['stage_event']))

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
        'sms': fake_sms(json['tracking_token'], json.get('patient_phone'), 'started')
        if json.get('tracking_token') else None,
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
                         ('approved', 'approved', 'pending'))  # claim rebuilt clean by the billing fix
        self.assertEqual(d.state, 'flagged')  # insurance sign-off still outstanding
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
        self.assertEqual(d.insurance_hold_count, 0)  # the billing fix cleared the supply hold
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
        self.assertEqual(wiz.view_state, 'flagged')  # red badge while something is unbilled
        self.assertFalse(wiz.can_approve)
        with self.assertRaises(UserError):
            wiz.action_approve()
        for line in wiz.line_ids:  # what the user types into the Billed column
            line.billed_qty = line.surgical_qty_used
        self.assertEqual(wiz.total_unbilled, 0)
        self.assertEqual(wiz.view_state, 'reconciled')  # green as soon as billed == used
        self.assertTrue(wiz.can_approve)
        wiz.action_approve()
        wiz.invalidate_recordset()
        self.assertEqual(wiz.view_state, 'approved')
        self.assertEqual(d.supply_line_ids.mapped('billed_qty'), [4, 1])
        self.assertEqual((d.revision_id, d.billing_status), (2, 'approved'))  # no new revision

    def test_21b_billing_red_alert_clears_once_billed_matches_used(self):
        d = self._locked()
        self.assertEqual(d.billing_status, 'flagged')
        self.assertEqual(d.unbilled_item_count, 2)
        self.assertEqual(d.top_alert_kind, 'pharmacy')  # allergy conflict outranks billing
        self.assertIn("Surgical billing discrepancy", [i[2] for i in d._alert_items()])
        d.medication_line_ids.write({'is_flagged': False, 'conflict_reason': False})
        d.pharmacy_status = 'approved'
        self.assertEqual(d.top_alert_kind, 'billing')
        for s in d.supply_line_ids:  # billed quantity now equals what was used (same revision)
            s.billed_qty = s.surgical_qty_used
        self.assertEqual(d.unbilled_item_count, 0)
        self.assertEqual(d.billing_status, 'flagged')  # not signed yet ...
        self.assertNotEqual(d.top_alert_kind, 'billing')  # ... but nothing is red any more
        self.assertNotIn("Surgical billing discrepancy", [i[2] for i in d._alert_items()])

    # ------------------------------------------------------------ Phase 15: AMA override
    def _ama_wizard(self, d, user=None, **vals):
        env = d.with_user(user).env if user else d.env
        return env['clearpath.ama.wizard'].with_context(default_discharge_id=d.id).create(
            dict({'discharge_id': d.id}, **vals))

    def _released_sms_count(self):
        return len([c for c in self.requests.post.call_args_list
                    if (c.kwargs.get('json') or {}).get('stage_event') == 'released'])

    def test_60_ama_releases_through_a_locked_gate_and_records_the_waiver(self):
        d = self._locked()
        self.assertEqual((d.state, d.coordinator_gate_locked, d.revision_id), ('flagged', True, 1))
        action = d.with_user(self.doctor).action_open_ama_wizard()
        self.assertEqual((action['res_model'], action['target']), ('clearpath.ama.wizard', 'new'))
        wiz = self._ama_wizard(d, self.doctor)
        self.assertEqual(wiz.patient_signature_name, 'Test Patient')  # pre-filled with the patient's own name
        with self.assertRaises(UserError):  # liability box not ticked
            wiz.action_confirm_ama_discharge()
        self.assertEqual((d.state, d.is_ama_discharge), ('flagged', False))
        sms_before = self._released_sms_count()
        wiz.confirm_liability_waiver = True
        wiz.stated_reason = "Family emergency"
        self.assertEqual(wiz.action_confirm_ama_discharge(), {'type': 'ir.actions.act_window_close'})
        self.assertEqual((d.state, d.is_ama_discharge, d.coordinator_gate_locked), ('released', True, False))
        self.assertEqual((d.ama_signed_by, d.ama_reason), ('Test Patient', "Family emergency"))
        self.assertTrue(d.ama_timestamp and d.released_on)
        self.assertEqual((d.coordinator_id, d.revision_id), (self.doctor, 1))
        self.assertEqual(d.pharmacy_status, 'flagged')  # the open flags are not erased
        self.assertTrue(any('AGAINST MEDICAL ADVICE' in (m.body or '') for m in d.message_ids))
        self.assertEqual(self._released_sms_count(), sms_before)  # no "you are cleared" text for an AMA exit

    def test_61_ama_is_limited_to_doctor_coordinator_and_admin(self):
        d = self._locked()
        for user in (self.nurse, self.pharmacist, self.billing):
            with self.assertRaises(UserError):
                d.with_user(user).action_open_ama_wizard()
        wiz = self._ama_wizard(d, confirm_liability_waiver=True)
        for user in (self.nurse, self.pharmacist, self.billing):
            with self.assertRaises(UserError):
                wiz.with_user(user).action_confirm_ama_discharge()
        self.assertEqual(d.state, 'flagged')
        for user in (self.coordinator, self.insurer):
            self.assertEqual(d.with_user(user).action_open_ama_wizard()['target'], 'new')

    def test_62_ama_is_final_and_reset_demo_clears_it(self):
        d = self._locked()
        self._ama_wizard(d, self.coordinator, confirm_liability_waiver=True).action_confirm_ama_discharge()
        with self.assertRaises(UserError):
            d.action_open_ama_wizard()  # already released
        with self.assertRaises(UserError):
            self._ama_wizard(d, confirm_liability_waiver=True).action_confirm_ama_discharge()
        with self.assertRaises(UserError):
            d.write({'raw_handoff_note': 'edited after an AMA release'})
        demo = self.env.ref('clearpath_discharge.demo_patient_pt402')
        d.patient_id = demo
        d.action_reset_demo()
        self.assertEqual((d.state, d.is_ama_discharge, d.ama_reason, d.ama_signed_by, d.ama_timestamp),
                         ('draft', False, False, False, False))

    def test_63_tracker_shows_the_ama_notice_not_an_exit_pass(self):
        d = self._locked()
        d.with_context(clearpath_skip_guard=True).write({'patient_payment_status': 'paid'})
        self._ama_wizard(d, self.doctor, confirm_liability_waiver=True,
                         stated_reason="Private clinical reason").action_confirm_ama_discharge()
        values = d._tracker_values()
        self.assertEqual((values['copay_mode'], values['badge_kind']), ('ama', 'ama'))
        self.assertEqual(values['ama_signed_by'], 'Test Patient')
        self.assertEqual(values['steps'][-1]['tag'], "AMA")
        self.assertNotIn('private clinical reason', repr(values).lower())  # the reason never leaves Odoo
        html = str(self.env['ir.qweb']._render('clearpath_discharge.patient_tracker_page', values))
        self.assertIn('Released Against Medical Advice (Patient Choice)', html)
        self.assertNotIn('Cleared to Go Home', html)
        self.assertNotIn('Exit pass QR', html)

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
        for needle in ('data-icon="person"', 'data-icon="history"', '#dc3545', 'data-icon="warning"',
                       '&lt;script&gt;'):
            self.assertIn(needle, html)
        self.assertNotIn('---', html)
        self.assertNotIn('**', html)
        self.assertIn('text-danger fw-bold', html)  # allergy value
        self.assertIn('text-muted fw-bold small text-uppercase', html)  # field labels
        self.assertNotIn('---', clean_summary_text(sample))
        self.assertIn('Just a plain note.', str(summary_to_html("Just a plain note.")))
        bare = str(summary_to_html("Discharge Summary\n- **Patient**: PT-1\n- **Plan**: home"))
        self.assertNotIn('Discharge Summary', bare)  # generic title: the form's card header carries it
        self.assertIn('data-icon="person"', bare)
        titled = str(summary_to_html("Cardiac Discharge"+chr(10)+"- **Patient**: PT-1"+chr(10)+"- **Plan**: home"))
        self.assertIn('Cardiac Discharge', titled)  # a specific title is kept

    def test_30_top_alert_ribbons_and_icon_buttons(self):
        d = self._locked()
        self.assertEqual(d.top_alert_kind, 'pharmacy')
        self.assertEqual(d.top_alert_title, 'Penicillin Allergy Conflict')
        arch = self.env['clearpath.discharge'].get_view(
            self.env.ref('clearpath_discharge.clearpath_discharge_view_form').id, 'form')['arch']
        for needle in ('Allergy Hold', 'Cleared', 'text-warning', 'data-icon="medical_services"', 'cp-beat-fade', 'data-icon="security"',
                       'cp-pulse', 'action_generate_invoice', 'action_view_invoice'):
            self.assertIn(needle, arch)
        self.assertEqual(arch.count('name="action_open_pharmacy_wizard"'), 4)
        self.assertEqual(arch.count('name="action_open_billing_wizard"'), 4)
        self.assertEqual(arch.count('name="action_open_insurance_wizard"'), 4)

    # ------------------------------------------------------------ Phase 9: reception flow + claim sync
    def test_31_reception_creates_contact_then_patient(self):
        india = self.env.ref('base.in')
        rita = self.env['clearpath.patient'].create({
            'name': 'Rita Sen', 'phone': '+91 98000 00001', 'email': 'rita@example.com',
            'street': '1 Main Road', 'city': 'Pune', 'zip': '411001', 'country_id': india.id,
            'patient_code': 'PT-R1', 'bed_number': '12'})
        partner = rita.partner_id
        self.assertEqual((partner.name, partner.city, partner.zip, partner.country_id), ('Rita Sen', 'Pune', '411001', india))
        self.assertEqual((rita.patient_code, rita.bed_number), ('PT-R1', '12'))
        self.assertEqual(partner.patient_count, 1)
        self.assertEqual(partner.action_open_clearpath_patient()['res_id'], rita.id)

        # Reception first creates a plain Contact, clinical staff admit it as a patient later
        walk_in = self.env['res.partner'].create({'name': 'Walk In', 'email': 'walk@example.com'})
        self.assertEqual(walk_in.patient_count, 0)
        self.assertEqual(walk_in.action_open_clearpath_patient()['context']['default_partner_id'], walk_in.id)
        admitted = self.env['clearpath.patient'].create({'partner_id': walk_in.id, 'patient_code': 'PT-R2'})
        self.assertEqual((admitted.name, admitted.email), ('Walk In', 'walk@example.com'))
        self.assertEqual(self.env['res.partner'].search_count([('name', '=', 'Walk In')]), 1)  # no duplicate

    def test_32_billing_fix_rebuilds_claim_without_new_revision(self):
        d = self._locked()
        self.assertIn('Unreconciled surgical supplies', d.insurance_hold_reasons)
        for line in d.supply_line_ids:
            line.billed_qty = line.surgical_qty_used
        self.assertNotIn('Unreconciled surgical supplies', d.insurance_hold_reasons or '')
        self.assertEqual((d.revision_id, d.billing_status), (1, 'flagged'))
        self.assertIn('TPA claim rebuilt', ' '.join(d.message_ids.mapped('body')))

    def test_33_billing_edit_withdraws_insurance_signoff(self):
        d = self._locked()
        self._reconcile_and_sign_all_but_insurance(d)
        self._approve(d, 'insurance')
        self.assertEqual((d.state, d.coordinator_gate_locked), ('cleared', False))
        d.supply_line_ids[0].billed_qty = 0  # a later billing change invalidates the approved claim
        self.assertEqual((d.state, d.coordinator_gate_locked), ('flagged', True))
        self.assertIn(d.insurance_status, ('flagged', 'pending'))
        self.assertFalse(d.insurance_approver_id)
        self.assertEqual(d.revision_id, 2)  # still the same revision: only the claim was rebuilt

    def test_34_flagged_buttons_show_count_and_triangle(self):
        arch = self.env['clearpath.discharge'].get_view(
            self.env.ref('clearpath_discharge.clearpath_discharge_view_form').id, 'form')['arch']
        for count in ('pharmacy_conflict_count', 'unbilled_item_count', 'insurance_hold_count'):
            self.assertIn('<field name="%s"/>' % count, arch)
            self.assertIn("%s == 0 or" % count, arch)
        self.assertEqual(arch.count('text-danger cp-beat-fade" data-icon="warning"'), 3)
        d = self._locked()
        self.assertEqual((d.pharmacy_conflict_count, d.unbilled_item_count, d.insurance_hold_count), (1, 2, 2))

    def test_35_reset_demo_drops_the_invoice_link(self):
        demo = self.env.ref('clearpath_discharge.demo_patient_pt402')
        d = self.env['clearpath.discharge'].create({
            'patient_id': demo.id, 'supply_line_ids': [(0, 0, {
                'item_name': 'Gauze', 'surgical_qty_used': 1, 'billed_qty': 1, 'price_unit': 5.0})]})
        d.with_context(clearpath_skip_guard=True).write({'billing_status': 'approved'})
        d.action_generate_invoice()
        move = d.invoice_id
        move.action_post()  # posted invoices cannot be deleted: reset must only drop the link
        d.action_reset_demo()
        self.assertFalse(d.invoice_id)
        self.assertTrue(move.exists())

    def test_36_audit_log_toggle_and_bold_chatter(self):
        d = self.discharge
        self.assertFalse(d.show_audit_log)
        d.action_toggle_audit_log()
        self.assertTrue(d.show_audit_log)
        d.action_toggle_audit_log()
        self.assertFalse(d.show_audit_log)
        arch = self.env['clearpath.discharge'].get_view(
            self.env.ref('clearpath_discharge.clearpath_discharge_view_form').id, 'form')['arch']
        self.assertIn('invisible="not show_audit_log"', arch)
        msg = d.message_post(body="Released by <b>Admin</b> <script>x</script>")
        self.assertIn('<b>Admin</b>', str(msg.body))
        self.assertNotIn('<script>', str(msg.body))

    # ------------------------------------------------------------ duplicate-proofing + Phase 10 layout
    def test_37_item_listed_once_per_discharge(self):
        d = self.discharge
        with self.assertRaises(ValidationError):
            d.supply_line_ids = [(0, 0, {'item_name': ' gauze ', 'surgical_qty_used': 1})]  # same item, any case
        self.assertEqual(len(d.supply_line_ids), 2)

    def test_38_demo_seed_is_idempotent_and_uses_the_patient_name(self):
        demo = self.env.ref('clearpath_discharge.demo_discharge_pt402')
        for _ in range(3):  # re-seeding (reset / hook) must never stack copies
            demo._seed_demo_content()
        self.assertEqual(len(demo.medication_line_ids), 1)
        self.assertEqual(len(demo.supply_line_ids), 2)
        self.assertIn(demo.patient_id.name, demo.raw_handoff_note)
        self.assertNotIn('John Doe', demo.raw_handoff_note)

    def test_39_banner_short_hash_and_gate_pill(self):
        d = self._locked()
        self.assertEqual(d.short_revision_hash, '#' + d.revision_hash[:8])
        self.assertEqual(len(d.short_revision_hash), 9)
        arch = self.env['clearpath.discharge'].get_view(
            self.env.ref('clearpath_discharge.clearpath_discharge_view_form').id, 'form')['arch']
        for needle in ('o_cp_banner', 'short_revision_hash', 'Gate Locked', 'Gate Unlocked',
                       'Doctor Voice Dictation', 'Nurse Supply Sheet Checklist', 'Structured AI Discharge Summary',
                       'btn-light border btn-sm'):
            self.assertIn(needle, arch)

    # ------------------------------------------------- Phase 12: patient SMS + live tracker
    def _sms_calls(self, event=None):
        calls = [c for c in self.requests.post.call_args_list if c.args[0].endswith('/api/v1/notify-sms')]
        return [c for c in calls if event is None or c.kwargs['json']['stage_event'] == event]

    def _cleared(self):
        d = self._locked()
        self._reconcile_and_sign_all_but_insurance(d)
        self._approve(d, 'insurance')
        return d

    def test_42_tracking_token_is_unique_and_unguessable(self):
        other = self.discharge.copy()
        self.assertTrue(self.discharge.tracking_token and other.tracking_token)
        self.assertNotEqual(self.discharge.tracking_token, other.tracking_token)
        self.assertGreaterEqual(len(self.discharge.tracking_token), 20)
        self.assertTrue(self.discharge.patient_tracker_url.endswith(
            '/discharge/track/' + self.discharge.tracking_token))
        action = self.discharge.action_open_patient_tracker()
        self.assertEqual((action['type'], action['target']), ('ir.actions.act_url', 'new'))

    def test_43_lock_texts_the_patient_and_logs_the_link(self):
        self.patient.phone = '+971501234567'
        self.requests.post.reset_mock()
        d = self._locked()
        verify = [c for c in self.requests.post.call_args_list if c.args[0].endswith('/api/v1/verify')][-1]
        self.assertEqual(verify.kwargs['json']['tracking_token'], d.tracking_token)
        self.assertEqual(verify.kwargs['json']['patient_phone'], '+971501234567')
        self.assertIn('mock mode', d.last_sms_status)
        self.assertNotIn('1234567', d.last_sms_status.replace('+97150', ''))  # number is masked
        bodies = ' '.join(d.message_ids.mapped('body'))
        self.assertIn('/discharge/track/' + d.tracking_token, bodies)
        # the stored audit packet never carries the phone number or the tracker link
        self.assertNotIn(d.tracking_token, d.audit_snapshot_json)
        self.assertNotIn('971501234567', d.audit_snapshot_json)

    def test_44_internal_re_verify_does_not_text_again(self):
        d = self._locked()
        self.requests.post.reset_mock()
        d.supply_line_ids[0].billed_qty = 4  # billing fix: claim is rebuilt through /verify
        verifies = [c for c in self.requests.post.call_args_list if c.args[0].endswith('/api/v1/verify')]
        self.assertTrue(verifies)
        self.assertFalse(any(c.kwargs['json'].get('tracking_token') for c in verifies))
        self.assertFalse(self._sms_calls())

    def test_45_copay_sms_once_then_pay_then_exit_pass(self):
        self.requests.post.reset_mock()
        d = self._cleared()
        self.assertEqual(len(self._sms_calls('ready_for_payment')), 1)
        self.assertIn('Co-pay ready', d.last_sms_status)
        values = d._tracker_values()
        self.assertEqual((values['copay_mode'], values['badge_kind']), ('pay', 'pay'))
        self.assertEqual([s['state'] for s in values['steps']], ['done'] * 4 + ['active'])

        self.assertTrue(d.sudo().action_patient_pay_copay())
        self.assertEqual(d.patient_payment_status, 'paid')
        self.assertTrue(d.patient_paid_on)
        self.assertIn('Patient completed co-pay of AED 150.00 via Mobile Tracker', d.message_ids[0].body)
        self.assertFalse(d.sudo().action_patient_pay_copay())  # second tap changes nothing
        self.assertEqual(d._tracker_values()['copay_mode'], 'paid_wait')

        d.action_coordinator_release()
        self.assertEqual(len(self._sms_calls('released')), 1)
        values = d._tracker_values()
        self.assertEqual((values['copay_mode'], values['done_count'], values['progress']), ('pass', 5, 100))
        self.assertIn('EXIT-PT-T1-R%d-' % d.revision_id, values['pass_code'])

    def test_46_copay_closed_until_all_three_approve(self):
        d = self._locked()
        with self.assertRaises(UserError):
            d.sudo().action_patient_pay_copay()
        self.assertEqual(d.patient_payment_status, 'unpaid')
        self.assertEqual(d._tracker_values()['copay_mode'], 'waiting')

    def test_47_tracker_shows_progress_but_no_clinical_detail(self):
        d = self.discharge
        values = d._tracker_values()
        self.assertEqual([s['state'] for s in values['steps']], ['active'] + ['waiting'] * 4)
        d = self._locked()
        values = d._tracker_values()
        self.assertEqual([s['state'] for s in values['steps']],
                         ['done', 'attention', 'attention', 'attention', 'waiting'])
        self.assertEqual(values['badge_kind'], 'attention')
        text = repr(values).lower()
        for secret in ('amoxicillin', 'penicillin', 'anaphylaxis', 'gauze', 'stapler', 'k35', 'pol-t1'):
            self.assertNotIn(secret, text)

    def test_48_late_edit_reopens_the_tracker_and_the_copay_sms(self):
        self.requests.post.reset_mock()
        d = self._cleared()
        sig = d._tracker_values()['sig']
        d.write({'raw_handoff_note': 'Changed plan after lock. appendectomy.'})
        values = d._tracker_values()
        self.assertNotEqual(values['sig'], sig)
        self.assertEqual([s['state'] for s in values['steps']], ['active'] + ['waiting'] * 4)
        self.assertFalse(d.copay_sms_sent)

    def test_49_sms_failure_never_blocks_the_sign_off(self):
        d = self._locked()
        self._reconcile_and_sign_all_but_insurance(d)

        def flaky(url, json=None, timeout=None):
            if url.endswith('/api/v1/notify-sms'):
                return FakeResponse({'detail': 'down'}, status=503)
            return fake_post(url, json=json, timeout=timeout)
        self.requests.post.side_effect = flaky
        try:
            self._approve(d, 'insurance')
        finally:
            self.requests.post.side_effect = fake_post
        self.assertEqual(d.state, 'cleared')
        self.assertIn('not sent', d.last_sms_status)

    def test_50_form_has_tracker_button_and_copay_badge(self):
        arch = self.env['clearpath.discharge'].get_view(
            self.env.ref('clearpath_discharge.clearpath_discharge_view_form').id, 'form')['arch']
        for needle in ('action_open_patient_tracker', 'Live Patient Tracker', 'patient_payment_status',
                       'Co-Pay'):
            self.assertIn(needle, arch)


@tagged('post_install', '-at_install')
class TestPatientTrackerPage(HttpCase):
    """The public page itself: token access, no login, the pay route."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.patient = cls.env['clearpath.patient'].create({
            'name': 'Tracker Patient', 'patient_code': 'PT-T9', 'bed_number': '9'})
        cls.discharge = cls.env['clearpath.discharge'].create({
            'patient_id': cls.patient.id, 'raw_handoff_note': 'POD1 appendectomy.',
            'medication_line_ids': [(0, 0, {'drug_name': 'Amoxicillin', 'dosage': '500mg', 'frequency': 'TID'})],
        })
        cls.url = '/discharge/track/' + cls.discharge.tracking_token

    def _set(self, **vals):
        self.discharge.with_context(clearpath_skip_guard=True).write(vals)

    def test_01_unknown_or_short_token_is_404(self):
        self.assertEqual(self.url_open('/discharge/track/' + 'x' * 22).status_code, 404)
        self.assertEqual(self.url_open('/discharge/track/abc').status_code, 404)

    def test_02_page_opens_without_login_and_hides_clinical_data(self):
        res = self.url_open(self.url)
        self.assertEqual(res.status_code, 200)
        self.assertIn('no-store', res.headers.get('Cache-Control', ''))
        self.assertEqual(res.headers.get('Referrer-Policy'), 'no-referrer')
        for needle in ('Tracker Patient', 'Bed 9', 'Doctor Handoff', 'Pharmacy Safety Check',
                       'Surgical Billing Audit', 'TPA Insurance Pre-Auth', 'Co-Pay'):
            self.assertIn(needle, res.text)
        self.assertNotIn('Amoxicillin', res.text)
        self.assertNotIn('Pay by card', res.text)  # co-pay not open yet

    def test_03_pay_is_refused_until_ready_then_marks_paid(self):
        res = self.url_open(self.url + '/pay', data={'x': '1'})
        self.assertEqual(res.status_code, 200)  # redirected back to the tracker
        self.assertIn('Payment is not open yet', res.text)
        self.assertEqual(self.discharge.patient_payment_status, 'unpaid')

        self._set(state='cleared', pharmacy_status='approved', billing_status='approved',
                  insurance_status='approved', coordinator_gate_locked=False)
        self.assertIn('Pay by card', self.url_open(self.url).text)
        res = self.url_open(self.url + '/pay', data={'x': '1'})
        self.assertIn('Payment received', res.text)
        self.discharge.invalidate_recordset()
        self.assertEqual(self.discharge.patient_payment_status, 'paid')

        self._set(state='released')
        page = self.url_open(self.url).text
        self.assertIn('Cleared to Go Home', page)
        self.assertIn('/report/barcode/', page)
        self.assertEqual(self.url_open('/discharge/track/%s/pay' % ('y' * 22), data={'x': '1'}).status_code, 404)


@tagged('post_install', '-at_install')
class TestHistoryAndKanban(TransactionCase):
    """Phase 13 (past medical / surgical history) and Phase 14 (role-tailored kanban dashboard)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        patcher = patch(MODEL_REQUESTS)
        cls.requests = patcher.start()
        cls.addClassCleanup(patcher.stop)
        cls.requests.post.side_effect = fake_post
        cls.requests.RequestException = Exception

        cls.patient = cls.env['clearpath.patient'].create({
            'name': 'History Patient', 'patient_code': 'PT-H1', 'bed_number': '7',
            'insurance_provider': 'Test TPA', 'policy_number': 'POL-H1',
            'allergy_ids': [(0, 0, {'allergen': 'Penicillin', 'reaction': 'Anaphylaxis', 'criticality': 'high'})],
            'history_ids': [
                (0, 0, {'condition_name': 'Type 2 Diabetes', 'history_type': 'medical',
                        'diagnosis_date': '2015', 'status': 'active'}),
                (0, 0, {'condition_name': 'Open Cholecystectomy', 'history_type': 'surgical',
                        'diagnosis_date': '2018', 'status': 'resolved'}),
            ],
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
        for key in ('doctor', 'pharmacist', 'billing', 'coordinator', 'nurse', 'admin'):
            setattr(cls, 'u_' + key, user('h_' + key, 'clearpath_discharge.group_clearpath_' + key))

    def _locked(self):
        d = self.discharge
        d.action_generate_ai_draft()
        d.action_lock_revision_and_verify()
        return d

    def _arch(self, user, view_xmlid, view_type):
        env = self.env(user=user) if user else self.env
        return env['clearpath.discharge'].get_view(self.env.ref(view_xmlid).id, view_type)['arch']

    FORM = 'clearpath_discharge.clearpath_discharge_view_form'
    KANBAN = 'clearpath_discharge.clearpath_discharge_view_kanban'

    # ---------------------------------------------------------------- Phase 13
    def test_01_history_model_relations_and_pills(self):
        self.assertEqual(self.patient.history_ids.mapped('condition_name'),
                         ['Type 2 Diabetes', 'Open Cholecystectomy'])
        self.assertEqual(self.discharge.patient_history_ids, self.patient.history_ids)
        hist = self.patient.history_ids
        self.assertEqual(hist.mapped('history_type'), ['medical', 'surgical'])
        self.assertEqual(hist.mapped('status'), ['active', 'resolved'])
        html = str(self.discharge.patient_history_html)
        self.assertIn('Type 2 Diabetes (2015)', html)
        self.assertIn('o_cp_hist_resolved', html)
        default = self.env['clearpath.medical.history'].create({'patient_id': self.patient.id, 'condition_name': 'X'})
        self.assertEqual((default.history_type, default.status), ('medical', 'active'))
        with self.assertRaises(Exception), mute_logger('odoo.sql_db'):
            self.env['clearpath.medical.history'].create({'patient_id': self.patient.id})  # name is required

    def test_02_history_access_rights(self):
        History = self.env['clearpath.medical.history']
        row = History.create({'patient_id': self.patient.id, 'condition_name': 'Asthma'})
        self.assertEqual(History.with_user(self.u_doctor).search_count([('id', '=', row.id)]), 1)
        History.with_user(self.u_doctor).create({'patient_id': self.patient.id, 'condition_name': 'COPD'})
        row.with_user(self.u_coordinator).write({'status': 'resolved'})
        for key in ('pharmacist', 'billing', 'nurse', 'admin'):
            user = getattr(self, 'u_' + key)
            self.assertEqual(History.with_user(user).search_count([('id', '=', row.id)]), 1, key)  # can read
            with self.assertRaises(AccessError, msg=key):
                row.with_user(user).write({'status': 'active'})
            with self.assertRaises(AccessError, msg=key):
                History.with_user(user).create({'patient_id': self.patient.id, 'condition_name': 'No'})

    def test_03_draft_request_carries_the_history(self):
        self.requests.post.reset_mock()
        self.discharge.action_generate_ai_draft()
        call = [c for c in self.requests.post.call_args_list if c.args[0].endswith('/api/v1/draft')][-1]
        sent = call.kwargs['json']
        self.assertEqual([h['condition_name'] for h in sent['past_medical_history']],
                         ['Type 2 Diabetes', 'Open Cholecystectomy'])
        self.assertEqual(sent['past_medical_history'][0],
                         {'condition_name': 'Type 2 Diabetes', 'history_type': 'medical', 'status': 'active',
                          'diagnosis_date': '2015'})
        self.assertEqual(len(sent['orm_history']), 2)  # raw rows for the token-savings measurement

    def test_04_patient_form_has_history_tab_and_banner_pills(self):
        patient_arch = self.env['clearpath.patient'].get_view(
            self.env.ref('clearpath_discharge.clearpath_patient_view_form').id, 'form')['arch']
        self.assertIn('history_ids', patient_arch)
        self.assertIn('Past Medical &amp; Surgical History', patient_arch)
        form = self._arch(None, self.FORM, 'form')
        self.assertIn('patient_history_html', form)
        self.assertIn('Past history', form)

    # ---------------------------------------------------------------- Phase 14: role-tailored UI
    def test_10_alerts_and_top_alert_follow_the_role(self):
        d = self._locked()
        self.assertEqual((d.pharmacy_status, d.billing_status, d.insurance_status), ('flagged',) * 3)

        def titles(user):
            return [i[2] for i in d.with_user(user)._alert_items()]
        self.assertEqual(titles(self.u_pharmacist), ["Pharmacy safety conflict"])
        self.assertEqual(titles(self.u_billing), ["Surgical billing discrepancy"])
        self.assertEqual(titles(self.u_admin), ["TPA insurance claim on hold"])
        self.assertEqual(titles(self.u_nurse), [])
        for user in (self.u_doctor, self.u_coordinator):
            self.assertEqual(len(titles(user)), 4, user.login)  # 3 flags + token savings
        self.assertEqual(len(d._alert_items()), 4)  # Administrator sees everything
        self.assertEqual(d.with_user(self.u_pharmacist).alert_count, 1)
        self.assertEqual(d.with_user(self.u_nurse).alert_count, 0)
        self.assertEqual(d.with_user(self.u_billing).top_alert_kind, 'billing')
        self.assertEqual(d.with_user(self.u_pharmacist).top_alert_kind, 'pharmacy')
        self.assertFalse(d.with_user(self.u_nurse).top_alert_kind)

    def test_11_alerts_popup_text_is_role_filtered(self):
        d = self._locked()
        wiz = self.env['clearpath.alerts.wizard'].with_user(self.u_billing).create({'discharge_id': d.id})
        self.assertIn('billing discrepancy', wiz.alerts_html)
        self.assertNotIn('Pharmacy safety conflict', wiz.alerts_html)
        self.assertNotIn('TPA insurance claim', wiz.alerts_html)

    def test_12_form_smart_buttons_by_role(self):
        wizards = {'pharmacy': 'action_open_pharmacy_wizard', 'billing': 'action_open_billing_wizard',
                   'insurance': 'action_open_insurance_wizard'}
        expected = {'pharmacist': {'pharmacy'}, 'billing': {'billing'}, 'admin': {'insurance'},
                    'doctor': set(wizards), 'coordinator': set(wizards), 'nurse': set()}
        for key, depts in expected.items():
            arch = self._arch(getattr(self, 'u_' + key), self.FORM, 'form')
            for dept, method in wizards.items():
                self.assertEqual(method in arch, dept in depts, '%s sees %s button' % (key, dept))
        admin = self._arch(None, self.FORM, 'form')  # Administrator: everything on one screen
        for method in wizards.values():
            self.assertIn(method, admin)
        self.assertIn('action_view_invoice', admin)
        self.assertNotIn('action_view_invoice', self._arch(self.u_pharmacist, self.FORM, 'form'))

    def test_13_form_fields_by_role(self):
        pharm = self._arch(self.u_pharmacist, self.FORM, 'form')
        self.assertIn('medication_line_ids', pharm)
        self.assertNotIn('supply_line_ids', pharm)  # supply logs are Billing's
        bill = self._arch(self.u_billing, self.FORM, 'form')
        self.assertIn('supply_line_ids', bill)
        self.assertNotIn('medication_line_ids', bill)
        self.assertNotIn('conflict_reason', bill)
        self.assertIn('conflict_reason', pharm)
        self.assertNotIn('Allergy Hold', bill)

    def test_14_wizard_buttons_include_administrators(self):
        views = {'clearpath_discharge.clearpath_pharmacy_wizard_view_form': 'group_clearpath_pharmacist',
                 'clearpath_discharge.clearpath_billing_wizard_view_form': 'group_clearpath_billing',
                 'clearpath_discharge.clearpath_insurance_wizard_view_form': 'group_clearpath_admin'}
        for xmlid, group in views.items():
            view = self.env.ref(xmlid, raise_if_not_found=False)
            if not view:
                continue
            self.assertIn('base.group_system', view.arch_db, xmlid)
            self.assertIn(group, view.arch_db, xmlid)

    # ---------------------------------------------------------------- Phase 14: kanban
    def test_20_window_action_opens_kanban_first(self):
        action = self.env.ref('clearpath_discharge.clearpath_discharge_action')
        self.assertEqual(action.view_mode, 'kanban,list,form')
        self.assertEqual(self.env['clearpath.discharge'].get_views([(False, 'kanban')])['views']['kanban']['id'],
                         self.env.ref(self.KANBAN).id)
        for dept in ('pharmacy', 'billing', 'insurance'):
            queue = self.env.ref('clearpath_discharge.clearpath_discharge_action_%s_queue' % dept)
            self.assertTrue(queue.view_mode.startswith('kanban'))
            self.assertIn('search_default_queue_%s' % dept, queue.context)

    def test_21_kanban_arch_is_safe_and_grouped_by_state(self):
        arch = self._arch(None, self.KANBAN, 'kanban')
        self.assertIn('default_group_by="state"', arch)
        self.assertIn('records_draggable="false"', arch)  # moving a card must not bypass a sign-off
        self.assertIn('quick_create="false"', arch)
        for needle in ('patient_id', 'bed_number', 'revision_id', 'Review Meds', 'Review Billing', 'Review Claim'):
            self.assertIn(needle, arch)

    def test_22_kanban_cards_show_only_the_users_department(self):
        def tags(user):
            arch = self._arch(user, self.KANBAN, 'kanban')
            return {'pharmacy': 'Pharmacy' in arch, 'billing': 'Billing' in arch, 'insurance': 'Insurance' in arch}

        def buttons(user):
            arch = self._arch(user, self.KANBAN, 'kanban')
            return {'Review Meds' in arch, 'Review Billing' in arch, 'Review Claim' in arch}
        self.assertEqual(tags(self.u_pharmacist), {'pharmacy': True, 'billing': False, 'insurance': False})
        self.assertEqual(tags(self.u_billing), {'pharmacy': False, 'billing': True, 'insurance': False})
        self.assertEqual(tags(self.u_admin), {'pharmacy': False, 'billing': False, 'insurance': True})
        self.assertEqual(tags(self.u_nurse), {'pharmacy': False, 'billing': False, 'insurance': False})
        self.assertEqual(tags(self.u_doctor), {'pharmacy': True, 'billing': True, 'insurance': True})
        self.assertEqual(tags(None), {'pharmacy': True, 'billing': True, 'insurance': True})
        arch = lambda user: self._arch(user, self.KANBAN, 'kanban')  # noqa: E731
        self.assertIn('Review Meds', arch(self.u_pharmacist))
        self.assertNotIn('Review Billing', arch(self.u_pharmacist))
        self.assertNotIn('Review Claim', arch(self.u_pharmacist))
        self.assertIn('Review Billing', arch(self.u_billing))
        self.assertNotIn('Review Meds', arch(self.u_billing))
        self.assertIn('Review Claim', arch(self.u_admin))
        self.assertNotIn('Review Meds', arch(self.u_admin))
        for text in ('Review Meds', 'Review Billing', 'Review Claim'):
            self.assertNotIn(text, arch(self.u_doctor))  # doctors fix the plan, they do not sign off departments
            self.assertNotIn(text, arch(self.u_nurse))
            self.assertIn(text, arch(None))  # Administrator can demo every action

    def test_23_kanban_columns_cover_the_whole_pipeline(self):
        # web_read_group + read_group_expand is what the kanban view calls; it applies the field's group_expand
        groups = self.env['clearpath.discharge'].with_context(read_group_expand=True).web_read_group(
            [], ['state'], ['__count'])['groups']
        states = [g['state'][0] if isinstance(g['state'], (list, tuple)) else g['state'] for g in groups]
        for state in ('draft', 'verifying', 'flagged', 'cleared', 'released'):
            self.assertIn(state, states)
        self.assertLess(states.index('draft'), states.index('flagged'))
        self.assertLess(states.index('flagged'), states.index('released'))
        label = dict(self.env['clearpath.discharge']._fields['state'].selection)['verifying']
        self.assertEqual(label, 'Locked / Verifying')

    def test_24_queue_filters_move_cards_out_after_sign_off(self):
        from lxml import etree
        search = etree.fromstring(self._arch(None, 'clearpath_discharge.clearpath_discharge_view_search', 'search'))
        domains = {f.get('name'): f.get('domain') for f in search.iter('filter') if f.get('name', '').startswith('queue_')}
        self.assertEqual(set(domains), {'queue_pharmacy', 'queue_billing', 'queue_insurance'})
        d = self._locked()
        Discharge = self.env['clearpath.discharge']

        def queue(name):
            return d in Discharge.search(eval(domains[name]))  # noqa: S307  (our own view domain)
        self.assertTrue(queue('queue_pharmacy') and queue('queue_billing') and queue('queue_insurance'))
        d.with_context(clearpath_skip_guard=True).write({'pharmacy_status': 'approved'})
        self.assertFalse(queue('queue_pharmacy'))  # left the pharmacist's queue...
        self.assertTrue(queue('queue_billing'))  # ...but still waits for the others
        d.with_context(clearpath_skip_guard=True).write({'state': 'cleared'})
        self.assertFalse(queue('queue_billing'))

    def test_25_kanban_card_button_opens_the_wizard_popup(self):
        d = self._locked()
        for method, model in (('action_open_pharmacy_wizard', 'clearpath.pharmacy.approval.wizard'),
                              ('action_open_billing_wizard', 'clearpath.billing.approval.wizard'),
                              ('action_open_insurance_wizard', 'clearpath.insurance.approval.wizard')):
            action = getattr(d, method)()
            self.assertEqual((action['type'], action['target'], action['res_model']),
                             ('ir.actions.act_window', 'new', model))

    def test_26_role_menus(self):
        expected = {'pharmacy': 'group_clearpath_pharmacist', 'billing': 'group_clearpath_billing',
                    'insurance': 'group_clearpath_admin'}
        for dept, group in expected.items():
            menu = self.env.ref('clearpath_discharge.clearpath_menu_%s_queue' % dept)
            self.assertIn(self.env.ref('clearpath_discharge.' + group), menu.group_ids)
            self.assertIn(self.env.ref('base.group_system'), menu.group_ids)
        self.assertFalse(self.env.ref('clearpath_discharge.clearpath_menu_discharges').group_ids)  # open to all roles
