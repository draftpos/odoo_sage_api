from odoo import models, fields, api
import requests
import json
import logging

_logger = logging.getLogger(__name__)

class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def _action_done(self):
        # Call the original action_done logic
        res = super(StockPicking, self)._action_done()
        
        # After validation, check if it's an incoming receipt for a Purchase Order
        for picking in self:
            if picking.state == 'done' and picking.picking_type_code == 'incoming' and picking.purchase_id:
                order = picking.purchase_id
                
                # Only push GRV if it's synced to Sage and we haven't already processed a GRV for it
                if order.is_sage_synced and order.sage_invoice_number:
                    picking._push_grv_to_sage(order)
                    
        return res

    def _push_grv_to_sage(self, order):
        enabled = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.enabled', default='True')
        if str(enabled).lower() != 'true':
            return

        api_url = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.api_url', default='http://localhost:5062/api')
        timeout = int(self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.timeout', default=10))

        # We construct the lines based on what was actually received in this picking
        lines_payload = []
        for move in self.move_ids:
            if move.product_id and move.quantity > 0:
                warehouse_code = self.picking_type_id.warehouse_id.code if self.picking_type_id and self.picking_type_id.warehouse_id else "Mstr"
                if warehouse_code == "WH":
                    warehouse_code = "Mstr"
                
                lines_payload.append({
                    "itemCode": move.product_id.default_code or f"PROD{move.product_id.id}",
                    "quantityToProcess": int(move.quantity),
                    "warehouseCode": warehouse_code
                })

        if not lines_payload:
            _logger.info("No lines to process for GRV on picking %s", self.name)
            return

        grv_payload = {
            "orderNumber": order.sage_invoice_number,
            "externalOrderNo": order.name,
            "supplierInvoiceNo": order.partner_ref or "",
            "lines": lines_payload
        }
        
        grv_url = f"{api_url.rstrip('/')}/Purchase/orders/grv"
        
        try:
            _logger.info("Sending GRV payload for %s on picking %s: %s", order.name, self.name, json.dumps(grv_payload))
            grv_resp = requests.post(grv_url, json=grv_payload, headers={"Content-Type": "application/json", "Connection": "close"}, timeout=timeout)
            grv_resp.raise_for_status()
            
            grv_data = grv_resp.json() if grv_resp.text else {}
            grv_number = grv_data.get('grvNumber') or grv_data.get('GrvNumber') or grv_data.get('grv_number')
            
            if grv_number:
                order.with_context(skip_sage_sync=True).write({'sage_grv_number': grv_number})
                _logger.info("Successfully captured GRV number %s for PO %s", grv_number, order.name)
            else:
                _logger.info("GRV processed for PO %s (no GRV number in response)", order.name)
                
            self.message_post(body=f"Successfully synced GRV to Sage for {order.name}.")
                
        except requests.exceptions.RequestException as e:
            error_detail = e.response.text if hasattr(e, 'response') and e.response is not None else str(e)
            _logger.warning("Failed to process GRV in Sage for PO %s: %s", order.name, error_detail)
            
            self.env['havano.sage.queue'].sudo().create({
                'name': f'GRV for {order.name}',
                'res_model': 'stock.picking',
                'res_id': self.id,
                'payload': json.dumps(grv_payload),
                'endpoint': '/Purchase/orders/grv',
                'method': 'post',
                'error_message': f'Queued due to: {str(e)}'
            })
            self.message_post(body=f"GRV Sync Queued or Failed: {error_detail}")
