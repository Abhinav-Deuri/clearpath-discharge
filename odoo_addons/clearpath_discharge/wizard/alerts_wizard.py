from markupsafe import Markup

from odoo import api, fields, models


class ClearpathAlertsWizard(models.TransientModel):
    """Pop-up that lists everything needing attention (replaces the inline banners)."""
    _name = 'clearpath.alerts.wizard'
    _description = 'Review Alerts'

    discharge_id = fields.Many2one('clearpath.discharge', required=True, readonly=True, ondelete='cascade')
    patient_name = fields.Char(related='discharge_id.patient_id.name')
    patient_code = fields.Char(related='discharge_id.patient_code')
    bed_number = fields.Char(related='discharge_id.bed_number')
    revision_id = fields.Integer(related='discharge_id.revision_id')
    pharmacy_status = fields.Selection(related='discharge_id.pharmacy_status')
    billing_status = fields.Selection(related='discharge_id.billing_status')
    insurance_status = fields.Selection(related='discharge_id.insurance_status')
    alerts_html = fields.Html(compute='_compute_alerts_html', sanitize=False)

    @api.depends('discharge_id')
    def _compute_alerts_html(self):
        for wiz in self:
            cards = [
                Markup('<div class="alert alert-%s o_cp_alert d-flex align-items-start" role="alert">'
                       '<i class="oi oi-2x %s me-3" data-icon="%s"/><div><div class="o_cp_alert_title">%s</div>%s'
                       '</div></div>')
                % (level, icon[1], icon[0], title, text)
                for level, icon, title, text in wiz.discharge_id._alert_items()
            ]
            wiz.alerts_html = Markup('').join(cards) if cards else Markup(
                '<div class="alert alert-success o_cp_alert" role="alert">'
                '<div class="o_cp_alert_title"><i class="oi" data-icon="check_circle"/> Nothing needs attention</div>'
                '</div>')

    def action_open_pharmacy(self):
        return self.discharge_id.action_open_pharmacy_wizard()

    def action_open_billing(self):
        return self.discharge_id.action_open_billing_wizard()

    def action_open_insurance(self):
        return self.discharge_id.action_open_insurance_wizard()
