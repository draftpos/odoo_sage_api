import logging
import requests
import json
from odoo import models, fields, api

_logger = logging.getLogger(__name__)

class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    sage_invoice_number = fields.Char(string="Sage PO Number", readonly=True, copy=False)
    sage_grv_number = fields.Char(string="GRV Number", copy=False, help="GRV number assigned by Sage Evolution. Can be entered manually from Sage Purchase Order Maintenance.")
    is_sage_synced = fields.Boolean(string="Sage Synced", default=False, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        orders = super(PurchaseOrder, self).create(vals_list)
        if not self.env.context.get('skip_sage_sync'):
            for order in orders:
                if order.partner_id and not order.partner_id.is_sage_synced:
                    order.partner_id._push_to_sage(order.partner_id, is_create=False)
                for line in order.order_line:
                    if line.product_id:
                        tmpl = line.product_id.product_tmpl_id
                        tmpl._push_to_sage(tmpl, is_create=not tmpl.is_sage_synced)
                order._push_purchase_to_sage(is_update=False)
        return orders

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
        # write() will automatically handle the push to Sage now since state changed to 'purchase'
        return res

    def write(self, vals):
        if not self.env.context.get('skip_sage_sync') and 'is_sage_synced' not in vals:
            vals['is_sage_synced'] = False
            
        res = super(PurchaseOrder, self).write(vals)
        
        if not self.env.context.get('skip_sage_sync'):
            for order in self:
                if order.state in ['purchase', 'done'] and not order.is_sage_synced:
                    # If it has a sage_invoice_number, it means it's an update in Sage (e.g. flipping Quote to Order)
                    is_update = bool(order.sage_invoice_number)
                    order._push_purchase_to_sage(is_update=is_update)
        return res

    def _push_purchase_to_sage(self, is_update=False):
        enabled = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.enabled', default='True')
        if str(enabled).lower() != 'true':
            return

        api_url = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.api_url', default='http://localhost:5062/api')
        timeout = int(self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.timeout', default=10))

        for order in self:
            agent_id = order.user_id.sage_agent_id if hasattr(order, 'user_id') and order.user_id and hasattr(order.user_id, 'sage_agent_id') and order.user_id.sage_agent_id else None

            # Purchase Orders should never be Quotes in Sage, they are always Unprocessed Orders
            is_quotation = False

            payload = {
                "supplierCode": order.partner_id.ref or f"CUST{order.partner_id.id}",
                "externalOrderNo": order.name or "",
                "orderDate": order.date_order.strftime("%Y-%m-%dT%H:%M:%S") if order.date_order else None,
                "orderNo": order.partner_ref or "",
                "agentId": agent_id,
                "isQuotation": is_quotation,
                "lines": []
            }
            
            for line in order.order_line:
                if not line.product_id:
                    continue
                warehouse_code = order.picking_type_id.warehouse_id.code if order.picking_type_id and order.picking_type_id.warehouse_id else "Mstr"
                if warehouse_code == "WH":
                    warehouse_code = "Mstr"
                    
                payload["lines"].append({
                    "itemCode": line.product_id.default_code or line.product_id.product_tmpl_id.default_code or f"PROD{line.product_id.id}",
                    "quantity": float(line.product_qty),
                    "unitPrice": float(line.price_subtotal / line.product_qty) if line.product_qty else 0.0,
                    "taxTypeID": 1 if line.tax_ids else 5,
                    "warehouseCode": warehouse_code
                })
            
            if is_update:
                endpoint = f"/Purchase/orders/{order.name}"
                method = 'put'
            else:
                endpoint = "/Purchase/orders"
                method = 'post'
            
            # Create a queue record for the background worker to handle the sync
            # Always queue it up immediately to avoid blocking the UI
            existing = self.env['havano.sage.queue'].sudo().search([
                ('res_model', '=', 'purchase.order'),
                ('res_id', '=', order.id),
                ('state', 'in', ['pending', 'failed'])
            ], limit=1)
            
            if existing:
                existing.write({
                    'payload': json.dumps(payload),
                    'endpoint': endpoint,
                    'method': method,
                    'state': 'pending',
                    'retry_count': 0,
                    'error_message': False
                })
            else:
                self.env['havano.sage.queue'].sudo().create({
                    'name': f'Purchase Order {order.name}',
                    'res_model': 'purchase.order',
                    'res_id': order.id,
                    'payload': json.dumps(payload),
                    'endpoint': endpoint,
                    'method': method,
                    'state': 'pending'
                })
            
            # Optimistically mark it as synced to avoid downstream blocks
            order.with_context(skip_sage_sync=True).write({'is_sage_synced': True})
            _logger.info("Queued purchase order %s for background Sage sync", order.name)
