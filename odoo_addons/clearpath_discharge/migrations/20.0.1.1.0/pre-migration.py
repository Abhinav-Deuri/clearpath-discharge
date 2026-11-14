"""Phase 7: clearpath.patient now delegates to res.partner.

Existing patients keep their name by getting a Contact; the old NOT NULL `name` column is dropped so
new patients (whose name lives on the partner) can be created.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    cr.execute("""SELECT 1 FROM information_schema.columns
                  WHERE table_name = 'clearpath_patient' AND column_name = 'partner_id'""")
    if cr.fetchone():
        return  # already migrated
    cr.execute("""SELECT 1 FROM information_schema.columns
                  WHERE table_name = 'clearpath_patient' AND column_name = 'name'""")
    if not cr.fetchone():
        return  # fresh install: nothing to carry over
    env = api.Environment(cr, SUPERUSER_ID, {})
    cr.execute("ALTER TABLE clearpath_patient ADD COLUMN partner_id integer")
    cr.execute("SELECT id, name FROM clearpath_patient")
    for patient_id, name in cr.fetchall():
        partner = env['res.partner'].create({'name': name})
        cr.execute("UPDATE clearpath_patient SET partner_id = %s WHERE id = %s", (partner.id, patient_id))
    cr.execute("ALTER TABLE clearpath_patient DROP COLUMN name")
