from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import UserError


class ClearpathAmaWizard(models.TransientModel):
    """Legal confirmation for a patient who insists on leaving against medical advice (AMA).

    Confirming releases the record even though the Coordinator Gate is locked or checks are incomplete,
    and records who acknowledged the liability waiver, when, and why.
    """
    _name = 'clearpath.ama.wizard'
    _description = 'Discharge Against Medical Advice (AMA)'

    discharge_id = fields.Many2one('clearpath.discharge', string="Discharge Record", required=True,
                                   readonly=True, ondelete='cascade')
    patient_name = fields.Char(related='discharge_id.patient_id.name', readonly=True)
    patient_code = fields.Char(related='discharge_id.patient_code', readonly=True)
    bed_number = fields.Char(related='discharge_id.bed_number', readonly=True)
    stated_reason = fields.Text(
        string="Patient's Stated Reason for Leaving", required=True,
        default="Patient insists on leaving against clinical advice despite explanation of "
                "post-operative and medication risks.")
    patient_signature_name = fields.Char(string="Confirmed & Signed By (Patient Full Name)", required=True)
    confirm_liability_waiver = fields.Boolean(
        string="I confirm the doctor has advised against discharge and the patient accepts full "
               "medical & financial liability.")

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        discharge_id = res.get('discharge_id') or self.env.context.get('default_discharge_id')
        if 'patient_signature_name' in fields_list and discharge_id and not res.get('patient_signature_name'):
            # pre-filled with the patient's own name; whoever signs can overwrite it (e.g. a representative)
            res['patient_signature_name'] = self.env['clearpath.discharge'].browse(
                discharge_id).patient_id.name or ''
        return res

    def action_confirm_ama_discharge(self):
        self.ensure_one()
        discharge = self.discharge_id
        discharge._require_ama_rights()
        if not self.confirm_liability_waiver:
            raise UserError(
                "Tick the liability confirmation first: the discharge cannot be recorded as AMA without it.")
        reason = (self.stated_reason or '').strip()
        signer = (self.patient_signature_name or '').strip()
        if not reason or not signer:
            raise UserError("Enter the patient's stated reason and the name of the person who signed.")
        if discharge.state == 'released':
            raise UserError("This discharge packet is already released.")
        now = fields.Datetime.now()
        discharge.write({
            'is_ama_discharge': True,
            'ama_reason': reason,
            'ama_signed_by': signer,
            'ama_timestamp': now,
            'coordinator_gate_locked': False,
            'state': 'released',
            'released_on': now,
            'coordinator_id': self.env.user.id,
        })
        # No patient SMS here: the normal release text says "you are cleared to go home", which is not true.
        discharge.message_post(body=Markup(
            '<div class="text-danger">⚠️ <b>DISCHARGE AGAINST MEDICAL ADVICE (AMA):</b> Patient '
            'elected to leave against doctor and hospital recommendation. Liability waiver signed by '
            '<b>%s</b>. Reason: %s</div>') % (signer, reason))
        return {'type': 'ir.actions.act_window_close'}
