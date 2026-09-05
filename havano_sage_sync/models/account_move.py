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

                        # Step 1: Ensure the SO is confirmed (not a Quotation) in Sage before invoicing.
                        # Queue the conversion to a Sales Order.
                        if so.state == 'sale' and sage_inv_no:
                            convert_endpoint = f"/Sales/orders/{sage_inv_no}"
                            import json
                            so_user = so.user_id or so.create_uid or self.env.user
                            so_agent_id = so_user.sage_agent_id if so_user and hasattr(so_user, 'sage_agent_id') and so_user.sage_agent_id else None
                            convert_payload = {
                                "customerCode": so.partner_id.ref or f"CUST{so.partner_id.id}",
                                "externalOrderNo": so.name or "",
                                "orderDate": so.date_order.strftime("%Y-%m-%dT%H:%M:%S") if so.date_order else None,
                                "agentId": so_agent_id,
                                "isQuotation": False,
                                "lines": [
                                    {
                                        "itemCode": line.product_id.default_code or line.product_id.product_tmpl_id.default_code or f"PROD{line.product_id.id}",
                                        "quantity": float(line.product_uom_qty),
                                        "unitPrice": float(line.price_subtotal / line.product_uom_qty) if line.product_uom_qty else 0.0,
                                        "taxTypeID": 1 if line.tax_ids else 5,
                                        "warehouseCode": "Mstr"
                                    }
                                    for line in so.order_line if line.product_id
                                ]
                            }
                            
                            self.env['havano.sage.queue'].sudo().create({
                                'name': f'Quote Conversion {so.name}',
                                'res_model': 'sale.order',
                                'res_id': so.id,
                                'payload': json.dumps(convert_payload),
                                'endpoint': convert_endpoint,
                                'method': 'put',
                                'state': 'pending'
                            })

                        # Step 2: Trigger Invoice Creation in Sage (queued after conversion)
                        endpoint = f"/Sales/orders/{sage_inv_no}/invoice"
                        self.env['havano.sage.queue'].sudo().create({
                            'name': f'Invoice {move.name}',
                            'res_model': 'account.move',
                            'res_id': move.id,
                            'payload': '{}',  # Empty payload for POST to invoice
                            'endpoint': endpoint,
                            'method': 'post',
                            'state': 'pending'
                        })
                        
                        move.write({'is_sage_synced': True})
                        _logger.info("Queued Sales Invoice %s for background Sage sync", move.name)

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
                        import json
                        payload = {
                            "supplierInvoiceNo": move.ref or move.name,
                            "orderNumber": sage_inv_no
                        }
                        if po.sage_grv_number:
                            payload["grvNumber"] = po.sage_grv_number
                        
                        self.env['havano.sage.queue'].sudo().create({
                            'name': f'Vendor Bill {move.name}',
                            'res_model': 'account.move',
                            'res_id': move.id,
                            'payload': json.dumps(payload),
                            'endpoint': endpoint,
                            'method': 'post',
                            'state': 'pending'
                        })
                        
                        move.write({'is_sage_synced': True})
                        _logger.info("Queued Vendor Bill %s for background Sage sync", move.name)

            except Exception as e:
                _logger.error("Sage Sync Error for Invoice %s: %s", move.name, str(e))
                move.message_post(body=f"Sage Sync Failed: Unexpected Error - {str(e)}")

        return res
