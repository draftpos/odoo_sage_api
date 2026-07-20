import logging
import requests
import json
from odoo import models, fields

_logger = logging.getLogger(__name__)

class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    sage_invoice_number = fields.Char(string="Sage Invoice Number", readonly=True, copy=False)
    is_sage_synced = fields.Boolean(string="Sage Synced", default=False, copy=False)

    def button_confirm(self):
        for order in self:
            if order.partner_id:
                # Forcefully sync the customer/supplier first before confirming the order
                order.partner_id._push_to_sage(order.partner_id, is_create=False)
                
        res = super(PurchaseOrder, self).button_confirm()
        self._push_purchase_to_sage(is_update=False)
        return res

    def write(self, vals):
        if not self.env.context.get('skip_sage_sync') and 'is_sage_synced' not in vals:
            vals['is_sage_synced'] = False
            
        res = super(PurchaseOrder, self).write(vals)
        
        if not self.env.context.get('skip_sage_sync'):
            for order in self:
                if order.state in ['purchase', 'done'] and not order.sage_invoice_number and not order.is_sage_synced:
                    order._push_purchase_to_sage(is_update=True)
        return res

    def _push_purchase_to_sage(self, is_update=False):
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
                    "unitPrice": float(line.price_unit),
                    "warehouseCode": order.picking_type_id.warehouse_id.code if order.picking_type_id and order.picking_type_id.warehouse_id else ""
                })
            
            endpoint = f"/purchase/orders/{order.name}" if is_update else "/purchase/orders"
            url = f"{api_url.rstrip('/')}{endpoint}"
            
            try:
                if is_update:
                    response = requests.put(url, json=payload, headers={"Content-Type": "application/json", "Connection": "close"}, timeout=timeout)
                else:
                    response = requests.post(url, json=payload, headers={"Content-Type": "application/json", "Connection": "close"}, timeout=timeout)
                response.raise_for_status()
                
                resp_data = response.json() if response.text else {}
                sage_inv_no = resp_data.get('orderNumber')
                
                vals = {'is_sage_synced': True}
                if sage_inv_no:
                    vals['sage_invoice_number'] = sage_inv_no
                    
                order.with_context(skip_sage_sync=True).write(vals)
                _logger.info("Successfully synced purchase order %s to Sage (Sage No: %s)", order.name, sage_inv_no)
            except requests.exceptions.RequestException as e:
                error_detail = e.response.text if hasattr(e, 'response') and e.response is not None else str(e)
                full_error = f"{str(e)} - Details: {error_detail}"
                _logger.error("Failed to sync purchase order %s to Sage: %s", order.name, full_error)
                order.message_post(body=f"Sage Sync Failed: {full_error}")
