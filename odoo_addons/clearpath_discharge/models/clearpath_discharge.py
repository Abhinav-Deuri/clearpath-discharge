import hashlib
import json
import logging
import re
import secrets
from urllib.parse import urlencode

import requests

from markupsafe import Markup, escape

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Editing any of these on a locked discharge triggers the Stale-Write Guard.
CLINICAL_FIELDS = {'raw_handoff_note', 'ai_draft_summary'}

# Demo content lives here ONLY (not in demo XML): noupdate XML records that were deleted get re-created on every
# module upgrade, which is how the demo supply lines used to double up. The patient's real name and bed are
# filled in at seed time, so renaming the demo patient never leaves a stale name in the note.
DEMO_NOTE_TEMPLATE = (
    "Rounds note - Bed {bed}, {name}, POD1 laparoscopic appendectomy. Uncomplicated course, afebrile, "
    "pain controlled on oral analgesia, tolerating regular diet, ambulating independently. Incisions "
    "clean and dry. Plan: discharge home today. Start Amoxicillin 500mg three times daily for 5 days as "
    "post-operative prophylaxis. Surgical clinic follow-up in 2 weeks."
)
DEMO_MEDICATIONS = [
    {'drug_name': 'Amoxicillin', 'dosage': '500mg', 'frequency': 'TID'},
]
DEMO_SUPPLIES = [
    {'item_name': 'Hemostatic Gauze Pad', 'surgical_qty_used': 4, 'billed_qty': 2, 'price_unit': 12.5},
    {'item_name': 'Skin Stapler', 'surgical_qty_used': 1, 'billed_qty': 0, 'price_unit': 85.0},
]

GROUP_DOCTOR = 'clearpath_discharge.group_clearpath_doctor'
GROUP_PHARMACIST = 'clearpath_discharge.group_clearpath_pharmacist'
GROUP_BILLING = 'clearpath_discharge.group_clearpath_billing'
GROUP_COORDINATOR = 'clearpath_discharge.group_clearpath_coordinator'
GROUP_NURSE = 'clearpath_discharge.group_clearpath_nurse'
GROUP_ADMIN = 'clearpath_discharge.group_clearpath_admin'

# Who may see each department's flags, alerts and queue (Administrators always can).
# Doctor and Coordinator work across departments; Nurse sees none; each department sees only its own.
DEPT_ROLE_GROUPS = {
    'pharmacy': (GROUP_PHARMACIST, GROUP_DOCTOR, GROUP_COORDINATOR),
    'billing': (GROUP_BILLING, GROUP_DOCTOR, GROUP_COORDINATOR),
    'insurance': (GROUP_ADMIN, GROUP_DOCTOR, GROUP_COORDINATOR),
}


