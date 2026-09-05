import logging
import requests
import json
from odoo import models, fields, api

_logger = logging.getLogger(__name__)

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    sage_invoice_number = fields.Char(string="Sage Invoice Number", readonly=True, copy=False)
    is_sage_synced = fields.Boolean(string="Sage Synced", default=False, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        orders = super(SaleOrder, self).create(vals_list)
        if not self.env.context.get('skip_sage_sync'):
            for order in orders:
                if order.partner_id and not order.partner_id.is_sage_synced:
                    order.partner_id._push_to_sage(order.partner_id, is_create=False)
                for line in order.order_line:
                    if line.product_id and not line.product_id.is_sage_synced:
                        line.product_id.product_tmpl_id._push_to_sage(line.product_id.product_tmpl_id, is_create=False)
                order._push_sales_to_sage(is_update=False)
        return orders

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
        # write() will automatically handle the push to Sage now since state changed to 'sale'
        return res

    def write(self, vals):
        if not self.env.context.get('skip_sage_sync') and 'is_sage_synced' not in vals:
            vals['is_sage_synced'] = False
            
        res = super(SaleOrder, self).write(vals)
        
        if not self.env.context.get('skip_sage_sync'):
            for order in self:
                if order.state in ['draft', 'sent', 'sale', 'done'] and not order.is_sage_synced:
                    # If it has a sage_invoice_number, it means it's an update in Sage (e.g. flipping Quote to Order)
                    is_update = bool(order.sage_invoice_number)
                    order._push_sales_to_sage(is_update=is_update)
        return res

    def _push_sales_to_sage(self, is_update=False):
        enabled = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.enabled', default='True')
        if str(enabled).lower() != 'true':
            return

        api_url = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.api_url', default='http://localhost:5062/api')
        timeout = int(self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.timeout', default=10))

        for order in self:
            user = order.user_id or order.create_uid or self.env.user
            agent_id = user.sage_agent_id if user and hasattr(user, 'sage_agent_id') and user.sage_agent_id else None
            
            is_quotation = order.state in ['draft', 'sent']
            
            payload = {
                "customerCode": order.partner_id.ref or f"CUST{order.partner_id.id}",
                "externalOrderNo": order.name or "",
                "orderDate": order.date_order.strftime("%Y-%m-%dT%H:%M:%S") if order.date_order else None,
                "invoiceDate": order.date_order.strftime("%Y-%m-%dT%H:%M:%S") if order.date_order else None,
                "orderNo": order.client_order_ref or order.name,
                "agentId": agent_id,
                "isQuotation": is_quotation,
                "lines": []
            }
            
            for line in order.order_line:
                if not line.product_id:
                    continue
                if hasattr(order, 'warehouse_id') and order.warehouse_id:
                    warehouse_code = order.warehouse_id.code
                else:
                    warehouse_code = "Mstr"
                if warehouse_code == "WH":
                    warehouse_code = "Mstr"
                    
                payload["lines"].append({
                    "itemCode": line.product_id.default_code or line.product_id.product_tmpl_id.default_code or f"PROD{line.product_id.id}",
                    "quantity": float(line.product_uom_qty),
                    "unitPrice": float(line.price_subtotal / line.product_uom_qty) if line.product_uom_qty else 0.0,
                    "taxTypeID": 1 if line.tax_ids else 5,
                    "warehouseCode": warehouse_code
                })
            
            if is_update:
                endpoint = f"/Sales/orders/{order.name}"
                method = 'put'
            else:
                endpoint = "/Sales/orders"
                method = 'post'
            
            # Create a queue record for the background worker to handle the sync
            # Always queue it up immediately to avoid blocking the UI
            existing = self.env['havano.sage.queue'].sudo().search([
                ('res_model', '=', 'sale.order'),
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
                    'name': f'Sales Order {order.name}',
                    'res_model': 'sale.order',
                    'res_id': order.id,
                    'payload': json.dumps(payload),
                    'endpoint': endpoint,
                    'method': method,
                    'state': 'pending'
                })
            
            # Optimistically mark it as synced to avoid downstream blocks
            order.with_context(skip_sage_sync=True).write({'is_sage_synced': True})
            _logger.info("Queued sales order/quote %s for background Sage sync", order.name)
