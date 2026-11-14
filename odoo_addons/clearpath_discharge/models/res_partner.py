from odoo import api, fields, models


class ResPartner(models.Model):
    """Contacts <-> ClearPath: a Contact can be a patient (clearpath.patient delegates to res.partner).

    Reception creates the demographic record in Contacts; clinical staff then see the same person as a
    patient, with the clinical admission details added on top.
    """
    _inherit = 'res.partner'

    patient_ids = fields.One2many('clearpath.patient', 'partner_id', string="Patient Records")
    patient_count = fields.Integer(compute='_compute_patient_count')

    @api.depends('patient_ids')
    def _compute_patient_count(self):
        counts = {p.id: n for p, n in self.env['clearpath.patient']._read_group(
            [('partner_id', 'in', self.ids)], ['partner_id'], ['__count'])}
        for partner in self:
            partner.patient_count = counts.get(partner.id, 0)

    def action_open_clearpath_patient(self):
        """Open this Contact's patient record, or start one (admission details) for a new patient."""
        self.ensure_one()
        action = {'type': 'ir.actions.act_window', 'res_model': 'clearpath.patient', 'view_mode': 'form',
                  'target': 'current'}
        if self.patient_ids:
            action['res_id'] = self.patient_ids[0].id
        else:
            action['context'] = {'default_partner_id': self.id}
        return action
