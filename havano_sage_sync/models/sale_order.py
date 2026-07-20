import logging
import requests
import json
from odoo import models

_logger = logging.getLogger(__name__)

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    def action_confirm(self):
        res = super(SaleOrder, self).action_confirm()
        self._push_sales_to_sage()
        return res

    def _push_sales_to_sage(self):
        enabled = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.enabled', default='True')
        if str(enabled).lower() != 'true':
            return

        api_url = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.api_url', default='http://localhost:5062/api')
        timeout = int(self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.timeout', default=10))

        for order in self:
            payload = {
                "customerCode": order.partner_id.ref or f"CUST{order.partner_id.id}",
                "externalOrderNo": order.name,
                "orderDate": order.date_order.strftime("%Y-%m-%dT%H:%M:%S") if order.date_order else "",
                "orderNo": order.client_order_ref or "",
                "lines": []
            }
            
            for line in order.order_line:
                if not line.product_id:
                    continue
                payload["lines"].append({
                    "itemCode": line.product_id.default_code or f"PROD{line.product_id.id}",
                    "quantity": float(line.product_uom_qty),
                    "unitPrice": float(line.price_unit)
                })
            
            endpoint = "/sales/orders"
            url = f"{api_url.rstrip('/')}{endpoint}"
            
            try:
                response = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=timeout)
                response.raise_for_status()
                _logger.info("Successfully synced sales order %s to Sage", order.name)
            except requests.exceptions.RequestException as e:
                _logger.error("Failed to sync sales order %s to Sage: %s", order.name, str(e))
                order.message_post(body=f"Sage Sync Failed: {str(e)}")
