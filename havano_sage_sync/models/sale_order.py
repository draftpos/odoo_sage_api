import logging
import requests
import json
from odoo import models, fields

_logger = logging.getLogger(__name__)

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    sage_invoice_number = fields.Char(string="Sage Invoice Number", readonly=True, copy=False)
    is_sage_synced = fields.Boolean(string="Sage Synced", default=False, copy=False)

    def action_confirm(self):
        for order in self:
            if order.partner_id and not order.partner_id.is_sage_synced:
                # Forcefully sync the customer/supplier first before confirming the order
                order.partner_id._push_to_sage(order.partner_id, is_create=False)
            
            # Sync unsynced products
            for line in order.order_line:
                if line.product_id and not line.product_id.is_sage_synced:
                    line.product_id.product_tmpl_id._push_to_sage(line.product_id.product_tmpl_id, is_create=False)
                
        res = super(SaleOrder, self).action_confirm()
        self._push_sales_to_sage(is_update=False)
        return res

    def write(self, vals):
        if not self.env.context.get('skip_sage_sync') and 'is_sage_synced' not in vals:
            vals['is_sage_synced'] = False
            
        res = super(SaleOrder, self).write(vals)
        
        if not self.env.context.get('skip_sage_sync'):
            for order in self:
                if order.state in ['sale', 'done'] and order.sage_invoice_number and not order.is_sage_synced:
                    # Only do PUT (is_update=True) if it ALREADY has a sage_invoice_number!
                    order._push_sales_to_sage(is_update=True)
        return res

    def _push_sales_to_sage(self, is_update=False):
        enabled = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.enabled', default='True')
        if str(enabled).lower() != 'true':
            return

        api_url = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.api_url', default='http://localhost:5062/api')
        timeout = int(self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.timeout', default=10))

        for order in self:
            agent_id = order.user_id.sage_agent_id if order.user_id and hasattr(order.user_id, 'sage_agent_id') and order.user_id.sage_agent_id else None
            
            payload = {
                "customerCode": order.partner_id.ref or f"CUST{order.partner_id.id}",
                "externalOrderNo": order.name or "",
                "orderDate": order.date_order.strftime("%Y-%m-%dT%H:%M:%S") if order.date_order else None,
                "invoiceDate": order.date_order.strftime("%Y-%m-%dT%H:%M:%S") if order.date_order else None,
                "orderNo": order.client_order_ref or order.name,
                "agentId": agent_id,
                "lines": []
            }
            
            for line in order.order_line:
                if not line.product_id:
                    continue
                warehouse_code = order.warehouse_id.code if order.warehouse_id else "Mstr"
                if warehouse_code == "WH":
                    warehouse_code = "Mstr"
                    
                payload["lines"].append({
                    "itemCode": line.product_id.default_code or f"PROD{line.product_id.id}",
                    "quantity": float(line.product_uom_qty),
                    "unitPrice": float(line.price_unit),
                    "taxTypeID": 1,
                    "warehouseCode": warehouse_code
                })
            
            endpoint = "/Sales/orders"
            url = f"{api_url.rstrip('/')}{endpoint}"
            
            # Log payload for diagnostics
            _logger.info("Sage Sales Order POST Payload for %s: %s", order.name, json.dumps(payload))
            
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
                    # Auto-generate invoice in Sage for this order
                    try:
                        inv_url = f"{api_url.rstrip('/')}/sales/orders/{sage_inv_no}/invoice"
                        inv_resp = requests.post(inv_url, headers={"Content-Type": "application/json", "Connection": "close"}, timeout=timeout)
                        inv_resp.raise_for_status()
                        
                        # C# API returns the generated invoice number (e.g. INV2726) in the response text or JSON
                        if inv_resp.text:
                            try:
                                inv_data = inv_resp.json()
                                if isinstance(inv_data, dict):
                                    real_inv_no = inv_data.get('invoiceNumber') or inv_data.get('orderNumber') or inv_resp.text
                                else:
                                    real_inv_no = str(inv_data)
                            except Exception:
                                real_inv_no = inv_resp.text.strip('\"')
                            
                            if real_inv_no:
                                vals['sage_invoice_number'] = real_inv_no
                    except Exception as ie:
                        _logger.warning("Failed to auto-invoice sales order %s in Sage: %s", sage_inv_no, str(ie))
                        # If invoicing fails due to network, queue the invoice generation call
                        status_code = ie.response.status_code if hasattr(ie, 'response') and ie.response is not None else 0
                        if status_code == 0 or status_code >= 500:
                            self.env['havano.sage.queue'].sudo().create({
                                'name': f"Invoice: {order.name}",
                                'res_model': 'sale.order',
                                'res_id': order.id,
                                'payload': '{}',
                                'endpoint': f"/sales/orders/{sage_inv_no}/invoice",
                                'method': 'post',
                                'state': 'pending'
                            })
                    
                order.with_context(skip_sage_sync=True).write(vals)
                _logger.info("Successfully synced sales order %s to Sage (Sage No: %s)", order.name, vals.get('sage_invoice_number', sage_inv_no))
            except requests.exceptions.RequestException as e:
                # Handle friendly errors or queue if offline
                error_detail = e.response.text if hasattr(e, 'response') and e.response is not None else str(e)
                status_code = e.response.status_code if hasattr(e, 'response') and e.response is not None else 0
                
                # If network error or timeout, queue it
                if status_code == 0 or status_code >= 500:
                    self.env['havano.sage.queue'].sudo().create({
                        'name': f'Sales Order {order.name}',
                        'res_model': 'sale.order',
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
                    _logger.error("Failed to sync sales order %s to Sage: %s", order.name, full_error)
                    order.message_post(body=f"Sage Sync Failed: {full_error}. Please correct the issue and manually retry if needed.")
                    # Do NOT raise UserError here so the Odoo workflow can continue!
