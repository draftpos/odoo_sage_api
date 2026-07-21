from odoo import http
from odoo.http import request
import logging

_logger = logging.getLogger(__name__)

class SageWebhookController(http.Controller):

    @http.route('/api/sage/webhook', type='http', auth='public', methods=['POST'], csrf=False)
    def sage_webhook(self, **kwargs):
        # Authenticate using Bearer token
        auth_header = request.httprequest.headers.get('Authorization')
        expected_token = request.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.webhook_token', default="SECRET_TOKEN_123")
        
        if not auth_header or auth_header != f"Bearer {expected_token}":
            _logger.warning("Unauthorized access attempt to Sage Webhook.")
            return request.make_json_response({"status": "error", "message": "Unauthorized"}, status=401)

        try:
            import json
            payload_data = request.httprequest.data
            payload = json.loads(payload_data) if payload_data else {}
        except Exception:
            return request.make_json_response({"status": "error", "message": "Invalid JSON"}, status=400)
            
        _logger.info(f"Received Sage Webhook Payload: {payload}")

        
        if not payload:
            return request.make_json_response({"status": "error", "message": "Empty payload"}, status=400)
            
        record_type = payload.get('type')
        data = payload.get('data', {})
        action = payload.get('action', 'create')
        
        try:
            if record_type == 'customer':
                # Example logic to create a customer
                partner_val = {
                    'name': data.get('name', 'Unknown from Sage'),
                    'is_sage_synced': True,
                    # Add more fields as needed based on the payload from Sage
                }
                partner = request.env['res.partner'].sudo().with_context(skip_sage_sync=True).create(partner_val)
                return request.make_json_response({"status": "success", "id": partner.id, "message": "Customer created successfully"})
                
            elif record_type == 'invoice':
                # Add logic for invoices
                return request.make_json_response({"status": "success", "message": "Invoice logic not implemented yet"})
                
            else:
                return request.make_json_response({"status": "error", "message": f"Unsupported record type: {record_type}"}, status=400)
                
        except Exception as e:
            _logger.error(f"Error processing Sage Webhook: {str(e)}")
            return request.make_json_response({"status": "error", "message": str(e)}, status=500)
            
        return request.make_json_response({"status": "success", "message": "Payload received"})
