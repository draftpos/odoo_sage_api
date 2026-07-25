import logging
import requests
from odoo import models, fields, api

_logger = logging.getLogger(__name__)

class HavanoSageQueue(models.Model):
    _name = 'havano.sage.queue'
    _description = 'Sage Sync Queue'

    name = fields.Char("Reference", required=True)
    res_model = fields.Char("Model", required=True)
    res_id = fields.Integer("Record ID", required=True)
    payload = fields.Text("Payload JSON", required=True)
    endpoint = fields.Char("API Endpoint", required=True)
    method = fields.Selection([
        ('post', 'POST'),
        ('put', 'PUT')
    ], string="HTTP Method", required=True)
    state = fields.Selection([
        ('pending', 'Pending'),
        ('failed', 'Failed'),
        ('done', 'Done')
    ], default='pending', string="Status")
    error_message = fields.Text("Last Error")
    retry_count = fields.Integer("Retry Count", default=0)

    def process_queue(self):
        records = self.search([('state', 'in', ['pending', 'failed'])], limit=50)
        
        api_url = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.api_url', default='http://localhost:5062/api')
        timeout = int(self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.timeout', default=10))

        for record in records:
            url = f"{api_url.rstrip('/')}{record.endpoint}"
            try:
                headers = {"Content-Type": "application/json", "Connection": "close"}
                if record.method == 'post':
                    response = requests.post(url, data=record.payload, headers=headers, timeout=timeout)
                else:
                    response = requests.put(url, data=record.payload, headers=headers, timeout=timeout)
                
                response.raise_for_status()
                record.write({'state': 'done', 'error_message': False})
                
                # Mark original record as synced
                target_record = self.env[record.res_model].sudo().browse(record.res_id)
                if target_record.exists() and hasattr(target_record, 'is_sage_synced'):
                    target_record.with_context(skip_sage_sync=True).write({'is_sage_synced': True})
                    if hasattr(target_record, 'message_post'):
                        target_record.message_post(body="Successfully synced to Sage via background Queue.")

            except requests.exceptions.RequestException as e:
                error_detail = e.response.text if hasattr(e, 'response') and e.response is not None else str(e)
                record.write({
                    'state': 'failed',
                    'error_message': f"Attempt {record.retry_count + 1}: {str(e)} - {error_detail}",
                    'retry_count': record.retry_count + 1
                })
