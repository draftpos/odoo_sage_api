import logging
import requests
import json
from odoo import models, fields, api

_logger = logging.getLogger(__name__)

class AccountAccount(models.Model):
    _inherit = 'account.account'

    is_sage_synced = fields.Boolean(string="Sage Synced", default=False, copy=False)
    sage_account_link = fields.Integer(string="Sage Account ID", readonly=True, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        records = super(AccountAccount, self).create(vals_list)
        if not self.env.context.get('import_file') and not self.env.context.get('skip_sage_sync'):
            self._push_to_sage(records, is_create=True)
        return records

    def write(self, vals):
        if not self.env.context.get('skip_sage_sync') and 'is_sage_synced' not in vals:
            vals['is_sage_synced'] = False
            
        result = super(AccountAccount, self).write(vals)
        
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
            # Strip Odoo sub-account prefix: '2000>2060' → '2060', '>040' → '040'
            sage_code = record.code.split('>')[-1] if record.code and '>' in record.code else (record.code or '')
            payload = {
                "master_Sub_Account": sage_code,
                "account": sage_code,
                "description": record.name or "",
            }
            
            endpoint = "/GLAccounts"
            method = 'post' if is_create else 'put'
            
            existing = self.env['havano.sage.queue'].sudo().search([
                ('res_model', '=', 'account.account'),
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
                    'name': f'GL Account {record.code}',
                    'res_model': 'account.account',
                    'res_id': record.id,
                    'payload': json.dumps(payload),
                    'endpoint': endpoint,
                    'method': method,
                    'state': 'pending'
                })
            
            _logger.info("Queued GL Account %s for background Sage sync", record.code)
