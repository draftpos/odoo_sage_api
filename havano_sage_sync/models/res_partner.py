import logging
import requests
import json
from odoo import models, fields, api

_logger = logging.getLogger(__name__)

class ResPartner(models.Model):
    _inherit = 'res.partner'

    is_sage_synced = fields.Boolean(string="Sage Synced", default=False, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        records = super(ResPartner, self).create(vals_list)
        # Auto-generate ref for any partner that doesn't have one
        for record in records:
            if not record.ref:
                record.with_context(skip_sage_sync=True).write({'ref': f'CUST{record.id:05d}'})
        if not self.env.context.get('import_file') and not self.env.context.get('skip_sage_sync'):
            self._push_to_sage(records, is_create=True)
        return records

    def write(self, vals):
        if not self.env.context.get('skip_sage_sync') and 'is_sage_synced' not in vals:
            vals['is_sage_synced'] = False
            
        result = super(ResPartner, self).write(vals)
        
        if not self.env.context.get('skip_sage_sync'):
            # Only push records that are not synced yet
            unsynced = self.filtered(lambda r: not r.is_sage_synced)
            if unsynced:
                self._push_to_sage(unsynced, is_create=False)
        return result

    def _push_to_sage(self, records, is_create=True):
        enabled = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.enabled', default='True')
        if str(enabled).lower() != 'true':
            return

        api_url = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.api_url', default='http://localhost:5062/api')
        timeout = int(self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.timeout', default=10))

        for record in records:
            # No type restriction - sync all partners to Sage
                
            payload = {
                "code": record.ref or f"CUST{record.id}",
                "description": record.name,
                "telephone": record.phone or "",
                "emailAddress": record.email or "",
                "postalAddress": {
                    "line1": record.street or "",
                    "line2": record.street2 or "",
                    "line3": record.city or "",
                    "line4": record.state_id.name if record.state_id else "",
                    "line5": record.country_id.name if record.country_id else "",
                    "postalCode": record.zip or ""
                },
                "physicalAddress": {
                    "line1": record.street or "",
                    "line2": record.street2 or "",
                    "line3": record.city or "",
                    "line4": record.state_id.name if record.state_id else "",
                    "line5": record.country_id.name if record.country_id else "",
                    "postalCode": record.zip or ""
                }
            }
            
            # Determine if supplier or customer
            endpoint = "/suppliers" if record.supplier_rank > 0 else "/customers"
            
            # Create a queue record for the background worker to handle the sync
            # Always queue it up immediately to avoid blocking the UI
            method = 'post' if is_create else 'put'
            self.env['havano.sage.queue'].sudo().create({
                'name': record.name,
                'res_model': 'res.partner',
                'res_id': record.id,
                'payload': json.dumps(payload),
                'endpoint': endpoint,
                'method': method,
                'state': 'pending'
            })
            
            # Optimistically mark it as synced to avoid downstream blocks
            record.with_context(skip_sage_sync=True).write({'is_sage_synced': True})
            _logger.info("Queued partner %s for background Sage sync", record.name)
