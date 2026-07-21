from odoo import models, fields, api, _
from odoo.exceptions import UserError
import requests
import logging

_logger = logging.getLogger(__name__)

class MasterSyncWizard(models.TransientModel):
    _name = 'havano.sage.master.sync.wizard'
    _description = 'Master Sync Wizard'

    direction = fields.Selection([
        ('pull', 'Pull from Sage (Sage -> Odoo)'),
        ('push', 'Push to Sage (Odoo -> Sage)')
    ], string="Sync Direction", required=True, default='pull')
    
    sync_customers = fields.Boolean("Customers & Suppliers", default=True)
    sync_products = fields.Boolean("Inventory Products", default=False)
    sync_warehouses = fields.Boolean("Warehouses", default=False)
    sync_sales = fields.Boolean("Sales Orders", default=False)
    sync_purchases = fields.Boolean("Purchase Orders", default=False)
    
    def action_start_sync(self):
        self.ensure_one()
        api_url = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.api_url', default='http://localhost:5062/api')
        timeout = int(self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.timeout', default=60))
        
        results = []
        
        if self.direction == 'pull':
            if self.sync_customers:
                results.append(self._pull_customers(api_url, timeout))
            if self.sync_products:
                results.append(self._pull_products(api_url, timeout))
            if self.sync_warehouses:
                results.append(self._pull_warehouses(api_url, timeout))
            if self.sync_sales:
                results.append(self._pull_sales(api_url, timeout))
            if self.sync_purchases:
                results.append(self._pull_purchases(api_url, timeout))
        else:
            if self.sync_customers:
                results.append(self._push_customers(api_url, timeout))
            if self.sync_products:
                results.append(self._push_products(api_url, timeout))
            if self.sync_warehouses:
                results.append(self._push_warehouses(api_url, timeout))
            if self.sync_sales:
                results.append(self._push_sales(api_url, timeout))
            if self.sync_purchases:
                results.append(self._push_purchases(api_url, timeout))
                
        message = "\n".join(filter(None, results))
        if not message:
            message = "Nothing was selected for sync."
            
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Master Sync Completed',
                'message': message,
                'type': 'success',
                'sticky': True
            }
        }

    def _pull_customers(self, api_url, timeout):
        messages = []
        Partner = self.env['res.partner'].sudo().with_context(skip_sage_sync=True)
        
        # Pull Customers
        try:
            url_cust = f"{api_url.rstrip('/')}/Customers"
            resp_cust = requests.get(url_cust, timeout=timeout)
            resp_cust.raise_for_status()
            customers = resp_cust.json()
            if customers:
                created_cust = 0
                for cust in customers:
                    code = cust.get('code')
                    name = cust.get('description') or code
                    if not code: continue
                    if not Partner.search(['|', ('ref', '=', code), ('name', '=', name)], limit=1):
                        with self.env.cr.savepoint():
                            Partner.create({
                                'name': name, 
                                'ref': code, 
                                'is_sage_synced': True, 
                                'is_customer': True, 
                                'contact_type': 'customer'
                            })
                            created_cust += 1
                messages.append(f"Customers: Created {created_cust} of {len(customers)}.")
        except Exception as e:
            messages.append(f"Error Customers: {str(e)}")

        # Pull Suppliers
        try:
            url_supp = f"{api_url.rstrip('/')}/Suppliers"
            resp_supp = requests.get(url_supp, timeout=timeout)
            resp_supp.raise_for_status()
            suppliers = resp_supp.json()
            if suppliers:
                created_supp = 0
                for supp in suppliers:
                    code = supp.get('code')
                    name = supp.get('description') or code
                    if not code: continue
                    if not Partner.search(['|', ('ref', '=', code), ('name', '=', name)], limit=1):
                        with self.env.cr.savepoint():
                            Partner.create({
                                'name': name, 
                                'ref': code, 
                                'is_sage_synced': True, 
                                'is_supplier': True, 
                                'contact_type': 'supplier'
                            })
                            created_supp += 1
                messages.append(f"Suppliers: Created {created_supp} of {len(suppliers)}.")
        except Exception as e:
            messages.append(f"Error Suppliers: {str(e)}")

        return " | ".join(messages) if messages else "No Customers or Suppliers found."
            
    def _pull_products(self, api_url, timeout):
        try:
            url = f"{api_url.rstrip('/')}/inventory"
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            items = response.json()
            if not items: return "No inventory items found in Sage."
            
            Product = self.env['product.template'].sudo().with_context(skip_sage_sync=True)
            created = 0
            for item in items:
                code = (item.get('code') or '').strip()
                name = (item.get('description') or code).strip()
                if not code: continue
                
                # Check for existing
                existing = Product.search(['|', ('default_code', '=', code), ('name', '=ilike', name)], limit=1)
                if not existing:
                    try:
                        with self.env.cr.savepoint():
                            Product.create({
                                'name': name, 
                                'default_code': code, 
                                'type': 'consu'
                            })
                            created += 1
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).warning("Skipped product %s due to constraint: %s", name, str(e))
            return f"Pulled Products: Created {created} of {len(items)}."
        except Exception as e:
            return f"Error Pulling Products: {str(e)}"

    def _pull_warehouses(self, api_url, timeout):
        try:
            url = f"{api_url.rstrip('/')}/Inventory/warehouses"
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            warehouses = response.json()
            if not warehouses: return "No warehouses found in Sage."
            
            Warehouse = self.env['stock.warehouse'].sudo()
            created = 0
            for wh in warehouses:
                code = (wh.get('code') or '').strip()
                name = (wh.get('name') or code).strip()
                if not code: continue
                
                # Check for existing
                existing = Warehouse.search([('code', '=', code[:5])], limit=1)
                if not existing:
                    try:
                        # Odoo requires a company_id for warehouses
                        company = self.env.company
                        with self.env.cr.savepoint():
                            Warehouse.create({
                                'name': name,
                                'code': code[:5],
                                'company_id': company.id
                            })
                            created += 1
                    except Exception as e:
                        _logger.warning("Skipped warehouse %s due to constraint: %s", name, str(e))
            return f"Pulled Warehouses: Created {created} of {len(warehouses)}."
        except Exception as e:
            return f"Error Pulling Warehouses: {str(e)}"
    def _pull_sales(self, api_url, timeout):
        try:
            url = f"{api_url.rstrip('/')}/Sales/orders"
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            orders = response.json()
            if not orders: return "No sales orders found in Sage."
            
            SaleOrder = self.env['sale.order'].sudo()
            Partner = self.env['res.partner'].sudo()
            Product = self.env['product.product'].sudo()
            
            created = 0
            for order in orders:
                order_num = order.get('orderNum')
                account_id = (order.get('accountID') or '').strip()
                lines = order.get('lines', [])
                if not order_num or not account_id: continue
                
                # Check if order already exists (we might want to use client_order_ref or a custom field)
                existing = SaleOrder.search([('client_order_ref', '=', order_num)], limit=1)
                if existing:
                    if existing.state in ['draft', 'sent']:
                        try:
                            existing.with_context(skip_sage_sync=True).action_confirm()
                            invoices = existing.with_context(skip_sage_sync=True)._create_invoices()
                            for inv in invoices:
                                inv.with_context(skip_sage_sync=True).action_post()
                            existing.with_context(skip_sage_sync=True).write({'is_sage_synced': True})
                            _logger.info("Auto-invoiced existing order %s", order_num)
                        except Exception as e:
                            _logger.warning("Failed to auto-confirm existing order %s: %s", order_num, e)
                    elif existing.invoice_status == 'to invoice':
                        try:
                            invoices = existing.with_context(skip_sage_sync=True)._create_invoices()
                            for inv in invoices:
                                inv.with_context(skip_sage_sync=True).action_post()
                            existing.with_context(skip_sage_sync=True).write({'is_sage_synced': True})
                            _logger.info("Auto-invoiced confirmed-but-uninvoiced order %s", order_num)
                        except Exception as e:
                            _logger.warning("Failed to auto-invoice order %s: %s", order_num, e)
                    continue
                
                # Find customer
                customer = Partner.search(['|', ('ref', '=', account_id), ('name', '=ilike', account_id)], limit=1)
                if not customer:
                    # Create placeholder customer
                    customer = Partner.create({'name': account_id, 'ref': account_id, 'is_customer': True})
                
                order_lines = []
                for line in lines:
                    item_code = (line.get('itemCode') or '').strip()
                    qty = line.get('quantity', 1.0)
                    price = line.get('price', 0.0)
                    
                    product = Product.search([('default_code', '=', item_code)], limit=1)
                    if not product:
                        product = Product.search([('name', '=ilike', item_code)], limit=1)
                        
                    if product:
                        order_lines.append((0, 0, {
                            'product_id': product.id,
                            'product_uom_qty': qty,
                            'price_unit': price,
                        }))
                
                if order_lines:
                    try:
                        with self.env.cr.savepoint():
                            new_order = SaleOrder.with_context(skip_sage_sync=True).create({
                                'partner_id': customer.id,
                                'client_order_ref': order_num,
                                'is_sage_synced': True,
                                'order_line': order_lines,
                            })
                            new_order.with_context(skip_sage_sync=True).action_confirm()
                            # Auto create and post invoice in Odoo
                            invoices = new_order.with_context(skip_sage_sync=True)._create_invoices()
                            for inv in invoices:
                                inv.with_context(skip_sage_sync=True).action_post()
                            created += 1
                            _logger.info("Created + invoiced new order %s from Sage", order_num)
                    except Exception as e:
                        _logger.warning("Skipped sales order %s due to error: %s", order_num, str(e))
                        
            return f"Pulled Sales Orders: Created {created} of {len(orders)}."
        except Exception as e:
            return f"Error Pulling Sales Orders: {str(e)}"
    def _pull_purchases(self, api_url, timeout):
        try:
            url = f"{api_url.rstrip('/')}/Purchase/orders"
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            orders = response.json()
            if not orders: return "No purchase orders found in Sage."
            
            PurchaseOrder = self.env['purchase.order'].sudo()
            Partner = self.env['res.partner'].sudo()
            Product = self.env['product.product'].sudo()
            
            created = 0
            for order in orders:
                order_num = order.get('orderNum')
                account_id = (order.get('accountID') or '').strip()
                lines = order.get('lines', [])
                if not order_num or not account_id: continue
                
                # Check if order already exists
                existing = PurchaseOrder.search([('partner_ref', '=', order_num)], limit=1)
                if existing: continue
                
                # Find supplier
                supplier = Partner.search(['|', ('ref', '=', account_id), ('name', '=ilike', account_id)], limit=1)
                if not supplier:
                    supplier = Partner.create({'name': account_id, 'ref': account_id, 'is_supplier': True})
                
                order_lines = []
                for line in lines:
                    item_code = (line.get('itemCode') or '').strip()
                    qty = line.get('quantity', 1.0)
                    price = line.get('price', 0.0)
                    
                    product = Product.search([('default_code', '=', item_code)], limit=1)
                    if not product:
                        product = Product.search([('name', '=ilike', item_code)], limit=1)
                        
                    if product:
                        order_lines.append((0, 0, {
                            'product_id': product.id,
                            'product_qty': qty,
                            'price_unit': price,
                        }))
                
                if order_lines:
                    try:
                        with self.env.cr.savepoint():
                            new_po = PurchaseOrder.create({
                                'partner_id': supplier.id,
                                'partner_ref': order_num,
                                'order_line': order_lines,
                            })
                            new_po.button_confirm()
                            # Auto create and post vendor bill in Odoo
                            new_po.action_create_invoice()
                            for inv in new_po.invoice_ids:
                                inv.action_post()
                            created += 1
                    except Exception as e:
                        _logger.warning("Skipped purchase order %s due to error: %s", order_num, str(e))
                        
            return f"Pulled Purchase Orders: Created {created} of {len(orders)}."
        except Exception as e:
            return f"Error Pulling Purchase Orders: {str(e)}"

    def _push_customers(self, api_url, timeout):
        Partner = self.env['res.partner'].sudo()
        unsynced = Partner.search([('is_sage_synced', '=', False)])
        if not unsynced: return "All Odoo customers are already synced to Sage."
        
        # call the push_to_sage method on the partner model
        Partner._push_to_sage(unsynced, is_create=False)
        return f"Pushed {len(unsynced)} Odoo customers to Sage."
        
    def _push_products(self, api_url, timeout):
        Product = self.env['product.template'].sudo()
        # Find products not synced (assuming we have a flag, but we don't. We'll push all active for now)
        unsynced = Product.search([('type', '=', 'consu')]) # In a real scenario, use a synced flag
        if not unsynced: return "No products to push."
        
        # We can call the existing _push_to_sage method
        unsynced._push_to_sage(unsynced, is_create=False)
        return f"Pushed {len(unsynced)} Odoo products to Sage."
        
    def _push_warehouses(self, api_url, timeout):
        return "Pushing warehouses from Odoo to Sage is not implemented yet."
        
    def _push_sales(self, api_url, timeout):
        SaleOrder = self.env['sale.order'].sudo()
        unsynced = SaleOrder.search([('state', 'in', ['sale', 'done']), ('is_sage_synced', '=', False)])
        if not unsynced: return "All confirmed Odoo sales orders are already synced to Sage."
        
        unsynced._push_sales_to_sage(is_update=False)
        return f"Pushed {len(unsynced)} Odoo Sales Orders to Sage."
        
    def _push_purchases(self, api_url, timeout):
        PurchaseOrder = self.env['purchase.order'].sudo()
        unsynced = PurchaseOrder.search([('state', 'in', ['purchase', 'done']), ('is_sage_synced', '=', False)])
        if not unsynced: return "All confirmed Odoo purchase orders are already synced to Sage."
        
        unsynced._push_purchase_to_sage(is_update=False)
        return f"Pushed {len(unsynced)} Odoo Purchase Orders to Sage."
