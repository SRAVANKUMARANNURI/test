{
    "name": "UAE E-Invoicing",
    "version": "19.0.3.0.0",
    "summary": "e-invoicing workflow",
    "category": "Accounting/Localizations",
    "author": "Sriman",
    "license": "LGPL-3",
    "depends": ["account", "purchase"],
    "data": [
        "data/einvoice.unit.code.csv",
        "security/ir.model.access.csv",
        "security/einvoice_purchase_security.xml",
        "views/account_move_views.xml",
        "views/einvoice_log_views.xml",
        "views/einvoice_menu.xml",
        "views/einvoice_purchase_views.xml",
        "views/einvoice_purchase_detail_views.xml",
        "views/einvoice_config_views.xml",
        "views/contact_view.xml",
        "views/einvoice_dashboard_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "ae_einvoice/static/src/dashboard/*",
        ],
    },
    "installable": True,
    "application": False,
}
