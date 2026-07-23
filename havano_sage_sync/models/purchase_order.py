import logging
import requests
import json
from odoo import models, fields

_logger = logging.getLogger(__name__)

class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    sage_invoice_number = fields.Char(string="Sage PO Number", readonly=True, copy=False)
    sage_grv_number = fields.Char(string="GRV Number", copy=False, help="GRV number assigned by Sage Evolution. Can be entered manually from Sage Purchase Order Maintenance.")
    is_sage_synced = fields.Boolean(string="Sage Synced", default=False, copy=False)

    def button_confirm(self):
        for order in self:
            if order.partner_id and not order.partner_id.is_sage_synced:
                # Forcefully sync the customer/supplier first before confirming the order
                order.partner_id._push_to_sage(order.partner_id, is_create=False)
            
            # Sync ALL products on this PO to Sage before creating the PO
            # This ensures Sage knows about the product before we try to create the PO
            for line in order.order_line:
                if line.product_id:
                    tmpl = line.product_id.product_tmpl_id
                    # Always force-sync product to ensure it exists in Sage
                    tmpl._push_to_sage(tmpl, is_create=not tmpl.is_sage_synced)
                
        res = super(PurchaseOrder, self).button_confirm()
        self._push_purchase_to_sage(is_update=False)
        return res

    def write(self, vals):
        if not self.env.context.get('skip_sage_sync') and 'is_sage_synced' not in vals:
            vals['is_sage_synced'] = False
            
        res = super(PurchaseOrder, self).write(vals)
        
        if not self.env.context.get('skip_sage_sync'):
            for order in self:
                if order.state in ['purchase', 'done'] and order.sage_invoice_number and not order.is_sage_synced:
                    # Only do PUT (is_update=True) if it ALREADY has a sage_invoice_number!
                    order._push_purchase_to_sage(is_update=True)
        return res

    def _push_purchase_to_sage(self, is_update=False):
        enabled = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.enabled', default='True')
        if str(enabled).lower() != 'true':
            return

        api_url = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.api_url', default='http://localhost:5062/api')
        timeout = int(self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.timeout', default=10))

        for order in self:
            agent_id = order.user_id.sage_agent_id if hasattr(order, 'user_id') and order.user_id and hasattr(order.user_id, 'sage_agent_id') and order.user_id.sage_agent_id else None

            payload = {
                "supplierCode": order.partner_id.ref or f"CUST{order.partner_id.id}",
                "externalOrderNo": order.name or "",
                "orderDate": order.date_order.strftime("%Y-%m-%dT%H:%M:%S") if order.date_order else None,
                "orderNo": order.partner_ref or "",
                "agentId": agent_id,
                "lines": []
            }
            
            for line in order.order_line:
                if not line.product_id:
                    continue
                warehouse_code = order.picking_type_id.warehouse_id.code if order.picking_type_id and order.picking_type_id.warehouse_id else "Mstr"
                if warehouse_code == "WH":
                    warehouse_code = "Mstr"
                    
                payload["lines"].append({
                    "itemCode": line.product_id.default_code or f"PROD{line.product_id.id}",
                    "quantity": float(line.product_qty),
                    "unitPrice": float(line.price_unit),
                    "taxTypeID": 1,
                    "warehouseCode": warehouse_code
                })
            
            endpoint = "/Purchase/orders"
            url = f"{api_url.rstrip('/')}{endpoint}"
            
            try:
                if is_update:
                    _logger.warning(f"Sage API does not support updates yet. Skipping sync for {order.name}")
                    order.message_post(body="Sage Sync: Updates are not currently supported by the Sage API. Changes made in Odoo will not be reflected in Sage.")
                    continue
                else:
                    response = requests.post(url, json=payload, headers={"Content-Type": "application/json", "Connection": "close"}, timeout=timeout)
                response.raise_for_status()
                
                resp_data = response.json() if response.text else {}
                sage_inv_no = resp_data.get('orderNumber')
                
                vals = {'is_sage_synced': True}
                # On PUT (update), never overwrite an existing real invoice number (INV...) with
                # the order number returned by the C# API. Only set from POST responses on new orders.
                if sage_inv_no and not is_update:
                    vals['sage_invoice_number'] = sage_inv_no
                    
                    # Auto-process the GRV in Sage right after creating the PO
                    grv_payload = {
                        "orderNumber": sage_inv_no,
                        "externalOrderNo": order.name,
                        "supplierInvoiceNo": order.partner_ref or "",
                        "lines": [
                            {
                                "itemCode": l["itemCode"],
                                "quantityToProcess": int(l["quantity"]),
                                "warehouseCode": l["warehouseCode"]
                            } for l in payload.get("lines", [])
                        ]
                    }
                    grv_url = f"{api_url.rstrip('/')}/Purchase/orders/grv"
                    try:
                        _logger.info("Waiting 3s for Sage to commit PO %s before processing GRV...", order.name)
                        import time
                        time.sleep(3)
                        _logger.info("Sending GRV payload for %s: %s", order.name, json.dumps(grv_payload))
                        grv_resp = requests.post(grv_url, json=grv_payload, headers={"Content-Type": "application/json", "Connection": "close"}, timeout=30)
                        grv_resp.raise_for_status()
                        grv_data = grv_resp.json() if grv_resp.text else {}
                        grv_number = grv_data.get('grvNumber') or grv_data.get('GrvNumber') or grv_data.get('grv_number')
                        if grv_number:
                            vals['sage_grv_number'] = grv_number
                            _logger.info("Auto-captured GRV number %s for PO %s", grv_number, order.name)
                        else:
                            _logger.info("GRV processed for PO %s (no GRV number in response)", order.name)
                    except Exception as e:
                        _logger.warning("Failed to auto-process GRV in Sage for PO %s: %s", order.name, str(e))

                order.with_context(skip_sage_sync=True).write(vals)
                _logger.info("Successfully synced purchase order %s to Sage (Sage No: %s)", order.name, vals.get('sage_invoice_number', sage_inv_no))
            except requests.exceptions.RequestException as e:
                # Handle friendly errors or queue if offline
                error_detail = e.response.text if hasattr(e, 'response') and e.response is not None else str(e)
                status_code = e.response.status_code if hasattr(e, 'response') and e.response is not None else 0
                
                # If network error or timeout, queue it
                if status_code == 0 or status_code >= 500:
                    self.env['havano.sage.queue'].sudo().create({
                        'name': f'Purchase Order {order.name}',
                        'res_model': 'purchase.order',
                        'res_id': order.id,
                        'payload': json.dumps(payload),
                        'endpoint': endpoint,
                        'method': 'put' if is_update else 'post',
                        'error_message': f'Queued due to: {str(e)}'
                    })
                    order.message_post(body=f"Sage Sync Queued: Network error or server offline. Will retry automatically.")
                else:
                    # Clean up 400 Bad Request JSON
                    try:
                        err_json = json.loads(error_detail)
                        friendly_msg = err_json.get('message') or err_json.get('title') or str(err_json)
                    except Exception:
                        friendly_msg = error_detail[:200] if error_detail else "Unknown Error (Please verify your network connection and payload)"
                        
                    if status_code == 405:
                        friendly_msg = f"API Endpoint configuration error (Method Not Allowed). Method {'PUT' if is_update else 'POST'} not allowed on {endpoint}."
                        
                    full_error = f"Sage rejected the sync: {friendly_msg}"
                    _logger.error("Failed to sync purchase order %s to Sage: %s", order.name, full_error)
                    order.message_post(body=f"Sage Sync Failed: {full_error}. Please correct the issue and manually retry if needed.")
                    # Do NOT raise UserError here so the Odoo workflow can continue!
