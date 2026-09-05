import logging
import requests
import json
from odoo import models, fields, api

_logger = logging.getLogger(__name__)

class AccountAnalyticAccount(models.Model):
    _inherit = 'account.analytic.account'

    is_sage_synced = fields.Boolean(string="Sage Synced", default=False, copy=False)
    sage_project_id = fields.Integer(string='Sage Project ID', default=0, help="The integer ID of this project in Sage Evolution.")

    @api.model_create_multi
    def create(self, vals_list):
        records = super(AccountAnalyticAccount, self).create(vals_list)
        if not self.env.context.get('import_file') and not self.env.context.get('skip_sage_sync'):
            self._push_to_sage(records, is_create=True)
        return records

    def write(self, vals):
        if not self.env.context.get('skip_sage_sync') and 'is_sage_synced' not in vals:
            vals['is_sage_synced'] = False
            
        result = super(AccountAnalyticAccount, self).write(vals)
        
        if not self.env.context.get('skip_sage_sync'):
            unsynced = self.filtered(lambda r: not r.is_sage_synced)
            if unsynced:
                self._push_to_sage(unsynced, is_create=False)
        return result

    def _push_to_sage(self, records, is_create=True):
        enabled = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.enabled', default='True')
        if str(enabled).lower() != 'true':
            return

        for record in records:
            payload = {
                "ProjectCode": record.name[:20] if record.name else "",
                "ProjectName": record.name or "",
                "ProjectDescription": record.name or "",
                "Active": True
            }
            
            endpoint = "/HavanaStores/Projects"
            method = 'post' if is_create else 'put'
            
            existing = self.env['havano.sage.queue'].sudo().search([
                ('res_model', '=', 'account.analytic.account'),
                ('res_id', '=', record.id),
                ('state', 'in', ['pending', 'failed'])
            ], limit=1)
            
            if existing:
                existing.write({
                    'payload': json.dumps(payload),
                    'endpoint': endpoint,
                    'method': method,
                    'state': 'pending',
                    'retry_count': 0,
                    'error_message': False
                })
            else:
                self.env['havano.sage.queue'].sudo().create({
                    'name': f'Project {record.name}',
                    'res_model': 'account.analytic.account',
                    'res_id': record.id,
                    'payload': json.dumps(payload),
                    'endpoint': endpoint,
                    'method': method,
                    'state': 'pending'
                })
            
            record.with_context(skip_sage_sync=True).write({'is_sage_synced': True})
            _logger.info("Queued Project %s for background Sage sync", record.name)
