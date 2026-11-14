from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import UserError


class ClearpathApprovalMixin(models.AbstractModel):
    """Shared behaviour of the three departmental sign-off pop-ups (HITL gate #1).

    A subclass supplies: `status` (related to the department status on the discharge),
    `_group_xmlid` / `_group_label`, `_dept_blockers()` and `_write_approval()`.
    The checks are the same ones the old header buttons ran; only the place moved.
    """
    _name = 'clearpath.approval.mixin'
    _description = 'ClearPath departmental approval pop-up (shared)'

    _group_xmlid = None
    _group_label = None

    discharge_id = fields.Many2one('clearpath.discharge', required=True, readonly=True, ondelete='cascade')
    patient_name = fields.Char(related='discharge_id.patient_id.name')
    patient_code = fields.Char(related='discharge_id.patient_code')
    bed_number = fields.Char(related='discharge_id.bed_number')
    revision_id = fields.Integer(related='discharge_id.revision_id')
    state = fields.Selection(related='discharge_id.state')
    can_approve = fields.Boolean(compute='_compute_approval_state')
    blocker_html = fields.Html(compute='_compute_approval_state', sanitize=False)

    # ------------------------------------------------------------- blockers
    @api.depends('discharge_id.state')
    def _compute_approval_state(self):
        self._fill_approval_state()

    def _fill_approval_state(self):
        for wiz in self:
            if not wiz.discharge_id or wiz.status == 'approved':
                wiz.can_approve = False
                wiz.blocker_html = False
                continue
            reasons = wiz._all_blockers()
            wiz.can_approve = not reasons
            items = Markup('').join(Markup('<li>%s</li>') % r for r in reasons)
            wiz.blocker_html = Markup(
                '<div class="fw-semibold mb-1">Why approval is blocked</div>'
                '<ul class="mb-0 ps-3">%s</ul>') % items if reasons else False

    def _all_blockers(self):
        self.ensure_one()
        state = self.discharge_id.state
        if state == 'released':
            return ["the discharge packet is released and locked"]
        if state not in ('flagged', 'verifying'):
            return ["sign-off opens once the revision is locked and verified"]
        return self._dept_blockers()

    def _dept_blockers(self):
        return []

    def _write_approval(self):
        raise NotImplementedError

    # ------------------------------------------------------------- the action
    def action_approve(self):
        """Approve & Resolve: group check, same-revision/blocker checks, sign, maybe open gate #2."""
        self.ensure_one()
        discharge = self.discharge_id
        discharge._require_group(self._group_xmlid, self._group_label)
        if self.status == 'approved':
            raise UserError("This revision is already approved by your department.")
        discharge._assert_signable()
        reasons = self._all_blockers()
        if reasons:
            raise UserError("Cannot approve yet: " + "; ".join(reasons) + ".")
        self._write_approval()
        discharge._update_gate()
        return discharge._soft_reload()
