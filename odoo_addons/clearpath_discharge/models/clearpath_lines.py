from odoo import api, fields, models
from odoo.exceptions import UserError

CLINICAL_LINE_FIELDS = {'drug_name', 'dosage', 'frequency'}
# billed_qty is intentionally NOT here: billing fixes it after lock without a new revision.
SUPPLY_GUARD_FIELDS = {'item_name', 'surgical_qty_used'}


class ClearpathMedicationLine(models.Model):
    _name = 'clearpath.medication.line'
    _description = 'ClearPath Medication Line'

    discharge_id = fields.Many2one('clearpath.discharge', required=True, ondelete='cascade')
    drug_name = fields.Char(required=True)
    dosage = fields.Char()
    frequency = fields.Char()
    is_flagged = fields.Boolean(string="Flagged", readonly=True)
    conflict_reason = fields.Text(readonly=True)

    # --- Stale-Write Guard hooks: editing a medication after lock bumps the revision ---
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if not self.env.context.get('clearpath_skip_guard'):
            records.discharge_id._bump_if_locked("medication added")
        return records

    def write(self, vals):
        if CLINICAL_LINE_FIELDS & vals.keys() and not self.env.context.get('clearpath_skip_guard'):
            discharges = self.discharge_id
            discharges._assert_not_released()
            res = super().write(vals)
            discharges._bump_if_locked("medication edited")
            return res
        return super().write(vals)

    def unlink(self):
        discharges = self.discharge_id
        if not self.env.context.get('clearpath_skip_guard'):
            discharges._assert_not_released()
        res = super().unlink()
        if not self.env.context.get('clearpath_skip_guard'):
            discharges._bump_if_locked("medication removed")
        return res


class ClearpathSupplyLine(models.Model):
    _name = 'clearpath.supply.line'
    _description = 'ClearPath Surgical Supply Line'

    discharge_id = fields.Many2one('clearpath.discharge', required=True, ondelete='cascade')
    item_name = fields.Char(required=True)
    surgical_qty_used = fields.Integer(string="Used in Surgery")
    billed_qty = fields.Integer(string="Billed")
    discrepancy_qty = fields.Integer(
        string="Unbilled Qty", compute='_compute_discrepancy', store=True,
    )
    is_flagged = fields.Boolean(string="Flagged", compute='_compute_discrepancy', store=True)

    @api.depends('surgical_qty_used', 'billed_qty')
    def _compute_discrepancy(self):
        for line in self:
            line.discrepancy_qty = max(line.surgical_qty_used - line.billed_qty, 0)
            line.is_flagged = line.discrepancy_qty > 0

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.context.get('clearpath_skip_guard'):
            for vals in vals_list:
                discharge = self.env['clearpath.discharge'].browse(vals.get('discharge_id'))
                if discharge.state == 'released':
                    raise UserError("This discharge packet is released and locked.")
        records = super().create(vals_list)
        if not self.env.context.get('clearpath_skip_guard'):
            records.discharge_id._bump_if_locked("supply line added")
        return records

    def unlink(self):
        discharges = self.discharge_id
        guard = not self.env.context.get('clearpath_skip_guard')
        if guard and any(line.discharge_id.state == 'released' for line in self):
            raise UserError("This discharge packet is released and locked.")
        res = super().unlink()
        if guard:
            discharges._bump_if_locked("supply line removed")
        return res

    def write(self, vals):
        if not self.env.context.get('clearpath_skip_guard'):
            for line in self:
                if line.discharge_id.state == 'released':
                    raise UserError("This discharge packet is released and locked.")
        bump = SUPPLY_GUARD_FIELDS & vals.keys() and not self.env.context.get('clearpath_skip_guard')
        discharges = self.discharge_id
        res = super().write(vals)
        if bump:
            discharges._bump_if_locked("surgical supply edited")
        return res
