import logging
import requests
from odoo import models, fields, api

_logger = logging.getLogger(__name__)

class AccountMove(models.Model):
    _inherit = 'account.move'

    is_sage_synced = fields.Boolean(string="Synced with Sage", default=False, tracking=True)

    def action_post(self):
        res = super(AccountMove, self).action_post()
        
        # Check if Sage sync is enabled
        enabled = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.enabled', default='True')
        if str(enabled).lower() != 'true':
            return res

        api_url = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.api_url', default='http://localhost:5062/api')
        timeout = int(self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.timeout', default=10))

        for move in self:
            # Check for skip context
            if self.env.context.get('skip_sage_sync') or move.is_sage_synced:
                continue

            try:
                # Handle Customer Invoices (Sales)
                if move.move_type == 'out_invoice':
                    # Find linked Sales Order
                    # Odoo invoice lines link to sale order lines which link to sale orders
                    sale_orders = move.invoice_line_ids.mapped('sale_line_ids.order_id')
                    
                    if not sale_orders:
                        _logger.info("Invoice %s has no linked Sales Order. Cannot sync to Sage.", move.name)
                        move.message_post(body="Sage Sync: Standalone invoices without a Sales Order cannot currently be synced to Sage.")
                        continue
                        
                    for so in sale_orders:
                        sage_inv_no = so.sage_invoice_number or so.client_order_ref or so.name
                        if not so.is_sage_synced:
                            _logger.warning("Sales Order %s is not synced to Sage yet. Skipping invoice push.", so.name)
                            move.message_post(body=f"Sage Sync: Linked Sales Order {so.name} is not synced to Sage yet. Cannot push Invoice.")
                            continue
                            
                        # Trigger Invoice Creation in Sage
                        endpoint = f"/Sales/orders/{sage_inv_no}/invoice"
                        url = f"{api_url.rstrip('/')}{endpoint}"
                        
                        response = requests.post(url, headers={"Content-Type": "application/json", "Connection": "close"}, timeout=timeout)
                        response.raise_for_status()
                        
                        move.write({'is_sage_synced': True})
                        move.message_post(body=f"Sage Sync: Successfully triggered Invoice creation for Sales Order {sage_inv_no} in Sage.")

                # Handle Vendor Bills (Purchase)
                elif move.move_type == 'in_invoice':
                    # Find linked Purchase Order
                    purchase_orders = move.invoice_line_ids.mapped('purchase_line_id.order_id')
                    
                    if not purchase_orders:
                        _logger.info("Bill %s has no linked Purchase Order. Cannot sync to Sage.", move.name)
                        move.message_post(body="Sage Sync: Standalone bills without a Purchase Order cannot currently be synced to Sage.")
                        continue
                        
                    for po in purchase_orders:
                        sage_inv_no = po.sage_invoice_number or po.partner_ref or po.name
                        if not po.is_sage_synced:
                            _logger.warning("Purchase Order %s is not synced to Sage yet. Skipping bill push.", po.name)
                            move.message_post(body=f"Sage Sync: Linked Purchase Order {po.name} is not synced to Sage yet. Cannot push Bill.")
                            continue
                            
                        # Trigger Invoice Creation in Sage
                        endpoint = f"/Purchase/orders/{sage_inv_no}/invoice"
                        url = f"{api_url.rstrip('/')}{endpoint}"
                        
                        # Provide supplierInvoiceNo
                        payload = {
                            "supplierInvoiceNo": move.ref or move.name,
                            "orderNumber": sage_inv_no
                        }
                        
                        response = requests.post(url, json=payload, headers={"Content-Type": "application/json", "Connection": "close"}, timeout=timeout)
                        response.raise_for_status()
                        
                        move.write({'is_sage_synced': True})
                        move.message_post(body=f"Sage Sync: Successfully triggered Bill creation for Purchase Order {sage_inv_no} in Sage.")

            except requests.exceptions.RequestException as e:
                error_detail = e.response.text if hasattr(e, 'response') and e.response is not None else str(e)
                _logger.error("Sage Sync API Error for Invoice %s: %s", move.name, error_detail)
                move.message_post(body=f"Sage Sync Failed: {error_detail}")
            except Exception as e:
                _logger.error("Sage Sync Error for Invoice %s: %s", move.name, str(e))
                move.message_post(body=f"Sage Sync Failed: Unexpected Error - {str(e)}")

        return res
