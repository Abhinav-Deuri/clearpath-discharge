{
    'name': "ClearPath Discharge",
    'version': '20.0.1.0.0',
    'summary': "AI-assisted surgical discharge with pharmacy and billing sign-off",
    'depends': ['base', 'mail'],
    'application': True,
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
        "views/clearpath_menus.xml",
        "data/demo_patient_pt402.xml",
    ],

    'assets': {
        'web.assets_backend': [
            'clearpath_discharge/static/src/voice_recorder/voice_recorder.js',
            'clearpath_discharge/static/src/scss/clearpath_wizards.scss',
        ],
    },
}
