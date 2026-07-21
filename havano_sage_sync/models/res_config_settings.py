from odoo import fields, models, _
import requests

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
    
    sage_webhook_token = fields.Char(
        string='Webhook Security Token',
        config_parameter='havano_sage_sync.webhook_token',
        help="The secret Bearer token the C# API must provide to push data into Odoo.",
        default="SECRET_TOKEN_123"
    )
    
    sage_timeout = fields.Integer(
        string='Webhook Timeout (seconds)',
        config_parameter='havano_sage_sync.timeout',
        help="Number of seconds to wait before timing out a webhook request to the Sage API.",
        default=10
    )

    def action_sync_all_customers_from_sage(self):
        self.ensure_one()
        api_url = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.api_url', default='http://localhost:5062/api')
    def action_open_master_sync_wizard(self):
        return {
            'name': _('Master Sage Sync'),
            'type': 'ir.actions.act_window',
            'res_model': 'havano.sage.master.sync.wizard',
            'view_mode': 'form',
            'target': 'new',
        }
