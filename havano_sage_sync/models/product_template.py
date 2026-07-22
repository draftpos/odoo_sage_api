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
            
            payload = {
                "code": record.default_code or f"PROD{record.id}",
                "description": record.name,
                "isWarehouseTracked": True,
                "isServiceItem": False,
                "active": record.active
            }
            
            endpoint = "/inventory"
            url = f"{api_url.rstrip('/')}{endpoint}"
            
            try:
                # Intelligent Upsert: Try PUT (update). If not found, try POST (create).
                response = requests.put(url, json=payload, headers={"Content-Type": "application/json", "Connection": "close"}, timeout=timeout)
                if response.status_code != 200 and "not found" in response.text.lower():
                    # Fallback to POST
                    response = requests.post(url, json=payload, headers={"Content-Type": "application/json", "Connection": "close"}, timeout=timeout)
                
                response.raise_for_status()
                record.with_context(skip_sage_sync=True).write({'is_sage_synced': True})
                _logger.info("Successfully synced product %s to Sage", record.name)
                
                # Auto-assign to default warehouse (Mstr) using new V20 API endpoint
                try:
                    wh_payload = {
                        "itemCode": payload["code"],
                        "warehouseCode": "Mstr"
                    }
                    wh_url = f"{api_url.rstrip('/')}/Inventory/warehouse"
                    wh_resp = requests.post(wh_url, json=wh_payload, headers={"Content-Type": "application/json", "Connection": "close"}, timeout=timeout)
                    wh_resp.raise_for_status()
                    _logger.info("Successfully linked product %s to Mstr warehouse", record.name)
                except requests.exceptions.RequestException as wh_e:
                    wh_error = wh_e.response.text if hasattr(wh_e, 'response') and wh_e.response is not None else str(wh_e)
                    _logger.warning("Could not link product %s to warehouse Mstr: %s", record.name, wh_error)
                    
            except requests.exceptions.RequestException as e:
                error_detail = e.response.text if hasattr(e, 'response') and e.response is not None else str(e)
                status_code = e.response.status_code if hasattr(e, 'response') and e.response is not None else 0
                full_error = f"{str(e)} - Details: {error_detail}"
                _logger.error("Failed to sync product %s to Sage: %s", record.name, full_error)
                record.message_post(body=f"Sage Sync Failed: {full_error}")
                
                # If network error or server error, queue it
                if status_code == 0 or status_code >= 500:
                    self.env['havano.sage.queue'].sudo().create({
                        'name': record.name,
                        'res_model': 'product.template',
                        'res_id': record.id,
                        'payload': json.dumps(payload),
                        'endpoint': endpoint,
                        'method': 'post' if is_create else 'put',
                        'state': 'pending'
                    })
