import logging
import requests
import json
from odoo import models, fields, api

_logger = logging.getLogger(__name__)

class ProductTemplate(models.Model):
    _inherit = 'product.template'

    is_sage_synced = fields.Boolean(string="Sage Synced", default=False, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        records = super(ProductTemplate, self).create(vals_list)
        # Auto-generate internal reference for any product that doesn't have one
        for record in records:
            if not record.default_code:
                record.with_context(skip_sage_sync=True).write({'default_code': f'PROD{record.id:05d}'})
        if not self.env.context.get('skip_sage_sync'):
            self._push_to_sage(records, is_create=True)
        return records

    def write(self, vals):
        if not self.env.context.get('skip_sage_sync') and 'is_sage_synced' not in vals:
            vals['is_sage_synced'] = False
            
        result = super(ProductTemplate, self).write(vals)
        
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
            # No type restriction - sync all products to Sage

                
            price_list_name = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.price_list_name', default='Retail')
            
            variant = record.product_variant_id
            variant_id = variant.id if variant else record.id
            
            payload = {
                "code": record.default_code or f"PROD{variant_id}",
                "description": record.name,
                "isServiceItem": False,
                "active": record.active,
                "sellingPrices": [
                    {
                        "priceList": price_list_name,
                        "priceExcl": record.list_price
                    }
                ]
            }
            
            # Only send warehouse tracking flag when creating a new product (POST)
            # Sage throws a 500 error if we try to alter it on an existing product (PUT)
            if is_create:
                payload["isWarehouseTracked"] = True
            
            endpoint = "/inventory"
            url = f"{api_url.rstrip('/')}{endpoint}"
            endpoint = "/inventory"
            
            # Create a queue record for the background worker to handle the sync
            # Always queue it up immediately to avoid blocking the UI
            method = 'post' if is_create else 'put'
            self.env['havano.sage.queue'].sudo().create({
                'name': record.name,
                'res_model': 'product.template',
                'res_id': record.id,
                'payload': json.dumps(payload),
                'endpoint': endpoint,
                'method': method,
                'state': 'pending'
            })
            
            # Optimistically mark it as synced to avoid downstream blocks
            record.with_context(skip_sage_sync=True).write({'is_sage_synced': True})
            _logger.info("Queued product %s for background Sage sync", record.name)
