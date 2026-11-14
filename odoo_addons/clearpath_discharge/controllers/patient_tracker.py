"""Public, login-free patient page: live discharge progress + co-pay.

Access is by an unguessable token in the URL (the link sent by SMS). The page only ever receives the
plain values built by `clearpath.discharge._tracker_values()`: progress states and patient-friendly
wording, never clinical findings (no drug names, allergies, billing lines or claim text).
"""
from odoo import http
from odoo.exceptions import UserError
from odoo.http import request

NO_STORE = [
    ('Cache-Control', 'no-store, max-age=0'),
    ('Referrer-Policy', 'no-referrer'),          # the token must not leak through outbound links
    ('X-Robots-Tag', 'noindex, nofollow'),
    ('X-Frame-Options', 'DENY'),
]


class PatientTracker(http.Controller):

    def _find(self, token):
        Discharge = request.env['clearpath.discharge'].sudo()
        if not token or len(token) < 16:
            return Discharge
        return Discharge.search([('tracking_token', '=', token)], limit=1)

    @http.route('/discharge/track/<string:token>', type='http', auth='public', methods=['GET'], sitemap=False)
    def track(self, token, **kwargs):
        discharge = self._find(token)
        if not discharge:
            return request.not_found()
        values = discharge._tracker_values()
        values['notice'] = kwargs.get('notice') if kwargs.get('notice') in ('paid', 'not_ready') else ''
        html = request.env['ir.qweb'].sudo()._render('clearpath_discharge.patient_tracker_page', values)
        return request.make_response(
            '<!DOCTYPE html>\n' + str(html),
            headers=[('Content-Type', 'text/html; charset=utf-8')] + NO_STORE)

    # csrf=False: the patient has no Odoo session to carry a CSRF token; the unguessable URL token is
    # the credential. This is a SIMULATED payment for the demo: no card data is collected or charged.
    @http.route('/discharge/track/<string:token>/pay', type='http', auth='public', methods=['POST'],
                csrf=False, sitemap=False)
    def pay(self, token, **kwargs):
        discharge = self._find(token)
        if not discharge:
            return request.not_found()
        try:
            discharge.action_patient_pay_copay()
            notice = 'paid'
        except UserError:
            notice = 'not_ready'
        return request.redirect('/discharge/track/%s?notice=%s' % (token, notice), code=303, local=True)
