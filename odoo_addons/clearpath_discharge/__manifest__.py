{
    'name': "ClearPath Discharge",
    'version': '20.0.1.1.0',
    'summary': "AI-assisted surgical discharge with pharmacy and billing sign-off",
    'depends': ['base', 'mail', 'contacts', 'account'],
    'application': True,
    'post_init_hook': '_post_init_seed_demo',
    'installable': True,
    'author': 'ClearPath',
    'license': 'LGPL-3',

    'data': [
        "security/clearpath_security.xml",
        "security/ir.access.csv",
        "views/clearpath_report.xml",
        "wizard/clearpath_wizards_views.xml",
        "views/clearpath_discharge_views.xml",
        "views/clearpath_patient_views.xml",
        "views/res_partner_views.xml",
        "views/clearpath_menus.xml",
        "data/demo_patient_pt402.xml",
    ],

    'assets': {
        # Blue + white theme: colour variables go in before Odoo's own, so the whole backend follows them.
        'web._assets_primary_variables': [
            ('before', 'web/static/src/scss/primary_variables.scss',
             'clearpath_discharge/static/src/scss/clearpath_primary_variables.scss'),
        ],
        'web.assets_backend': [
            'clearpath_discharge/static/src/voice_recorder/voice_recorder.js',
            'clearpath_discharge/static/src/fx/clearpath_fx.js',
            'clearpath_discharge/static/src/scss/clearpath_wizards.scss',
            'clearpath_discharge/static/src/scss/clearpath_theme.scss',
        ],
    },
}
