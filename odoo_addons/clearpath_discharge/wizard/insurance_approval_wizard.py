from markupsafe import Markup

from odoo import api, fields, models

from ..models.clearpath_discharge import GROUP_ADMIN


class ClearpathInsuranceApprovalWizard(models.TransientModel):
    _name = 'clearpath.insurance.approval.wizard'
    _description = 'TPA Insurance Claim Review'
    _inherit = ['clearpath.approval.mixin']

    _group_xmlid = GROUP_ADMIN
    _group_label = "Hospital Admin / Insurance Officer"

    status = fields.Selection(related='discharge_id.insurance_status')
    icd10_cpt_codes = fields.Char(related='discharge_id.icd10_cpt_codes')
    tpa_provider = fields.Char(related='discharge_id.tpa_provider')
    policy_number = fields.Char(related='discharge_id.policy_number')
    insurance_approver_id = fields.Many2one(related='discharge_id.insurance_approver_id')
    insurance_approved_on = fields.Datetime(related='discharge_id.insurance_approved_on')
    tpa_claim_html = fields.Html(related='discharge_id.tpa_claim_html', sanitize=False)
    hold_html = fields.Html(compute='_compute_hold_html', sanitize=False)

    @api.depends('discharge_id.insurance_hold_reasons')
    def _compute_hold_html(self):
        for wiz in self:
            lines = [l.lstrip('- ').strip() for l in (wiz.discharge_id.insurance_hold_reasons or '').splitlines()
                     if l.strip()]
            items = Markup('').join(Markup('<li>%s</li>') % l for l in lines)
            wiz.hold_html = Markup('<ul class="mb-0 ps-3">%s</ul>') % items if lines else False

    @api.depends('discharge_id.state', 'discharge_id.insurance_status', 'discharge_id.icd10_cpt_codes',
                 'discharge_id.medication_line_ids.is_flagged', 'discharge_id.supply_line_ids.discrepancy_qty',
                 'discharge_id.patient_id.insurance_provider', 'discharge_id.patient_id.policy_number')
    def _compute_approval_state(self):
        self._fill_approval_state()

    def _dept_blockers(self):
        self.ensure_one()
        if self.status == 'stale':
            return ["the insurance claim is stale; re-verify the current revision"]
        return self.discharge_id._insurance_blockers()

    def _write_approval(self):
        d = self.discharge_id
        d.write({'insurance_status': 'approved', 'insurance_approver_id': self.env.user.id,
                 'insurance_approved_on': fields.Datetime.now(),
                 'insurance_hold_reasons': False})  # resolved: do not show stale holds on the packet
        d.message_post(body="Insurance officer <b>%s</b> approved the TPA claim for Rev %d (%s)." % (
            self.env.user.name, d.revision_id, d.icd10_cpt_codes))
