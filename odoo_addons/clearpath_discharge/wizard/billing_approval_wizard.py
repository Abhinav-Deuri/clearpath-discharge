from odoo import api, fields, models

from ..models.clearpath_discharge import GROUP_BILLING


class ClearpathBillingApprovalLine(models.TransientModel):
    """One supply row inside the billing pop-up. Only `billed_qty` is editable."""
    _name = 'clearpath.billing.approval.line'
    _description = 'Surgical Billing Review Line'

    wizard_id = fields.Many2one('clearpath.billing.approval.wizard', required=True, ondelete='cascade')
    supply_line_id = fields.Many2one('clearpath.supply.line', ondelete='cascade')
    item_name = fields.Char()
    surgical_qty_used = fields.Integer(string="Used in Surgery")
    billed_qty = fields.Integer(string="Billed")
    price_unit = fields.Float(string="Unit Price", digits='Product Price')
    discrepancy_qty = fields.Integer(string="Unbilled Qty", compute='_compute_discrepancy')
    is_flagged = fields.Boolean(compute='_compute_discrepancy')

    @api.depends('surgical_qty_used', 'billed_qty')
    def _compute_discrepancy(self):
        for line in self:
            line.discrepancy_qty = max(line.surgical_qty_used - line.billed_qty, 0)
            line.is_flagged = line.discrepancy_qty > 0


class ClearpathBillingApprovalWizard(models.TransientModel):
    _name = 'clearpath.billing.approval.wizard'
    _description = 'Surgical Billing Review'
    _inherit = ['clearpath.approval.mixin']

    _group_xmlid = GROUP_BILLING
    _group_label = "Billing Officer"

    status = fields.Selection(related='discharge_id.billing_status')
    billing_summary = fields.Text(related='discharge_id.billing_summary')
    billing_officer_id = fields.Many2one(related='discharge_id.billing_officer_id')
    billing_approved_on = fields.Datetime(related='discharge_id.billing_approved_on')
    line_ids = fields.One2many('clearpath.billing.approval.line', 'wizard_id', string="Supplies")
    total_used = fields.Integer(compute='_compute_totals')
    total_billed = fields.Integer(compute='_compute_totals')
    total_unbilled = fields.Integer(compute='_compute_totals')

    # ------------------------------------------------------------ line seeding
    @api.model
    def _line_commands(self, discharge_id):
        supplies = self.env['clearpath.discharge'].browse(discharge_id).supply_line_ids
        return [(0, 0, {'supply_line_id': s.id, 'item_name': s.item_name,
                        'surgical_qty_used': s.surgical_qty_used, 'billed_qty': s.billed_qty,
                        'price_unit': s.price_unit})
                for s in supplies]

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        discharge_id = res.get('discharge_id') or self.env.context.get('default_discharge_id')
        if 'line_ids' in fields_list and discharge_id:
            res['line_ids'] = self._line_commands(discharge_id)
        return res

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            discharge_id = vals.get('discharge_id') or self.env.context.get('default_discharge_id')
            if discharge_id and 'line_ids' not in vals:
                vals['line_ids'] = self._line_commands(discharge_id)
        return super().create(vals_list)

    # ----------------------------------------------------------------- compute
    @api.depends('line_ids.surgical_qty_used', 'line_ids.billed_qty')
    def _compute_totals(self):
        for wiz in self:
            wiz.total_used = sum(wiz.line_ids.mapped('surgical_qty_used'))
            wiz.total_billed = sum(wiz.line_ids.mapped('billed_qty'))
            wiz.total_unbilled = sum(wiz.line_ids.mapped('discrepancy_qty'))

    @api.depends('discharge_id.state', 'discharge_id.billing_status',
                 'line_ids.surgical_qty_used', 'line_ids.billed_qty')
    def _compute_approval_state(self):
        self._fill_approval_state()

    def _dept_blockers(self):
        self.ensure_one()
        if self.status == 'stale':
            return ["the billing check is stale; re-verify the current revision"]
        open_items = self.line_ids.filtered(lambda l: l.surgical_qty_used > l.billed_qty)
        if open_items:
            return ["unbilled items remain (%s); correct the Billed column" % ", ".join(
                "+%d %s" % (l.surgical_qty_used - l.billed_qty, l.item_name) for l in open_items)]
        return []

    # ------------------------------------------------------------------ action
    def _apply_billed_quantities(self):
        """Copy the edited Billed / Unit Price columns back to the supply lines (never bumps the revision)."""
        changed = False
        for line in self.line_ids:
            supply = line.supply_line_id
            if not supply:
                continue
            vals = {}
            if supply.billed_qty != line.billed_qty:
                vals['billed_qty'] = max(line.billed_qty, 0)
            if supply.price_unit != line.price_unit:
                vals['price_unit'] = max(line.price_unit, 0.0)
            if vals:
                supply.with_context(clearpath_defer_sync=True).write(vals)
                changed = changed or 'billed_qty' in vals
        if changed:  # one Insurance-worker re-run for all the corrections
            self.discharge_id._refresh_insurance_claim("billing corrections")

    def action_approve(self):
        self.ensure_one()
        discharge = self.discharge_id
        discharge._require_group(self._group_xmlid, self._group_label)
        if self.status != 'approved':
            discharge._assert_signable()
            self._apply_billed_quantities()
        return super().action_approve()

    def _write_approval(self):
        d = self.discharge_id
        d.write({'billing_status': 'approved', 'billing_officer_id': self.env.user.id,
                 'billing_approved_on': fields.Datetime.now()})
        d.message_post(body="Billing Officer <b>%s</b> approved Rev %d." % (self.env.user.name, d.revision_id))
