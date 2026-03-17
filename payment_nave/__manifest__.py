{
    'name': 'Nave Payment Provider',
    'version': '16.0.1.0.0',
    'category': 'Accounting/Payment',
    'summary': 'Pago por redirección usando Nave (tarjetas, MODO, QR)',
    'description': """
Nave for Odoo
=============
Integra Nave como método de pago por redirección en el eCommerce de Odoo.
Soporta tarjetas de crédito/débito, MODO y QR.

Características:
- Pago por redirección al checkout de Nave
- Webhook server-to-server para actualización de estados
- Cron de polling como safety net
- Soporte Odoo 16, 17 y 18
- Compatible con Community y Enterprise
    """,
    'author': 'Nave Integrations',
    'website': 'https://navenegocios.com',
    'license': 'LGPL-3',
    'depends': ['payment'],
    'data': [
        'security/ir.model.access.csv',
        'views/payment_nave_templates.xml',
        'data/payment_provider_data.xml',
        'data/ir_cron_data.xml',
    ],
    'assets': {
        'web.assets_frontend': [],
    },
    'images': ['static/description/banner.png'],
    'application': False,
    'installable': True,
    'auto_install': False,
}
