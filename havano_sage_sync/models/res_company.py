import logging
from odoo import models, api

_logger = logging.getLogger(__name__)

class ResCompany(models.Model):
    _inherit = 'res.company'

    @api.model
    def _cron_sync_to_sage(self):
        _logger.info("Running scheduled cron job to sync offline records to Sage...")
        
        # 1. Sync Partners (Customers/Suppliers)
        partners = self.env['res.partner'].search([('is_sage_synced', '=', False)])
        if partners:
            _logger.info("Cron syncing %s partners...", len(partners))
            for partner in partners:
                partner._push_to_sage(partner, is_create=False)
                
        # 2. Sync Products
        products = self.env['product.template'].search([('is_sage_synced', '=', False), ('type', '=', 'consu')])
        if products:
            _logger.info("Cron syncing %s products...", len(products))
            for product in products:
                product._push_to_sage(product, is_create=False)
                
        # 3. Sync Sales Orders
        sales = self.env['sale.order'].search([('state', 'in', ['sale', 'done']), ('is_sage_synced', '=', False)])
        if sales:
            _logger.info("Cron syncing %s sales orders...", len(sales))
            sales._push_sales_to_sage(is_update=False)
            
        # 4. Sync Purchase Orders
        purchases = self.env['purchase.order'].search([('state', 'in', ['purchase', 'done']), ('is_sage_synced', '=', False)])
        if purchases:
            _logger.info("Cron syncing %s purchase orders...", len(purchases))
            purchases._push_purchase_to_sage(is_update=False)
            
        _logger.info("Finished Sage offline sync cron job.")