def md_to_html(text):
    """Tiny Markdown -> HTML (headings, bold, bullets, pipe tables). markdown2 is not in the image."""
    def inline(t):
        return Markup(re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', str(escape(t))))

    out, rows, bullets = [], [], []

    def flush_table():
        if rows:
            head, body = rows[0], rows[1:]
            h = ''.join('<th style="text-align:left; padding:2px 8px; border-bottom:1px solid #d1d5db;">%s</th>' % inline(c) for c in head)
            b = ''.join('<tr>%s</tr>' % ''.join('<td style="padding:2px 8px;">%s</td>' % inline(c) for c in r) for r in body)
            out.append('<table style="width:100%%; font-size:12px; margin:4px 0;"><tr>%s</tr>%s</table>' % (h, b))
            rows.clear()

    def flush_bullets():
        if bullets:
            out.append('<ul style="margin:2px 0 4px 18px; padding:0;">%s</ul>' % ''.join('<li>%s</li>' % inline(x) for x in bullets))
            bullets.clear()

    for raw in (text or '').splitlines():
        line = raw.strip()
        if _RULE_RE.match(line):
            continue
        if line.startswith('|'):
            flush_bullets()
            cells = [c.strip() for c in line.strip('|').split('|')]
            if not all(re.fullmatch(r':?-{2,}:?', c) for c in cells if c):
                rows.append(cells)
            continue
        flush_table()
        m = re.match(r'^[-*]\s+(.*)', line)
        if m:
            bullets.append(m.group(1))
            continue
        flush_bullets()
        h = re.match(r'^(#{1,6})\s+(.*)', line)
        if h:
            size = 15 if len(h.group(1)) <= 1 else 13
            out.append('<div style="font-weight:700; font-size:%dpx; margin:8px 0 2px;">%s</div>' % (size, inline(h.group(2))))
        elif line:
            out.append('<div style="margin:2px 0;">%s</div>' % inline(line))
    flush_table()
    flush_bullets()
    return Markup(''.join(out))

_RULE_RE = re.compile(r'^\s*([-*_]\s*){3,}$')  # markdown horizontal rules ("---")
_ROW_RE = re.compile(r'^(?:[-*\u2022]\s+)?\*\*(.+?)\*\*\s*:?\s*(.*)$')
_SUB_BULLET_RE = re.compile(r'^\s*[-*\u2022]\s+(?!\*\*)(.*)$')
_HEADING_RE = re.compile(r'^#{1,6}\s+(.*)$')
_SUMMARY_ICONS = (
    ('surgical', 'content_cut'), ('medical history', 'stethoscope'),
    ('patient', 'person'), ('procedure', 'content_cut'), ('condition', 'content_cut'),
    ('course', 'favorite'), ('incision', 'medical_services'), ('plan', 'home'),
    ('medication', 'science'), ('allerg', 'error'), ('follow', 'history'),
)


def clean_summary_text(text):
    """Drop markdown horizontal rules (---) and blank runs from AI output."""
    lines = [ln for ln in (text or '').splitlines() if not _RULE_RE.match(ln)]
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()


def _md_inline(text):
    return Markup(re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', str(escape(text))))


def summary_to_html(text):
    """Discharge summary -> readable card (icon + small-caps label + value per row).

    Tables and inline styles only, so the same HTML renders in the form and in the wkhtmltopdf PDF.
    Text that is not `- **Label**: value` shaped falls back to md_to_html.
    """
    lines = [ln.rstrip() for ln in clean_summary_text(text).splitlines() if ln.strip()]
    if sum(1 for ln in lines if _ROW_RE.match(ln.strip()) and _ROW_RE.match(ln.strip()).group(2)) < 2:
        return md_to_html(clean_summary_text(text))

    title, rows = None, []  # rows: dict(label, value, bullets) | dict(heading=...) | dict(plain=...)
    for idx, raw in enumerate(lines):
        line = raw.strip()
        row = _ROW_RE.match(line)
        heading = _HEADING_RE.match(line)
        sub_bullet = _SUB_BULLET_RE.match(raw)
        bare = not row and not heading and not sub_bullet and len(line) <= 60 and not line.endswith(('.', ':'))
        if title is None and idx == 0 and (heading or bare or (row and not row.group(2))):
            title = (heading.group(1) if heading else row.group(1) if row else line).strip().rstrip(':')
        elif row:
            rows.append({'label': row.group(1).strip().rstrip(':'), 'value': row.group(2).strip(), 'bullets': []})
        elif heading:
            rows.append({'heading': heading.group(1).strip().rstrip(':')})
        elif sub_bullet and rows and 'label' in rows[-1]:
            rows[-1]['bullets'].append(sub_bullet.group(1).strip())
        else:
            rows.append({'plain': re.sub(r'^[-*\u2022]\s+', '', line)})

    td_icon = ('<td style="width:34px; padding:9px 0 9px 14px; vertical-align:top; text-align:center; '
               'color:%s;"><i class="oi" data-icon="%s"/></td>')
    border = 'border-top:1px solid rgba(128,128,128,.25);'
    body = []
    for r in rows:
        if 'heading' in r:
            body.append('<tr style="%s"><td colspan="3" style="padding:9px 14px 3px; font-weight:700; '
                        'font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:#6b7280;">%s</td></tr>'
                        % (border, escape(r['heading'])))
            continue
        if 'plain' in r:
            body.append('<tr style="%s"><td colspan="3" style="padding:8px 14px;">%s</td></tr>'
                        % (border, _md_inline(r['plain'])))
            continue
        label, key = r['label'], r['label'].lower()
        value = str(_md_inline(r['value'])) if r['value'] else ''
        if r['bullets']:
            value += '<ul style="margin:2px 0 0 18px; padding:0;">%s</ul>' % ''.join(
                '<li>%s</li>' % _md_inline(b) for b in r['bullets'])
        if 'review' in key:
            body.append(
                '<tr style="%s"><td colspan="3" style="padding:10px 14px;"><div style="border-left:4px solid #f59e0b; '
                'background:rgba(245,158,11,.14); border-radius:6px; padding:8px 12px;"><b>'
                '<i class="oi" data-icon="warning" style="color:#d97706;"/> %s</b><div style="margin-top:3px;">%s'
                '</div></div></td></tr>' % (border, escape(label), value))
            continue
        icon = next((ic for k, ic in _SUMMARY_ICONS if k in key), 'chevron_right')
        alert = 'allerg' in key
        body.append(
            '<tr style="%s">%s<td class="text-muted fw-bold small text-uppercase" style="width:140px; '
            'padding:9px 10px; vertical-align:top; font-size:11px; font-weight:700; text-transform:uppercase; '
            'letter-spacing:.05em; color:#6b7280;">%s</td>'
            '<td class="%s" style="padding:9px 14px 9px 0; vertical-align:top;%s">%s</td></tr>'
            % (border, td_icon % ('#dc3545' if alert else '#0d6efd', icon), escape(label),
               'text-danger fw-bold' if alert else '',
               ' color:#dc3545; font-weight:700;' if alert else '', value))

    head = ''
    # The form puts "Discharge Summary" in its own card header; other places keep the card title.
    if title and not title.lower().startswith('discharge summary'):
        head = ('<div style="padding:10px 14px; font-weight:700; font-size:15px; background:rgba(13,110,253,.10);">'
                '<i class="oi" data-icon="description" style="color:#0d6efd;"/> %s</div>' % escape(title))
    return Markup(
        '<div style="border:1px solid rgba(128,128,128,.35); border-radius:10px; overflow:hidden; font-size:13px;">'
        '%s<table style="width:100%%; border-collapse:collapse;">%s</table></div>' % (head, ''.join(body)))


class ClearpathDischarge(models.Model):
    _name = 'clearpath.discharge'
    _description = 'ClearPath Discharge Encounter'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(compute='_compute_name', store=True)
    patient_id = fields.Many2one('clearpath.patient', required=True, tracking=True)
    patient_code = fields.Char(related='patient_id.patient_code', store=True)
    bed_number = fields.Char(related='patient_id.bed_number')

    # draft -> locked -> verifying -> flagged -> cleared -> released
    #   locked    : doctor locked the revision, sidecar call in flight
    #   verifying : checks passed with no flags, awaiting departmental sign-off
    #   flagged   : checks found a conflict/discrepancy, awaiting resolution + sign-off
    #   cleared   : pharmacy AND billing approved the SAME revision (HITL gate #2 open)
    #   released  : coordinator released the final locked packet
    state = fields.Selection(
        [('draft', 'Draft'), ('locked', 'Locked'), ('verifying', 'Locked / Verifying'),
         ('flagged', 'Flagged'), ('cleared', 'Cleared'), ('released', 'Released')],
        default='draft', required=True, tracking=True, copy=False,
        group_expand='_read_group_expand_state',  # kanban shows every column, even empty ones
    )

    revision_id = fields.Integer(string="Revision", default=1, readonly=True, copy=False)
    revision_hash = fields.Char(string="Snapshot Hash", readonly=True, copy=False)
    sidecar_snapshot_id = fields.Char(string="Sidecar Snapshot", readonly=True, copy=False)

    raw_handoff_note = fields.Text(string="Handoff Note")
    ai_draft_summary = fields.Text(string="AI Draft Summary")
    review_prompts = fields.Text(string="Review Prompts")
    token_savings_summary = fields.Char(string="Token Savings", readonly=True, copy=False)

    medication_line_ids = fields.One2many('clearpath.medication.line', 'discharge_id', string="Medications")
    supply_line_ids = fields.One2many('clearpath.supply.line', 'discharge_id', string="Surgical Supplies")
    allergy_ids = fields.One2many(related='patient_id.allergy_ids')

    pharmacy_status = fields.Selection(
        [('pending', 'Pending'), ('flagged', 'Flagged'), ('approved', 'Approved'), ('stale', 'Stale')],
        default='pending', required=True, tracking=True, copy=False,
    )
    billing_status = fields.Selection(
        [('pending', 'Pending'), ('flagged', 'Flagged'), ('approved', 'Approved'), ('stale', 'Stale')],
        default='pending', required=True, tracking=True, copy=False,
    )
    pharmacy_summary = fields.Text(readonly=True, copy=False)
    billing_summary = fields.Text(readonly=True, copy=False)
    coordinator_gate_locked = fields.Boolean(default=True, readonly=True, copy=False)

    pharmacist_id = fields.Many2one('res.users', readonly=True, copy=False)
    pharmacy_approved_on = fields.Datetime(readonly=True, copy=False)
    billing_officer_id = fields.Many2one('res.users', readonly=True, copy=False)
    billing_approved_on = fields.Datetime(readonly=True, copy=False)
    coordinator_id = fields.Many2one('res.users', readonly=True, copy=False)
    released_on = fields.Datetime(readonly=True, copy=False)

    # --- Phase 5: multimodal inputs + TPA insurance
    voice_note_audio = fields.Binary(string="Doctor Voice Note (.mp3/.wav)", attachment=True, copy=False)
    voice_note_filename = fields.Char(string="Audio Filename", copy=False)
    nurse_checklist_image = fields.Binary(string="Nurse Supply Sheet Photo", attachment=True, copy=False)
    insurance_status = fields.Selection(
        [('pending', 'Pending'), ('flagged', 'Hold: Discrepancy'), ('approved', 'Approved'),
         ('stale', 'Stale (Invalidated)')],
        default='pending', required=True, tracking=True, copy=False,
    )
    tpa_claim_document = fields.Text(string="Auto-Filled TPA Insurance Claim", readonly=True, copy=False)
    tpa_claim_html = fields.Html(compute='_compute_tpa_claim_html', sanitize=False)
    icd10_cpt_codes = fields.Char(string="Mapped ICD-10 / CPT Codes", readonly=True, copy=False)
    insurance_hold_reasons = fields.Text(readonly=True, copy=False)
    insurance_approver_id = fields.Many2one('res.users', readonly=True, copy=False)
    insurance_approved_on = fields.Datetime(readonly=True, copy=False)
    tpa_provider = fields.Char(related='patient_id.insurance_provider', string="TPA Provider")
    policy_number = fields.Char(related='patient_id.policy_number', string="Policy Number")

    audit_snapshot_json = fields.Text(string="Audit Snapshot JSON", readonly=True, copy=False)
    locked_on = fields.Datetime(readonly=True, copy=False)
    ai_draft_summary_html = fields.Html(
        compute='_compute_ai_draft_summary_html', sanitize=False)
    audit_report_html = fields.Html(
        string="Audit Report", compute='_compute_audit_report_html', sanitize=False,
    )

    # --- Phase 6: numbers and labels shown on the smart (stat) buttons
    pharmacy_conflict_count = fields.Integer(compute='_compute_stat_buttons')
    unbilled_item_count = fields.Integer(compute='_compute_stat_buttons')
    insurance_hold_count = fields.Integer(compute='_compute_stat_buttons')
    pharmacy_button_text = fields.Char(compute='_compute_stat_buttons')
    billing_button_text = fields.Char(compute='_compute_stat_buttons')
    insurance_button_text = fields.Char(compute='_compute_stat_buttons')

    # --- Phase 7: native Invoicing
    invoice_id = fields.Many2one('account.move', string="Generated Invoice", readonly=True, copy=False)
    invoice_label = fields.Char(compute='_compute_invoice_label')
    alert_count = fields.Integer(compute='_compute_alert_count')
    short_revision_hash = fields.Char(compute='_compute_short_revision_hash')
    show_audit_log = fields.Boolean(string="Show Audit Log", default=False, copy=False)

    # Phase 12: patient SMS + login-free live tracker + co-pay
    tracking_token = fields.Char(string="Patient Tracking Token", copy=False, readonly=True, index=True)
    patient_tracker_url = fields.Char(string="Live Patient Tracker URL", compute='_compute_patient_tracker_url')
    copay_amount = fields.Float(string="Patient Co-Pay Amount (AED)", default=150.0)
    patient_payment_status = fields.Selection(
        [('unpaid', 'Awaiting Payment'), ('paid', 'Paid by Patient')],
        string="Co-Pay", default='unpaid', required=True, copy=False, tracking=True)
    patient_paid_on = fields.Datetime(string="Co-Pay Paid On", readonly=True, copy=False)
    last_sms_status = fields.Char(string="Last SMS Notification", readonly=True, copy=False)
    copay_sms_sent = fields.Boolean(readonly=True, copy=False)

    # Phase 13: past medical / surgical history (read from the patient, fed to the AI draft)
    patient_history_ids = fields.One2many(related='patient_id.history_ids', readonly=True)
    patient_history_html = fields.Html(compute='_compute_patient_history_html', sanitize=False)
    top_alert_kind = fields.Selection(
        [('pharmacy', 'Pharmacy'), ('billing', 'Billing'), ('insurance', 'Insurance')],
        compute='_compute_top_alert')
    top_alert_title = fields.Char(compute='_compute_top_alert')

    # ------------------------------------------------------------------ helpers
    @api.depends('tpa_claim_document')
    def _compute_tpa_claim_html(self):
        for rec in self:
            rec.tpa_claim_html = md_to_html(rec.tpa_claim_document)

    @api.depends('ai_draft_summary')
    def _compute_ai_draft_summary_html(self):
        for rec in self:
            rec.ai_draft_summary_html = summary_to_html(rec.ai_draft_summary)

    @api.depends(
        'state', 'revision_id', 'revision_hash', 'sidecar_snapshot_id', 'ai_draft_summary',
        'ai_draft_summary_html',
        'pharmacy_status', 'billing_status', 'pharmacy_summary', 'billing_summary',
        'audit_snapshot_json', 'locked_on', 'released_on', 'pharmacy_approved_on',
        'billing_approved_on', 'insurance_status', 'icd10_cpt_codes', 'insurance_hold_reasons',
        'insurance_approved_on', 'medication_line_ids.is_flagged', 'medication_line_ids.drug_name',
        'supply_line_ids.billed_qty', 'supply_line_ids.surgical_qty_used',
    )
    def _compute_audit_report_html(self):
        qweb = self.env['ir.qweb']
        for rec in self:
            rec.audit_report_html = qweb._render('clearpath_discharge.audit_body', {'o': rec})

    @staticmethod
    def _stat_text(state, status, count, one, many):
        if state == 'draft' and status != 'stale':
            return "Not run"
        if status == 'stale':
            return "Stale"
        if status == 'approved':
            return "Approved"
        return ("%d %s" % (count, one if count == 1 else many)) if count else "Clear"

    @api.depends('state', 'pharmacy_status', 'billing_status', 'insurance_status',
                 'insurance_hold_reasons', 'medication_line_ids.is_flagged',
                 'supply_line_ids.discrepancy_qty')
    def _compute_stat_buttons(self):
        for rec in self:
            conflicts = len(rec.medication_line_ids.filtered('is_flagged'))
            unbilled = len(rec.supply_line_ids.filtered(lambda s: s.discrepancy_qty > 0))
            holds = len([l for l in (rec.insurance_hold_reasons or '').splitlines() if l.strip()])
            rec.pharmacy_conflict_count = conflicts
            rec.unbilled_item_count = unbilled
            rec.insurance_hold_count = holds
            rec.pharmacy_button_text = self._stat_text(
                rec.state, rec.pharmacy_status, conflicts, "Allergy Flag", "Allergy Flags")
            rec.billing_button_text = self._stat_text(
                rec.state, rec.billing_status, unbilled, "Unbilled Item", "Unbilled Items")
            text = self._stat_text(rec.state, rec.insurance_status, holds, "Hold", "Holds")
            if text == "Clear" and rec.insurance_status == 'flagged':
                text = "On Hold"
            rec.insurance_button_text = text

    @api.model
    def _read_group_expand_state(self, groups, domain):
        """Kanban: always show the whole pipeline, left to right (a transient 'locked' card still shows)."""
        order = ['draft', 'verifying', 'flagged', 'cleared', 'released']
        return order + [g for g in groups if g not in order]

    def _can_see_dept(self, dept):
        """Role-tailored UI: may the current user see this department's flags and alerts?"""
        user = self.env.user
        if self.env.is_superuser() or user._is_admin():
            return True
        return any(user.has_group(g) for g in DEPT_ROLE_GROUPS[dept])

    @api.depends('patient_id.history_ids.condition_name', 'patient_id.history_ids.history_type',
                 'patient_id.history_ids.status', 'patient_id.history_ids.diagnosis_date')
    def _compute_patient_history_html(self):
        for rec in self:
            pills = []
            for h in rec.patient_id.history_ids:
                label = h.condition_name + (" (%s)" % h.diagnosis_date if h.diagnosis_date else "")
                pills.append(Markup('<span class="o_cp_hist_pill o_cp_hist_%s o_cp_hist_%s" title="%s, %s">%s</span>')
                             % (h.history_type, h.status, dict(h._fields['history_type'].selection)[h.history_type],
                                dict(h._fields['status'].selection)[h.status], label))
            rec.patient_history_html = Markup('').join(pills) if pills else False

    def _history_payload(self):
        return [{'condition_name': h.condition_name, 'history_type': h.history_type, 'status': h.status,
                 'diagnosis_date': h.diagnosis_date or ''} for h in self.patient_id.history_ids]

    def _alert_items(self):
        """What needs attention on this revision, limited to what the current user's role may see.
        Shown in the Review Alerts pop-up, not inline."""
        self.ensure_one()
        see = {dept: self._can_see_dept(dept) for dept in DEPT_ROLE_GROUPS}
        see_all = all(see.values())
        items = []
        if see['pharmacy'] and self.pharmacy_status == 'flagged':
            items.append(('danger', ('warning', 'cp-pulse'), "Pharmacy safety conflict",
                          "%s Open the Pharmacy button for details. Change the medication to create a "
                          "new revision, then re-run checks." % (self.pharmacy_summary or "")))
        if see['billing'] and self.billing_status == 'flagged':
            items.append(('warning', ('error', 'cp-shake'), "Surgical billing discrepancy",
                          "%s Open the Billing button, correct the billed quantities, then sign off."
                          % (self.billing_summary or "")))
        if see['insurance'] and self.insurance_status == 'flagged':
            items.append(('warning', ('pause_circle', 'cp-beat'), "TPA insurance claim on hold",
                          "Open the Insurance button. The claim is released for admin sign-off only after "
                          "every discrepancy is reconciled."))
        if any(see[d] and getattr(self, '%s_status' % d) == 'stale' for d in see):
            items.append(('secondary', ('refresh', ''), "Stale-Write Guard",
                          "The record was edited after locking. The previous checks and sign-offs are "
                          "invalid. Lock this revision again to re-verify."))
        if see_all and self.state == 'cleared' and not self.coordinator_gate_locked:
            items.append(('success', ('lock_open', 'cp-beat'), "Release gate open",
                          "Pharmacy, Billing and Insurance all approved this revision."))
        if see_all and self.token_savings_summary:
            items.append(('info', ('flash_on', ''), "Token savings before AI call", self.token_savings_summary))
        return items

    @api.depends_context('uid')
    @api.depends('state', 'pharmacy_status', 'billing_status', 'insurance_status',
                 'coordinator_gate_locked', 'token_savings_summary')
    def _compute_alert_count(self):
        for rec in self:
            rec.alert_count = len(rec._alert_items())

    @api.depends_context('uid')
    @api.depends('state', 'pharmacy_status', 'billing_status', 'insurance_status',
                 'medication_line_ids.is_flagged', 'medication_line_ids.conflict_reason',
                 'supply_line_ids.discrepancy_qty', 'patient_id.allergy_ids.allergen')
    def _compute_top_alert(self):
        for rec in self:
            kind, title = False, False
            if rec.pharmacy_status == 'flagged' and rec._can_see_dept('pharmacy'):
                reasons = " ".join(rec.medication_line_ids.filtered('is_flagged').mapped('conflict_reason')
                                   ).lower()
                allergens = rec.patient_id.allergy_ids.mapped('allergen')
                hit = [a for a in allergens if a and a.lower() in reasons] or [
                    a.allergen for a in rec.patient_id.allergy_ids if a.criticality == 'high'][:1]
                kind = 'pharmacy'
                title = "%s Allergy Conflict" % (", ".join(hit) if hit else "Drug")
            elif rec.billing_status == 'flagged' and rec._can_see_dept('billing'):
                n = len(rec.supply_line_ids.filtered(lambda s: s.discrepancy_qty > 0))
                kind, title = 'billing', "%d unbilled surgical item%s" % (n, '' if n == 1 else 's')
            elif rec.insurance_status == 'flagged' and rec._can_see_dept('insurance'):
                kind, title = 'insurance', "TPA insurance claim on hold"
            rec.top_alert_kind, rec.top_alert_title = kind, title

    def action_open_alerts_wizard(self):
        return self._open_wizard('clearpath_discharge.alerts_wizard_action', size='medium')

    def _alerts_action_or_reload(self):
        """After lock / late edit: pop the alerts up (the form refreshes when it closes)."""
        self.ensure_one()
        return self.action_open_alerts_wizard() if self._alert_items() else self._soft_reload()

    @api.depends('invoice_id.name', 'invoice_id.state')
    def _compute_invoice_label(self):
        for rec in self:
            move = rec.invoice_id
            rec.invoice_label = (move.name if move and move.name and move.name != '/' else "Draft Invoice") \
                if move else False

    @api.depends('patient_id.patient_code')
    def _compute_name(self):
        for rec in self:
            rec.name = "%s Discharge" % (rec.patient_id.patient_code or "New")

    def _require_group(self, xmlid, label):
        """Admin/superuser always allowed (single-screen demo). Groups arrive in Phase 3."""
        if self.env.is_superuser() or self.env.user._is_admin():
            return
        if not self.env.ref(xmlid, raise_if_not_found=False):
            return
        if not self.env.user.has_group(xmlid):
            raise UserError("Only a %s can do this." % label)

    def _require_any_group(self, xmlids, label):
        """Allow if the user holds ANY of the groups (admin always passes)."""
        if self.env.is_superuser() or self.env.user._is_admin():
            return
        existing = [x for x in xmlids if self.env.ref(x, raise_if_not_found=False)]
        if existing and not any(self.env.user.has_group(x) for x in existing):
            raise UserError("Only %s can do this." % label)

    def _single_flight(self, key):
        """Fail fast if the same long-running action is already running for this record."""
        self.env.cr.execute("SELECT pg_try_advisory_xact_lock(%s, %s)", (key, self.id))
        if not self.env.cr.fetchone()[0]:
            raise UserError("This action is already running for this discharge. Please wait.")

    def _toast(self, title, message):
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': title, 'message': message, 'type': 'success', 'sticky': False,
                       'next': self._soft_reload()},
        }

    @staticmethod
    def _soft_reload():
        """Refresh the open form after a button so new values show immediately."""
        return {'type': 'ir.actions.client', 'tag': 'soft_reload'}

    def _assert_not_released(self):
        if any(rec.state == 'released' for rec in self):
            raise UserError("This discharge packet is released and locked.")

    def _compute_revision_hash(self):
        self.ensure_one()
        meds = sorted(
            (m.drug_name or '', m.dosage or '', m.frequency or '')
            for m in self.medication_line_ids
        )
        supplies = sorted(
            ((l.item_name or '').strip().lower(), l.surgical_qty_used) for l in self.supply_line_ids
        )
        blob = json.dumps(
            {'rev': self.revision_id, 'note': self.raw_handoff_note or '',
             'summary': self.ai_draft_summary or '', 'meds': meds, 'supplies': supplies},
            sort_keys=True,
        )
        return hashlib.sha256(blob.encode()).hexdigest()

    @api.depends('revision_hash')
    def _compute_short_revision_hash(self):
        for rec in self:
            rec.short_revision_hash = ('#' + rec.revision_hash[:8]) if rec.revision_hash else False

    def action_toggle_audit_log(self):
        """Show / hide the chatter (audit history) on the form; hidden by default for a clean demo."""
        for record in self:
            record.show_audit_log = not record.show_audit_log

    def message_post(self, **kwargs):
        """Our chatter notes use <b>; Odoo escapes plain-string bodies, so keep ONLY <b> tags."""
        body = kwargs.get('body')
        if isinstance(body, str) and not isinstance(body, Markup):
            kwargs['body'] = Markup(
                str(escape(body)).replace('&lt;b&gt;', '<b>').replace('&lt;/b&gt;', '</b>'))
        return super().message_post(**kwargs)

    def _verify_payload(self):
        self.ensure_one()
        return {
            'discharge_id': self.id,
            'revision_id': self.revision_id,
            'revision_hash': self.revision_hash,
            'patient_code': self.patient_id.patient_code,
            'medications': self._medications_payload(),
            'allergies': self._allergies_payload(),
            'supplies': self._supplies_payload(),
            'push_to_odoo': False,  # Odoo applies the response itself (no self-callback)
            'procedure_text': ' '.join(filter(None, [self.raw_handoff_note, self.ai_draft_summary])),
            'tpa_provider': self.patient_id.insurance_provider or '',
            'policy_number': self.patient_id.policy_number or '',
        }

    def _refresh_insurance_claim(self, reason):
        """A billing correction changes what the TPA claim must say. Rebuild the claim for the SAME revision
        (the Insurance worker runs again) and drop any insurance sign-off that was based on the old claim."""
        for rec in self.filtered(lambda r: r.state in ('flagged', 'verifying', 'cleared')):
            snapshot = rec._post_sidecar('/api/v1/verify', rec._verify_payload(), timeout=30)
            ins = snapshot.get('insurance_output') or {}
            ins_flag = bool(ins) and ins.get('claim_status') != 'ready_for_review'
            codes = ' / '.join(x for x in (ins.get('icd10_code'), ins.get('cpt_code')) if x)
            vals = {
                'tpa_claim_document': ins.get('filled_claim_markdown') or False,
                'icd10_cpt_codes': codes or False,
                'insurance_hold_reasons': "\n".join("- " + h for h in ins.get('hold_reasons', [])) or False,
                'insurance_status': 'flagged' if ins_flag else 'pending',
            }
            was_approved = rec.insurance_status == 'approved'
            if was_approved:  # the approved claim no longer matches the billing: re-approval needed
                vals.update({'insurance_approver_id': False, 'insurance_approved_on': False,
                             'coordinator_gate_locked': True, 'state': 'flagged'})
            rec.with_context(clearpath_skip_guard=True).write(vals)
            rec.message_post(body="TPA claim rebuilt for Rev %d after %s.%s" % (
                rec.revision_id, reason,
                " The previous insurance sign-off was withdrawn." if was_approved else ""))

    def _sidecar_url(self):
        return self.env['ir.config_parameter'].sudo().get_str(
            'clearpath.sidecar_url', 'http://sidecar_service:8000').rstrip('/')

    def _medications_payload(self):
        return [{'drug_name': m.drug_name, 'dosage': m.dosage or '', 'frequency': m.frequency or ''}
                for m in self.medication_line_ids]

    def _allergies_payload(self):
        return [{'allergen': a.allergen, 'reaction': a.reaction or '', 'criticality': a.criticality}
                for a in self.patient_id.allergy_ids]

    def _supplies_payload(self):
        return [{'item_name': s.item_name, 'surgical_qty_used': s.surgical_qty_used,
                 'billed_qty': s.billed_qty} for s in self.supply_line_ids]

    def _post_sidecar(self, path, payload, timeout):
        try:
            resp = requests.post(self._sidecar_url() + path, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            raise UserError("Cannot reach the ClearPath sidecar (%s): %s" % (self._sidecar_url(), exc))
        if resp.status_code == 409:
            raise UserError("Sidecar rejected a stale revision: %s" % resp.json().get('detail', ''))
        if not resp.ok:
            raise UserError("Sidecar error %s: %s" % (resp.status_code, resp.text[:300]))
        return resp.json()

    # ------------------------------------------------------------------ delete
    def unlink(self):
        is_admin = self.env.is_superuser() or self.env.user._is_admin()
        if not is_admin and any(rec.state == 'released' for rec in self):
            raise UserError(
                "Released packets are part of the audit trail; only an administrator can delete them.")
        ids = self.ids
        base_url = self._sidecar_url()
        res = super().unlink()
        for discharge_id in ids:  # best effort: forget the sidecar's revision state
            try:
                requests.delete('%s/api/v1/discharge/%d/state' % (base_url, discharge_id), timeout=5)
            except requests.RequestException:
                _logger.warning("Could not clear sidecar state for discharge %s", discharge_id)
        return res

    # ------------------------------------------------------- Stale-Write Guard
    def write(self, vals):
        if CLINICAL_FIELDS & vals.keys() and not self.env.context.get('clearpath_skip_guard'):
            self._assert_not_released()
            locked = self.filtered(lambda r: r.state != 'draft')
            res = super().write(vals)
            locked._bump_revision("clinical note edited")
            return res
        return super().write(vals)

    def _bump_if_locked(self, reason):
        self.filtered(lambda r: r.state != 'draft')._bump_revision(reason)

    def _bump_revision(self, reason):
        """Edit after lock: new revision, old checks invalid, release gate shut."""
        for rec in self:
            old_rev = rec.revision_id
            old_snapshot = rec.sidecar_snapshot_id
            rec.medication_line_ids.with_context(clearpath_skip_guard=True).write(
                {'is_flagged': False, 'conflict_reason': False})
            rec.with_context(clearpath_skip_guard=True).write({
                # Old Rev checklist is invalid: drop its snapshot and findings.
                'sidecar_snapshot_id': False, 'audit_snapshot_json': False,
                'pharmacy_summary': False, 'billing_summary': False, 'locked_on': False,
                'revision_id': old_rev + 1,
                'state': 'draft',
                'pharmacy_status': 'stale',
                'billing_status': 'stale',
                'coordinator_gate_locked': True,
                'pharmacist_id': False, 'pharmacy_approved_on': False,
                'billing_officer_id': False, 'billing_approved_on': False,
                'insurance_status': 'stale', 'tpa_claim_document': False,
                'icd10_cpt_codes': False, 'insurance_hold_reasons': False,
                'insurance_approver_id': False, 'insurance_approved_on': False,
                'copay_sms_sent': False,  # the patient is told again once the new revision clears
            })
            rec.with_context(clearpath_skip_guard=True).write({
                'revision_hash': rec._compute_revision_hash(),
            })
            rec.message_post(body=(
                "<b>Stale-Write Guard:</b> %s after lock. Revision <b>Rev %d</b>%s is invalid; "
                "now <b>Rev %d</b>. Pharmacy, Billing and Insurance checks reset to STALE and the "
                "Coordinator release gate is locked. Re-lock and re-verify."
            ) % (reason, old_rev, " (snapshot %s)" % old_snapshot if old_snapshot else "", old_rev + 1))

    # ------------------------------------------------------------ demo helper
    def _seed_demo_content(self):
        """(Re)build the demo note, medications and supplies from the constants above. Idempotent: the old
        lines are removed first, so running it twice never leaves duplicates."""
        for rec in self:
            ctx = rec.sudo().with_context(clearpath_skip_guard=True)
            ctx.medication_line_ids.unlink()
            ctx.supply_line_ids.unlink()
            ctx.env['clearpath.medication.line'].create(
                [dict(m, discharge_id=rec.id) for m in DEMO_MEDICATIONS])
            ctx.env['clearpath.supply.line'].create(
                [dict(s, discharge_id=rec.id) for s in DEMO_SUPPLIES])
            ctx.write({'raw_handoff_note': DEMO_NOTE_TEMPLATE.format(
                bed=rec.patient_id.bed_number or '?', name=rec.patient_id.name or 'the patient')})

    def action_reset_demo(self):
        """Demo: return the PT-402 record to a clean Rev 1 draft and clear the sidecar's memory of it."""
        self.ensure_one()
        self._single_flight(7424)  # a double click must not run two resets at once
        if not (self.env.is_superuser() or self.env.user._is_admin()):
            raise UserError("Only an administrator can reset the demo.")
        if self.patient_id.patient_code != 'PT-402':
            raise UserError("Demo reset is only available for the PT-402 demo patient.")
        try:
            requests.delete(self._sidecar_url() + '/api/v1/discharge/%d/state' % self.id, timeout=10)
        except requests.RequestException:
            raise UserError("Cannot reach the sidecar to clear its state; start it and try again.")
        ctx = self.sudo().with_context(clearpath_skip_guard=True)  # admin-only action
        if ctx.invoice_id.state == 'draft':
            ctx.invoice_id.unlink()  # a posted invoice stays in Invoicing; only the link is dropped
        ctx.write({'invoice_id': False})
        ctx._seed_demo_content()
        ctx.write({
            'state': 'draft', 'revision_id': 1, 'revision_hash': False, 'sidecar_snapshot_id': False,
            'ai_draft_summary': False, 'review_prompts': False,
            'token_savings_summary': False, 'pharmacy_status': 'pending', 'billing_status': 'pending',
            'pharmacy_summary': False, 'billing_summary': False, 'coordinator_gate_locked': True,
            'pharmacist_id': False, 'pharmacy_approved_on': False, 'billing_officer_id': False,
            'billing_approved_on': False, 'coordinator_id': False, 'released_on': False,
            'locked_on': False, 'audit_snapshot_json': False,
            'insurance_status': 'pending', 'tpa_claim_document': False, 'icd10_cpt_codes': False,
            'insurance_hold_reasons': False, 'insurance_approver_id': False,
            'insurance_approved_on': False, 'voice_note_audio': False, 'voice_note_filename': False,
            'nurse_checklist_image': False,
            'patient_payment_status': 'unpaid', 'patient_paid_on': False, 'copay_sms_sent': False,
            'last_sms_status': False,
        })
        self.message_post(body="Demo reset to a clean Rev 1 draft.")
        return self._soft_reload()

    def action_simulate_late_doctor_edit(self):
        """Demo: doctor swaps the antibiotic after lock -> Stale-Write Guard fires (Rev N -> N+1)."""
        self.ensure_one()
        self._require_group(GROUP_DOCTOR, "Doctor")
        if self.state in ('draft', 'released'):
            raise UserError("Lock a revision first; the late edit only applies to a locked, unreleased discharge.")
        med = self.medication_line_ids.filtered(
            lambda m: 'amoxicillin' in (m.drug_name or '').lower())[:1]
        if not med:
            raise UserError("No Amoxicillin line to replace (already changed?).")
        med.write({'drug_name': 'Azithromycin', 'dosage': '500mg'})  # bumps the revision
        self.with_context(clearpath_skip_guard=True).write({
            'raw_handoff_note': re.sub(r'amoxicillin', 'Azithromycin', self.raw_handoff_note or '', flags=re.I),
        })
        self.message_post(body=(
            "<b>Late doctor edit (demo):</b> Amoxicillin 500mg replaced by Azithromycin 500mg to "
            "resolve the Penicillin allergy conflict. Now Rev %d."
        ) % self.revision_id)
        try:  # old AI draft still describes Amoxicillin: regenerate it for the new revision
            self.action_generate_ai_draft()
        except UserError as exc:
            self.message_post(body="AI draft not regenerated (%s). Regenerate it before re-locking." % exc.args[0])
        return self._alerts_action_or_reload()

    # ------------------------------------------------------------ Stage 1: draft
    def action_generate_ai_draft(self):
        self.ensure_one()
        self._require_group(GROUP_DOCTOR, "Doctor")
        if self.state != 'draft':
            raise UserError("The AI draft can only be generated while the discharge is in Draft.")
        if not self.raw_handoff_note:
            raise UserError("Enter the handoff note first.")
        self.env.cr.execute("SELECT pg_try_advisory_xact_lock(%s, %s)", (7421, self.id))
        if not self.env.cr.fetchone()[0]:
            raise UserError("An AI draft is already being generated for this discharge. Please wait.")
        # Flat clinical payload + raw ORM dumps as extras (sidecar measures, then discards them).
        payload = {
            'discharge_id': self.id,
            'patient_code': self.patient_id.patient_code,
            'patient_name': self.patient_id.name,
            'bed_number': self.patient_id.bed_number or '',
            'raw_handoff_note': self.raw_handoff_note,
            'medications': self._medications_payload(),
            'allergies': self._allergies_payload(),
            'supplies': self._supplies_payload(),
            'orm_discharge': self.read()[0],
            'orm_medication_lines': self.medication_line_ids.read(),
            'orm_supply_lines': self.supply_line_ids.read(),
            'orm_allergies': self.patient_id.allergy_ids.read(),
            'past_medical_history': self._history_payload(),
            'orm_history': self.patient_id.history_ids.read(),
        }
        payload = json.loads(json.dumps(payload, default=str))
        data = self._post_sidecar('/api/v1/draft', payload, timeout=60)
        stats = data['token_stats']
        self.write({
            'ai_draft_summary': clean_summary_text(data['ai_draft_summary']),
            'review_prompts': "\n".join("- " + p for p in data['review_prompts']),
            'token_savings_summary': (
                "Raw Odoo payload: {before_tokens:,} tokens -> Pruned payload: {after_tokens:,} tokens "
                "({saved_pct:.1f}% saved)"
            ).format(**stats),
        })
        self.message_post(body="AI draft generated (%s%s)." % (
            data['model_used'], ", mock" if data['used_mock'] else ""))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': "AI draft ready",
                'message': "Review it on the Clinical Draft tab, then click Lock Revision & Run Checks.",
                'type': 'success',
                'sticky': False,
                'next': self._soft_reload(),
            },
        }

    # ------------------------------------------- Stage 2: HITL gate - doctor lock
    def action_lock_revision_and_verify(self):
        self.ensure_one()
        self._require_group(GROUP_DOCTOR, "Doctor")
        if self.state != 'draft':
            raise UserError("Only a Draft can be locked.")
        if not self.ai_draft_summary:
            raise UserError("Generate and review the AI draft before locking.")
        self.with_context(clearpath_skip_guard=True).write({
            'state': 'locked',
            'locked_on': fields.Datetime.now(),
            'revision_hash': self._compute_revision_hash(),
            'pharmacy_status': 'pending',
            'billing_status': 'pending',
            'coordinator_gate_locked': True,
        })
        self._ensure_tracking_token()
        payload = self._verify_payload()
        payload.update({'patient_phone': self.patient_id.phone or '', 'tracking_token': self.tracking_token,
                        'copay_amount': self.copay_amount})
        snapshot = self._post_sidecar('/api/v1/verify', payload, timeout=30)
        sms = snapshot.pop('sms', None)  # kept out of the stored audit packet (phone number + tracker link)
        self.message_post(body="Revision <b>Rev %d</b> locked (%s). Verification dispatched." % (
            self.revision_id, self.revision_hash[:8]))
        self.apply_mcp_findings(
            revision_id=snapshot['revision_id'],
            revision_hash=snapshot['revision_hash'],
            pharmacy_result=snapshot['pharmacy'],
            billing_result=snapshot['billing'],
            insurance_result=snapshot.get('insurance_output'),
            audit_packet=snapshot,
        )
        self._log_sms(sms)
        return self._alerts_action_or_reload()

    # ---------------------------------------------- Stage 3: apply sidecar results
    def apply_mcp_findings(self, revision_id, revision_hash, pharmacy_result, billing_result,
                           audit_packet, insurance_result=None):
        """RPC target for the sidecar. Rejects findings that belong to an older revision."""
        self.ensure_one()
        if revision_id != self.revision_id or revision_hash != self.revision_hash:
            self.message_post(body=(
                "<b>Stale-Write Guard:</b> rejected findings for Rev %s; current revision is Rev %s."
            ) % (revision_id, self.revision_id))
            return {'accepted': False, 'reason': 'stale', 'current_revision': self.revision_id}

        ctx = self.with_context(clearpath_skip_guard=True)
        flagged_drugs = {c['drug_name']: c['reason'] for c in pharmacy_result.get('conflicts', [])}
        for med in ctx.medication_line_ids:
            reason = flagged_drugs.get(med.drug_name)
            med.write({'is_flagged': bool(reason), 'conflict_reason': reason or False})

        pharm_flag = pharmacy_result.get('status') == 'flagged'
        bill_flag = billing_result.get('status') == 'flagged'
        ins = insurance_result or {}
        ins_flag = bool(ins) and ins.get('claim_status') != 'ready_for_review'
        codes = ' / '.join(x for x in (ins.get('icd10_code'), ins.get('cpt_code')) if x)
        ctx.write({
            'pharmacy_status': 'flagged' if pharm_flag else 'pending',
            'billing_status': 'flagged' if bill_flag else 'pending',
            'insurance_status': 'flagged' if ins_flag else 'pending',
            'pharmacy_summary': pharmacy_result.get('summary'),
            'billing_summary': billing_result.get('summary'),
            'tpa_claim_document': ins.get('filled_claim_markdown') or False,
            'icd10_cpt_codes': codes or False,
            'insurance_hold_reasons': "\n".join("- " + h for h in ins.get('hold_reasons', [])) or False,
            'sidecar_snapshot_id': audit_packet.get('snapshot_id'),
            'audit_snapshot_json': json.dumps(audit_packet, indent=2),
            'state': 'flagged' if (pharm_flag or bill_flag or ins_flag) else 'verifying',
        })
        self.message_post(body=(
            "Rev %d verified (%s). Pharmacy: <b>%s</b>. Billing: <b>%s</b>. Insurance claim: <b>%s</b>."
        ) % (self.revision_id, audit_packet.get('snapshot_id'),
             pharmacy_result.get('summary'), billing_result.get('summary'),
             ('HOLD' if ins_flag else 'ready for review') if ins else 'not generated'))
        return {'accepted': True, 'state': self.state}

    # ---------------------------------------- Stage 4: HITL gate #1 - dept sign-off
    def _assert_signable(self):
        if self.state not in ('flagged', 'verifying'):
            raise UserError("Sign-off is only possible after verification of the current revision.")

    def _update_gate(self):
        """Open HITL gate #2 only when Pharmacy, Billing AND Insurance approved the SAME revision."""
        for rec in self:
            if (rec.pharmacy_status == 'approved' and rec.billing_status == 'approved'
                    and rec.insurance_status == 'approved'):
                rec.write({'state': 'cleared', 'coordinator_gate_locked': False})
                rec.message_post(body="All three sign-offs recorded on Rev %d. Coordinator gate <b>unlocked</b>." % rec.revision_id)
                if rec.patient_payment_status == 'unpaid' and not rec.copay_sms_sent:
                    rec._notify_patient_sms('ready_for_payment')
                    rec.copay_sms_sent = True  # one text per clearing, even if delivery failed

    # ------------------------------------------- Phase 5: multimodal bedside inputs
    def _b64(self, value):
        """Odoo 20 returns a BinaryValue for Binary fields; the sidecar wants a base64 string."""
        if not value:
            return ''
        if hasattr(value, 'to_base64'):
            value = value.to_base64()
        return value.decode() if isinstance(value, bytes) else value

    def action_transcribe_voice_and_draft(self):
        """Doctor voice note -> transcript -> note + AI draft (+ proposed meds if none entered yet)."""
        self.ensure_one()
        self._require_group(GROUP_DOCTOR, "Doctor")
        if self.state != 'draft':
            raise UserError("Voice dictation is only available while the discharge is in Draft.")
        if not self.voice_note_audio:
            raise UserError("Upload a voice note first.")
        self._single_flight(7422)
        data = self._post_sidecar('/api/v1/transcribe-and-draft', {
            'discharge_id': self.id,
            'audio_base64': self._b64(self.voice_note_audio),
            'filename': self.voice_note_filename or 'voice_note.wav',
            'patient_code': self.patient_id.patient_code,
            'patient_name': self.patient_id.name,
            'bed_number': self.patient_id.bed_number or '',
            'medications': self._medications_payload(),
            'allergies': self._allergies_payload(),
            'supplies': self._supplies_payload(),
            'past_medical_history': self._history_payload(),
        }, timeout=120)
        stats = data['token_stats']
        self.write({
            'raw_handoff_note': data['transcribed_text'],
            'ai_draft_summary': clean_summary_text(data['ai_draft_summary']),
            'review_prompts': "\n".join("- " + p for p in data['review_prompts']),
            'token_savings_summary': (
                "Raw Odoo payload: {before_tokens:,} tokens -> Pruned payload: {after_tokens:,} tokens "
                "({saved_pct:.1f}% saved)"
            ).format(**stats),
        })
        added = 0
        if not self.medication_line_ids and data.get('proposed_medications'):
            lines = self.env['clearpath.medication.line'].with_context(clearpath_skip_guard=True).create([
                {'discharge_id': self.id, 'drug_name': m['drug_name'], 'dosage': m.get('dosage') or '',
                 'frequency': m.get('frequency') or ''} for m in data['proposed_medications']])
            added = len(lines)
        self.message_post(body="Voice note transcribed and draft generated (%s%s). %d medication(s) proposed from the transcript." % (
            data['model_used'], ", mock" if data['used_mock'] else "", added))
        return self._toast("Voice note transcribed",
                           "Review the note, draft and medications, then click Lock Revision & Run Checks.")

    def action_scan_nurse_checklist(self):
        """Nurse tally-sheet photo -> surgical_qty_used on the supply lines (billed qty is kept)."""
        self.ensure_one()
        self._require_any_group([GROUP_NURSE, GROUP_DOCTOR], "a Nurse or Doctor")
        if self.state != 'draft':
            raise UserError("The supply sheet can only be scanned while the discharge is in Draft.")
        if not self.nurse_checklist_image:
            raise UserError("Upload the nurse supply sheet photo first.")
        self._single_flight(7423)
        data = self._post_sidecar('/api/v1/scan-supply-sheet', {
            'image_base64': self._b64(self.nurse_checklist_image), 'discharge_id': self.id,
        }, timeout=90)

        def norm(name):
            return (name or '').strip().lower().rstrip('s')

        existing = {norm(l.item_name): l for l in self.supply_line_ids}
        updated = created = 0
        for item in data['items']:
            line = existing.get(norm(item['item_name']))
            if line:
                line.write({'surgical_qty_used': item['surgical_qty_used']})
                updated += 1
            else:
                self.env['clearpath.supply.line'].create({
                    'discharge_id': self.id, 'item_name': item['item_name'],
                    'surgical_qty_used': item['surgical_qty_used'], 'billed_qty': 0})
                created += 1
        self.message_post(body="Nurse supply sheet scanned: %d line(s) updated, %d created." % (updated, created))
        return self._toast("Supply sheet scanned",
                           "%d updated, %d new. Open the Billing button to review." % (updated, created))

    # --------------------------------------- HITL gate #1 - insurance (admin) sign-off
    def _insurance_blockers(self):
        self.ensure_one()
        out = []
        if any(self.medication_line_ids.mapped('is_flagged')):
            out.append("unresolved pharmacy allergy conflict")
        open_items = self.supply_line_ids.filtered(lambda s: s.discrepancy_qty > 0)
        if open_items:
            out.append("unreconciled supplies (%s)" % ", ".join(
                "+%d %s" % (s.discrepancy_qty, s.item_name) for s in open_items))
        if not self.icd10_cpt_codes:
            out.append("procedure not mapped to ICD-10 / CPT")
        if not (self.patient_id.insurance_provider and self.patient_id.policy_number):
            out.append("TPA provider / policy number missing on the patient")
        return out

    # ---------------------------------- Phase 6: smart buttons -> department pop-ups
    def _open_wizard(self, xmlid, size='large'):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(xmlid)
        action['context'] = {'default_discharge_id': self.id, 'dialog_size': size}
        return action

    def action_open_pharmacy_wizard(self):
        return self._open_wizard('clearpath_discharge.pharmacy_approval_wizard_action')

    def action_open_billing_wizard(self):
        return self._open_wizard('clearpath_discharge.billing_approval_wizard_action')

    def action_open_insurance_wizard(self):
        return self._open_wizard('clearpath_discharge.insurance_approval_wizard_action')

    # ------------------------------------------------ Phase 7: final invoice (Odoo Invoicing)
    def action_generate_invoice(self):
        """Turn the reconciled supply log into a DRAFT customer invoice for the patient's Contact."""
        self.ensure_one()
        self._require_any_group([GROUP_BILLING, GROUP_ADMIN], "a Billing Officer or Hospital Admin")
        if self.invoice_id:
            raise UserError("An invoice already exists for this discharge (%s)." % self.invoice_label)
        if not (self.billing_status == 'approved' or self.state in ('cleared', 'released')):
            raise UserError("Billing must be approved before the final invoice can be generated.")
        lines = self.supply_line_ids.filtered(lambda s: s.billed_qty > 0)
        if not lines:
            raise UserError("There are no billed supply items to invoice.")
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.patient_id.partner_id.id,
            'invoice_origin': self.name,
            'ref': "%s Rev %d" % (self.patient_id.patient_code, self.revision_id),
            'invoice_line_ids': [(0, 0, {
                'name': line.item_name,
                'quantity': line.billed_qty,  # the reconciled (billed) quantity
                'price_unit': line.price_unit,
            }) for line in lines],
        })
        self.invoice_id = move
        self.message_post(body="Draft invoice created from the reconciled supply log (%d line(s))." % len(lines))
        return self._toast("Invoice created",
                           "A draft invoice for %s is ready in Invoicing." % self.patient_id.name)

    def action_view_invoice(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'account.move',
                'res_id': self.invoice_id.id, 'view_mode': 'form', 'target': 'current'}

    # ------------------------------------------ Phase 12: patient SMS + live tracker
    @api.model
    def _new_tracking_token(self):
        """Unguessable URL token (128 bits): it is the only credential on the public tracker page."""
        return secrets.token_urlsafe(16)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('tracking_token'):
                vals['tracking_token'] = self._new_tracking_token()
        return super().create(vals_list)

    def _ensure_tracking_token(self):
        """Records created before Phase 12 have no token yet."""
        for rec in self.filtered(lambda r: not r.tracking_token):
            rec.sudo().with_context(clearpath_skip_guard=True).write(
                {'tracking_token': self._new_tracking_token()})

    @api.depends('tracking_token')
    def _compute_patient_tracker_url(self):
        base = (self.env['ir.config_parameter'].sudo().get_str('web.base.url', '') or '').rstrip('/')
        for rec in self:
            rec.patient_tracker_url = (
                '%s/discharge/track/%s' % (base, rec.tracking_token) if rec.tracking_token else False)

    def action_open_patient_tracker(self):
        """Open the patient's phone view in a new tab (for showing it side by side in a demo)."""
        self.ensure_one()
        self._ensure_tracking_token()
        return {'type': 'ir.actions.act_url', 'target': 'new',
                'url': '/discharge/track/%s' % self.tracking_token}

    @staticmethod
    def _mask_phone(number):
        number = number or ''
        return number if len(number) < 8 else number[:5] + '*' * (len(number) - 8) + number[-3:]

    def _log_sms(self, sms):
        """Record what was (or would have been) texted to the patient, with a link to the tracker."""
        self.ensure_one()
        if not sms:
            return
        how = {'twilio': "sent via Twilio",
               'mock_logged': "mock mode, not sent to a phone",
               'twilio_failed_logged': "Twilio delivery failed, not sent"}.get(sms.get('mode'), sms.get('mode'))
        event = {'started': "Tracker link", 'ready_for_payment': "Co-pay ready",
                 'released': "Exit pass ready"}.get(sms.get('stage_event'), "Patient SMS")
        to = self._mask_phone(sms.get('to_number'))
        self.with_context(clearpath_skip_guard=True).write(
            {'last_sms_status': "%s: %s (%s)" % (event, how, to)})
        self.message_post(body=Markup(
            "Patient SMS <b>%s</b> (%s) to %s:<br/>%s<br/>"
            "<a href=\"%s\" target=\"_blank\">Open the live patient tracker</a>"
        ) % (event, how, to, sms.get('message_preview') or '', '/discharge/track/%s' % self.tracking_token))

    def _notify_patient_sms(self, stage_event):
        """Ask the sidecar to text the patient. A failed SMS must never block the clinical workflow."""
        self.ensure_one()
        self._ensure_tracking_token()
        try:
            sms = self._post_sidecar('/api/v1/notify-sms', {
                'tracking_token': self.tracking_token,
                'to_number': self.patient_id.phone or '',
                'stage_event': stage_event,
                'copay_amount': self.copay_amount,
            }, timeout=15)
        except UserError as exc:
            _logger.warning("Patient SMS (%s) not sent for discharge %s: %s", stage_event, self.id, exc)
            self.with_context(clearpath_skip_guard=True).write(
                {'last_sms_status': "SMS not sent: notification service unavailable"})
            return False
        self._log_sms(sms)
        return True

    def _copay_ready(self):
        self.ensure_one()
        return (self.state in ('cleared', 'released') and self.pharmacy_status == 'approved'
                and self.billing_status == 'approved' and self.insurance_status == 'approved')

    def action_patient_pay_copay(self):
        """Called (sudo) by the public tracker page. SIMULATED payment: marks the co-pay as paid."""
        self.ensure_one()
        if self.patient_payment_status == 'paid':
            return False
        if not self._copay_ready():
            raise UserError("The co-pay opens once Pharmacy, Billing and Insurance have all approved.")
        self.with_context(clearpath_skip_guard=True).write(
            {'patient_payment_status': 'paid', 'patient_paid_on': fields.Datetime.now()})
        self.message_post(
            body="\U0001F4B3 Patient completed co-pay of AED %.2f via Mobile Tracker." % self.copay_amount,
            author_id=self.env.ref('base.partner_root').id)
        return True

    def _tracker_values(self):
        """Plain values for the public patient page. Progress and patient-friendly wording ONLY:
        no drug names, allergies, billing lines or claim text ever leave through here."""
        self.ensure_one()
        locked = self.state != 'draft'
        revised = self.revision_id > 1

        def step(emoji, title, state, note):
            tag = {'done': "Done", 'active': "In progress", 'attention': "Under review",
                   'waiting': "Waiting"}[state]
            return {'emoji': emoji, 'title': title, 'state': state, 'note': note, 'tag': tag}

        def dept(emoji, title, status, review_note, done_note):
            if not locked:
                return step(emoji, title, 'waiting',
                            "Will be re-checked after your doctor's update" if revised
                            else "Starts after your doctor's review")
            if status == 'approved':
                return step(emoji, title, 'done', done_note)
            if status == 'flagged':
                return step(emoji, title, 'attention', review_note)
            return step(emoji, title, 'active', "Being checked now")

        steps = [
            step("\U0001F468‍⚕️", "Doctor Handoff & AI Summary", 'done',
                 "Your doctor completed your discharge review") if locked else
            step("\U0001F468‍⚕️", "Doctor Handoff & AI Summary", 'active',
                 "Your doctor is updating your discharge plan" if revised
                 else "Your doctor is preparing your discharge summary"),
            dept("\U0001F48A", "Pharmacy Safety Check", self.pharmacy_status,
                 "Doctor reviewing medication adjustment", "Your medications are confirmed"),
            dept("\U0001F9FE", "Surgical Billing Audit", self.billing_status,
                 "Billing is finalising your hospital bill", "Your bill is finalised"),
            dept("\U0001F6E1️", "TPA Insurance Pre-Auth", self.insurance_status,
                 "Your insurance paperwork is being completed", "Insurance approved"),
        ]

        paid = self.patient_payment_status == 'paid'
        ready = self._copay_ready()
        if paid and self.state == 'released':
            copay_mode = 'pass'
            steps.append(step("\U0001F4B3", "Co-Pay & Exit Pass", 'done', "Paid. Your exit pass is ready"))
        elif paid and ready:
            copay_mode = 'paid_wait'
            steps.append(step("\U0001F4B3", "Co-Pay & Exit Pass", 'active', "Paid. Waiting for final release"))
        elif ready:
            copay_mode = 'pay'
            steps.append(step("\U0001F4B3", "Co-Pay & Exit Pass", 'active', "Your co-pay is ready to pay"))
        else:
            copay_mode = 'waiting'
            steps.append(step("\U0001F4B3", "Co-Pay & Exit Pass", 'waiting',
                              "Co-pay already paid. Opens again after the checks" if paid
                              else "Opens when all three checks are done"))

        states = [s['state'] for s in steps]
        done_count = states.count('done')
        if copay_mode == 'pass':
            badge = ('done', "Cleared to go home")
        elif copay_mode == 'pay':
            badge = ('pay', "Payment needed")
        elif 'attention' in states:
            badge = ('attention', "Being reviewed")
        elif not locked:
            badge = ('progress', "With your doctor")
        else:
            badge = ('progress', "In progress")

        pass_code = "EXIT-%s-R%d-%s" % (
            self.patient_id.patient_code or 'PT', self.revision_id, (self.revision_hash or '')[:6].upper())
        return {
            'token': self.tracking_token,
            'patient_name': self.patient_id.name or "Patient",
            'bed': self.patient_id.bed_number or '',
            'revision': self.revision_id,
            'steps': steps,
            'done_count': done_count,
            'progress': max(done_count * 20, 6),
            'badge_kind': badge[0],
            'badge_text': badge[1],
            'copay_mode': copay_mode,
            'copay_amount': "%.2f" % self.copay_amount,
            'pass_code': pass_code,
            'qr_url': '/report/barcode/?' + urlencode(
                {'barcode_type': 'QR', 'value': pass_code, 'width': 168, 'height': 168}),
            # changes whenever anything visible changes: the page swaps its content only then
            'sig': hashlib.sha256(repr(
                (states, copay_mode, self.revision_id, badge, "%.2f" % self.copay_amount)).encode()
            ).hexdigest()[:16],
            'notice': '',
        }

    # ----------------------------------------- HITL gate #2 - coordinator release
    def action_coordinator_release(self):
        self.ensure_one()
        self._require_group(GROUP_COORDINATOR, "Discharge Coordinator")
        if (self.state != 'cleared' or self.coordinator_gate_locked
                or self.pharmacy_status != 'approved' or self.billing_status != 'approved'
                or self.insurance_status != 'approved'):
            raise UserError(
                "Release gate is locked: Pharmacy, Billing and Insurance must all approve the current revision.")
        sidecar = {}
        if self.audit_snapshot_json:
            try:
                sidecar = json.loads(self.audit_snapshot_json)
            except ValueError:
                sidecar = {}
        released_on = fields.Datetime.now()
        packet = {
            'patient_code': self.patient_id.patient_code,
            'bed': self.patient_id.bed_number,
            'revision_id': self.revision_id,
            'revision_hash': self.revision_hash,
            'sidecar_snapshot_id': self.sidecar_snapshot_id,
            'discharge_summary': self.ai_draft_summary,
            'medications': self._medications_payload(),
            'supplies': self._supplies_payload(),
            'pharmacy': {'status': self.pharmacy_status, 'by': self.pharmacist_id.name,
                         'on': str(self.pharmacy_approved_on)},
            'billing': {'status': self.billing_status, 'by': self.billing_officer_id.name,
                        'on': str(self.billing_approved_on)},
            'insurance': {'status': self.insurance_status, 'by': self.insurance_approver_id.name,
                          'on': str(self.insurance_approved_on), 'codes': self.icd10_cpt_codes},
            'released_by': self.env.user.name,
            'released_on': str(released_on),
            'verification_findings': sidecar,
        }
        self.write({
            'state': 'released',
            'coordinator_id': self.env.user.id,
            'released_on': released_on,
            'audit_snapshot_json': json.dumps(packet, indent=2, default=str),
        })
        self.message_post(body="Discharge packet <b>released</b> and locked by %s (Rev %d, %s)." % (
            self.env.user.name, self.revision_id, self.revision_hash[:8]))
        self._notify_patient_sms('released')
        return self._soft_reload()
