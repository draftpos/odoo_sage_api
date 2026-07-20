import logging
import requests
import json
from odoo import models, api

_logger = logging.getLogger(__name__)

class ProductTemplate(models.Model):
    _inherit = 'product.template'

    @api.model_create_multi
    def create(self, vals_list):
        records = super(ProductTemplate, self).create(vals_list)
        self._push_to_sage(records, is_create=True)
        return records

    def write(self, vals):
        result = super(ProductTemplate, self).write(vals)
        self._push_to_sage(self, is_create=False)
        return result

    def _push_to_sage(self, records, is_create=True):
        enabled = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.enabled', default='True')
        if str(enabled).lower() != 'true':
            return

        api_url = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.api_url', default='http://localhost:5062/api')
        timeout = int(self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.timeout', default=10))

        for record in records:
            if record.type != 'product':  # Only sync storable products
                continue
                
            payload = {
                "code": record.default_code or f"PROD{record.id}",
                "description": record.name,
                "type": "Stock",
                "active": record.active,
                "sellingPrices": {
                    "priceInclusive": float(record.list_price)
                }
            }
            
            endpoint = "/inventory"
            url = f"{api_url.rstrip('/')}{endpoint}"
            
            try:
                response = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=timeout)
                response.raise_for_status()
                _logger.info("Successfully synced product %s to Sage", record.name)
            except requests.exceptions.RequestException as e:
                _logger.error("Failed to sync product %s to Sage: %s", record.name, str(e))
                record.message_post(body=f"Sage Sync Failed: {str(e)}")
