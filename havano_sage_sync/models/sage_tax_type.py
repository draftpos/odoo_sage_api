import logging
import requests
from odoo import models, fields, api

_logger = logging.getLogger(__name__)


class SageTaxType(models.Model):
    """Mirrors the Tax Types from Sage Evolution for use in Cashbook batch settings."""
    _name = 'sage.tax.type'
    _description = 'Sage Tax Type'
    _order = 'tax_type_id'

    tax_type_id = fields.Integer(string='Sage Tax Type ID', readonly=True)
    code = fields.Char(string='Code', readonly=True)
    description = fields.Char(string='Description', readonly=True)
    tax_rate = fields.Float(string='Tax Rate (%)', readonly=True)
    is_active = fields.Boolean(string='Active', readonly=True, default=True)

    _rec_name = 'description'

    @api.depends('code', 'description', 'tax_rate')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = f"[{rec.code}] {rec.description} ({rec.tax_rate}%)" if rec.code else (rec.description or '')

    def name_get(self):
        result = []
        for rec in self:
            name = f"[{rec.code}] {rec.description} ({rec.tax_rate}%)"
            result.append((rec.id, name))
        return result

    @api.model
    def sync_from_sage(self):
        """Pull all Tax Types from Sage and upsert them into Odoo."""
        api_url = self.env['ir.config_parameter'].sudo().get_param(
            'havano_sage_sync.api_url', default='http://localhost:5062/api')
        base_url = api_url.rstrip('/')
        if base_url.endswith('/api'):
            base_url = base_url[:-4]
        base_url = base_url.rstrip('/')

        try:
            resp = requests.get(f"{base_url}/HavanaStores/Taxes", timeout=10)
            resp.raise_for_status()
        except Exception as e:
            _logger.error("Failed to fetch Sage Taxes: %s", str(e))
            return False

        taxes = resp.json()
        synced = 0
        for t in taxes:
            tax_type_id = t.get('taxTypeID')
            if not tax_type_id:
                continue
            existing = self.search([('tax_type_id', '=', tax_type_id)], limit=1)
            vals = {
                'tax_type_id': tax_type_id,
                'code': t.get('code', ''),
                'description': t.get('description', ''),
                'tax_rate': t.get('taxRate', 0.0),
                'is_active': t.get('isActive', True),
            }
            if existing:
                existing.write(vals)
            else:
                self.create(vals)
            synced += 1

        _logger.info("Synced %d Tax Types from Sage", synced)
        return synced
