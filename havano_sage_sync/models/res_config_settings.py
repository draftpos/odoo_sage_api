from odoo import fields, models

class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    sage_api_url = fields.Char(
        string='Sage API URL',
        config_parameter='havano_sage_sync.api_url',
        help="The local URL of the C# Sage API, e.g., http://192.168.1.50:5062/api",
        default="http://localhost:5062/api"
    )
    
    sage_sync_enabled = fields.Boolean(
        string='Enable Real-time Sage Sync',
        config_parameter='havano_sage_sync.enabled',
        help="If checked, Odoo will instantly push records to Sage upon creation/update.",
        default=True
    )
    
    sage_timeout = fields.Integer(
        string='Webhook Timeout (seconds)',
        config_parameter='havano_sage_sync.timeout',
        help="Number of seconds to wait before timing out a webhook request to the Sage API.",
        default=10
    )
