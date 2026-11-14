from odoo import api, fields, models
from odoo.exceptions import ValidationError


class ClearpathPatient(models.Model):
    """A patient IS an Odoo Contact: name, email, phone, address live on res.partner (delegation)."""
    _name = 'clearpath.patient'
    _description = 'ClearPath Patient'
    _inherits = {'res.partner': 'partner_id'}
    _order = 'patient_code'

    partner_id = fields.Many2one(
        'res.partner', string="Contact Record", required=True, ondelete='cascade', index=True)
    patient_code = fields.Char(string="Patient Code", required=True, copy=False)
    bed_number = fields.Char(string="Bed")
    insurance_provider = fields.Char(string="TPA / Insurance Provider")
    policy_number = fields.Char(string="Policy Number")
    allergy_ids = fields.One2many('clearpath.allergy', 'patient_id', string="Allergies")
    discharge_ids = fields.One2many('clearpath.discharge', 'patient_id', string="Discharges")
    history_ids = fields.One2many(
        'clearpath.medical.history', 'patient_id', string="Past Medical & Surgical History")

    @api.constrains('patient_code')
    def _check_patient_code_unique(self):
        for rec in self:
            if self.search_count([('patient_code', '=', rec.patient_code), ('id', '!=', rec.id)]):
                raise ValidationError("Patient code %s already exists." % rec.patient_code)


class ClearpathAllergy(models.Model):
    _name = 'clearpath.allergy'
    _description = 'ClearPath Patient Allergy'

    patient_id = fields.Many2one('clearpath.patient', required=True, ondelete='cascade')
    allergen = fields.Char(required=True)
    reaction = fields.Char()
    criticality = fields.Selection(
        [('low', 'Low'), ('medium', 'Medium'), ('high', 'High')],
        default='high', required=True,
    )


class ClearpathMedicalHistory(models.Model):
    """One past condition (chronic disease) or past operation. Fed to the AI draft so the discharge plan
    can account for it (e.g. a diabetic patient is told to watch blood sugar after surgery)."""
    _name = 'clearpath.medical.history'
    _description = 'Past Medical / Surgical History'
    _rec_name = 'condition_name'
    _order = 'history_type, id'

    patient_id = fields.Many2one('clearpath.patient', required=True, ondelete='cascade', index=True)
    condition_name = fields.Char(string="Condition / Procedure", required=True)
    history_type = fields.Selection(
        [('medical', 'Medical'), ('surgical', 'Surgical')], string="Type", required=True, default='medical')
    diagnosis_date = fields.Char(string="Year / Date", help="Free text, e.g. 2018")
    status = fields.Selection(
        [('active', 'Active'), ('resolved', 'Resolved')], required=True, default='active')
