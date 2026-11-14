from markupsafe import Markup

from odoo import api, fields, models

from ..models.clearpath_discharge import GROUP_PHARMACIST


class ClearpathPharmacyApprovalWizard(models.TransientModel):
    _name = 'clearpath.pharmacy.approval.wizard'
    _description = 'Pharmacy Safety Review'
    _inherit = ['clearpath.approval.mixin']

    _group_xmlid = GROUP_PHARMACIST
    _group_label = "Pharmacist"

    status = fields.Selection(related='discharge_id.pharmacy_status')
    pharmacy_summary = fields.Text(related='discharge_id.pharmacy_summary')
    pharmacist_id = fields.Many2one(related='discharge_id.pharmacist_id')
    pharmacy_approved_on = fields.Datetime(related='discharge_id.pharmacy_approved_on')
    ai_draft_summary_html = fields.Html(related='discharge_id.ai_draft_summary_html', sanitize=False)
    medication_line_ids = fields.One2many(related='discharge_id.medication_line_ids')
    allergy_ids = fields.One2many(related='discharge_id.allergy_ids')
    conflict_html = fields.Html(compute='_compute_conflict_html', sanitize=False)

    @api.depends('discharge_id.medication_line_ids.is_flagged',
                 'discharge_id.medication_line_ids.conflict_reason')
    def _compute_conflict_html(self):
        for wiz in self:
            flagged = wiz.discharge_id.medication_line_ids.filtered('is_flagged')
            rows = Markup('').join(
                Markup('<li><b>%s</b>: %s</li>') % (m.drug_name, m.conflict_reason or "allergy conflict")
                for m in flagged)
            wiz.conflict_html = Markup('<ul class="mb-0 ps-3">%s</ul>') % rows if flagged else False

    @api.depends('discharge_id.state', 'discharge_id.pharmacy_status',
                 'discharge_id.medication_line_ids.is_flagged')
    def _compute_approval_state(self):
        self._fill_approval_state()

    def _dept_blockers(self):
        self.ensure_one()
        if self.status == 'stale':
            return ["the pharmacy check is stale; re-verify the current revision"]
        if self.status != 'pending' or any(self.discharge_id.medication_line_ids.mapped('is_flagged')):
            return ["unresolved allergy conflict: the prescriber must change the medication "
                    "(this creates a new revision) before sign-off"]
        return []

    def _write_approval(self):
        d = self.discharge_id
        d.write({'pharmacy_status': 'approved', 'pharmacist_id': self.env.user.id,
                 'pharmacy_approved_on': fields.Datetime.now()})
        d.message_post(body="Pharmacist <b>%s</b> approved Rev %d." % (self.env.user.name, d.revision_id))
