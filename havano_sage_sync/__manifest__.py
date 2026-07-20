{
    'name': 'Havano Sage Evolution Sync',
    'version': '19.0.1.0.0',
    'category': 'Integration',
    'summary': 'Synchronize Odoo with local Havano C# Sage API',
    'description': """
        This module provides real-time integration between Odoo 19 and Sage Evolution
        via a local C# API (Havano).
        
        Features:
        - Dynamic API Configuration in Settings
        - Real-time Sync of Customers and Suppliers
        - Real-time Sync of Inventory Items
        - Real-time Sync of Sales Orders
        - Real-time Sync of Purchase Orders
    """,
    'author': 'Your Company',
    'depends': ['base', 'sale_management', 'purchase', 'stock', 'account'],
    'data': [
        'views/res_config_settings_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}
