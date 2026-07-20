import logging
import requests
import json
from odoo import models

_logger = logging.getLogger(__name__)

class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    def button_confirm(self):
        res = super(PurchaseOrder, self).button_confirm()
        self._push_purchase_to_sage(is_create=True)
        return res

    def write(self, vals):
        res = super(PurchaseOrder, self).write(vals)
        for order in self:
            if order.state in ['purchase', 'done']:
                order._push_purchase_to_sage(is_create=False)
        return res

    def _push_purchase_to_sage(self, is_create=True):
        enabled = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.enabled', default='True')
        if str(enabled).lower() != 'true':
            return

        api_url = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.api_url', default='http://localhost:5062/api')
        timeout = int(self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.timeout', default=10))

        for order in self:
            payload = {
                "supplierCode": order.partner_id.ref or f"CUST{order.partner_id.id}",
                "externalOrderNo": order.name,
                "orderDate": order.date_order.strftime("%Y-%m-%dT%H:%M:%S") if order.date_order else "",
                "orderNo": order.partner_ref or "",
                "lines": []
            }
            
            for line in order.order_line:
                if not line.product_id:
                    continue
                payload["lines"].append({
                    "itemCode": line.product_id.default_code or f"PROD{line.product_id.id}",
                    "quantity": float(line.product_qty),
                    "unitPrice": float(line.price_unit)
                })
            
            endpoint = "/purchase/orders"
            url = f"{api_url.rstrip('/')}{endpoint}"
            
            try:
                if is_create:
                    response = requests.post(url, json=payload, headers={"Content-Type": "application/json", "Connection": "close"}, timeout=timeout)
                else:
                    put_url = f"{url}/{order.name}"
                    response = requests.put(put_url, json=payload, headers={"Content-Type": "application/json", "Connection": "close"}, timeout=timeout)
                response.raise_for_status()
                _logger.info("Successfully synced purchase order %s to Sage", order.name)
            except requests.exceptions.RequestException as e:
                error_detail = e.response.text if hasattr(e, 'response') and e.response is not None else str(e)
                full_error = f"{str(e)} - Details: {error_detail}"
                _logger.error("Failed to sync purchase order %s to Sage: %s", order.name, full_error)
                order.message_post(body=f"Sage Sync Failed: {full_error}")
