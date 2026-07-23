import logging
import requests
from odoo import models, fields, api

_logger = logging.getLogger(__name__)

class HavanoSagePoller(models.AbstractModel):
    _name = 'havano.sage.poller'
    _description = 'Sage Sync Poller'

    @api.model
    def poll_changes(self):
        """Alias used by V2 cron definition in havano_sage_sync_views.xml"""
        return self.cron_poll_sage_changes()

    @api.model
    def cron_poll_sage_changes(self):
        enabled = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.enabled', default='True')
        if str(enabled).lower() != 'true':
            _logger.info("Sage Sync is disabled. Skipping poller.")
            return

        api_url = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.api_url', default='http://localhost:5062/api')
        timeout = int(self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.timeout', default=10))

        endpoint = "/sync/changes"
        url = f"{api_url.rstrip('/')}{endpoint}"

        try:
            # Step 1: Fetch Pending Changes
            response = requests.get(url, headers={"Connection": "close"}, timeout=timeout)
            
            if response.status_code == 404:
                _logger.warning("Poller endpoint %s not found (C# team might still be building it).", url)
                return
                
            response.raise_for_status()
            
            response_data = response.json()
            changes = response_data.get('items', [])
            if not changes:
                _logger.info("No new changes to sync from Sage.")
                return

            _logger.info("Fetched %d pending changes from Sage.", len(changes))

            for change in changes:
                if isinstance(change, str):
                    import json
                    try:
                        change = json.loads(change)
                    except Exception:
                        _logger.error("Failed to parse change string: %s", change)
                        continue
                        
                change_id = change.get('id')
                # Try getting the properties (casing might vary, check lower and capitalized)
                record_type = (change.get('entity') or change.get('Entity') or "").lower()
                action = (change.get('action') or change.get('Action') or "").lower()
                sage_id = change.get('externalReference') or change.get('ExternalReference')
                data = change.get('data') or change.get('Data') or {}
                
                _logger.info("Processing change ID %s: %s (Action: %s, Sage ID: %s)", change_id, record_type, action, sage_id)
                
                try:
                    if record_type == "customer":
                        self._sync_partner(sage_id, data, action, "customer")
                    elif record_type == "supplier":
                        self._sync_partner(sage_id, data, action, "supplier")
                    elif record_type in ("inventoryitem", "product"):
                        self._sync_product(sage_id, data, action)
                    elif record_type in ("agent", "salesrep"):
                        self._sync_agent(sage_id, data, action)
                    elif record_type in ("salesorder", "salesinvoice", "invoice"):
                        self._sync_sales_order(sage_id, data, action)
                    elif record_type in ("purchaseorder", "purchaseinvoice", "order"):
                        self._sync_purchase_order(sage_id, data, action)
                    elif record_type == "quotation":
                        self._sync_sales_order(sage_id, data, action, is_quote=True)
                    else:
                        _logger.warning("Unknown entity type %s for change ID %s", record_type, change_id)
                except Exception as model_ex:
                    _logger.error("Error processing %s %s: %s", record_type, sage_id, str(model_ex))
                    continue # Skip acknowledging this specific item so it retries later
                
                # Step 3: Acknowledge the Change
                if change_id:
                    ack_url = f"{api_url.rstrip('/')}/sync/changes/{change_id}/acknowledge"
                    ack_response = requests.post(ack_url, headers={"Connection": "close"}, timeout=timeout)
                    ack_response.raise_for_status()
                    _logger.info("Successfully acknowledged change ID %s.", change_id)

        except requests.exceptions.RequestException as e:
            error_detail = e.response.text if hasattr(e, 'response') and e.response is not None else str(e)
            _logger.error("Failed to poll Sage changes: %s - %s", str(e), error_detail)
        except Exception as ex:
            _logger.error("Unexpected error during Sage polling: %s", str(ex))

    def _sync_partner(self, sage_id, data, action, partner_type):
        partner_obj = self.env['res.partner']
        domain = [('ref', '=', sage_id)]
        partner = partner_obj.search(domain, limit=1)
        
        vals = {
            'name': data.get('Name') or data.get('name') or data.get('Description') or data.get('description') or sage_id,
            'ref': sage_id,
            'email': data.get('Email') or data.get('email') or data.get('emailAddress') or '',
            'phone': data.get('Telephone') or data.get('telephone') or data.get('phone') or data.get('Phone') or '',
            'is_sage_synced': True,
        }
        
        # Map address fields
        phys = data.get('physicalAddress') or data.get('PhysicalAddress') or {}
        if phys:
            vals.update({
                'street': phys.get('line1') or phys.get('Line1') or '',
                'street2': phys.get('line2') or phys.get('Line2') or '',
                'city': phys.get('line3') or phys.get('Line3') or '',
                'zip': phys.get('postalCode') or phys.get('PostalCode') or '',
            })
        
        if partner_type == "customer":
            vals['customer_rank'] = 1
            vals['contact_type'] = 'customer'
        else:
            vals['supplier_rank'] = 1
            vals['contact_type'] = 'supplier'
            
        if action.lower() == 'delete' and partner:
            partner.with_context(skip_sage_sync=True).write({'active': False})
            return
            
        if partner:
            partner.with_context(skip_sage_sync=True).write(vals)
        else:
            partner_obj.with_context(skip_sage_sync=True).create(vals)

    def _sync_product(self, sage_id, data, action):
        product_obj = self.env['product.template'].sudo()
        domain = [('default_code', '=', sage_id)]
        product = product_obj.search(domain, limit=1)
        
        vals = {
            'name': data.get('Description') or data.get('description') or sage_id,
            'default_code': sage_id,
            'type': 'consu',
            'is_sage_synced': True,
        }
        
        # Map selling price from sellingPrices list (priceList == 1) or fallback fields
        prices = data.get('sellingPrices', [])
        if prices:
            price_1 = next((p for p in prices if p.get('priceList') == 1), None)
            if price_1:
                vals['list_price'] = price_1.get('priceExcl', 0.0)
        else:
            vals['list_price'] = data.get('SellingPrice') or data.get('sellingPrice') or 0.0
            vals['standard_price'] = data.get('CostPrice') or data.get('costPrice') or 0.0
        
        if action.lower() == 'delete' and product:
            product.with_context(skip_sage_sync=True).write({'active': False})
            return
            
        if product:
            product.with_context(skip_sage_sync=True).write(vals)
        else:
            product_obj.with_context(skip_sage_sync=True).create(vals)

    def _sync_agent(self, sage_id, data, action):
        """Update the matching Odoo user when a Sage Agent changes."""
        if not data:
            return
        user_obj = self.env['res.users'].sudo()
        name = data.get('name') or data.get('Name') or sage_id
        
        # Find by sage_agent_id first, then fall back to name match
        user = user_obj.search([('sage_agent_id', '!=', 0), ('name', '=ilike', name)], limit=1)
        if not user:
            # Try to find by login or email as last resort
            email = data.get('email') or data.get('Email') or f"{sage_id}@havano.local"
            user = user_obj.search([('login', '=', email)], limit=1)
        
        if user:
            user.with_context(skip_sage_sync=True).write({'name': name})
            _logger.info("Updated Odoo user '%s' from Sage Agent change.", name)
        else:
            try:
                email = data.get('email') or data.get('Email') or f"agent_{sage_id}@havano.local"
                new_user = user_obj.with_context(skip_sage_sync=True).create({
                    'name': name,
                    'login': email,
                    'sage_agent_id': int(sage_id) if str(sage_id).isdigit() else 0,
                })
                group = self.env.ref('sales_team.group_sale_salesman', raise_if_not_found=False)
                if group:
                    group.sudo().write({'users': [(4, new_user.id)]})
                _logger.info("Created missing Odoo user '%s' from Sage Agent.", name)
            except Exception as e:
                _logger.warning("Could not create missing Odoo user '%s': %s", name, str(e))

    def _sync_sales_order(self, order_ref, data, action, is_quote=False):
        """Update a Sales Order/Quotation in Odoo when Sage reports a change (e.g. invoice number assigned)."""
        if not data:
            return
        order_obj = self.env['sale.order'].sudo()
        order = None
        # Only match by order_ref if it is not empty/falsy to prevent matching empty references
        if order_ref:
            order = order_obj.search(['|', ('name', '=', order_ref), ('client_order_ref', '=', order_ref)], limit=1)
            
        if not order:
            # Fallback: if order_ref is the invoice number, look up by orderNo/orderNumber in the data payload
            order_no = data.get('orderNo') or data.get('OrderNo') or data.get('orderNumber') or data.get('OrderNumber')
            if order_no:
                order = order_obj.search(['|', ('name', '=', order_no), ('client_order_ref', '=', order_no)], limit=1)
                
        if order:
            sage_inv_no = data.get('invoiceNumber') or data.get('InvoiceNumber') or data.get('orderNumber') or data.get('OrderNumber') or order_ref
            write_vals = {'is_sage_synced': True}
            if sage_inv_no:
                write_vals['sage_invoice_number'] = sage_inv_no
            order.with_context(skip_sage_sync=True).write(write_vals)
            _logger.info("Updated Odoo Sales Order %s from Sage change (Sage Inv: %s).", order.name, sage_inv_no)
        else:
            # Auto-create missing Sales Order
            account_id = (data.get('accountID') or data.get('AccountID') or '').strip()
            if not account_id:
                _logger.warning("Cannot create missing sales order %s without an account ID", order_ref)
                return
            
            Partner = self.env['res.partner'].sudo()
            customer = Partner.search(['|', ('ref', '=', account_id), ('name', '=ilike', account_id)], limit=1)
            if not customer:
                customer = Partner.with_context(skip_sage_sync=True).create({'name': account_id, 'ref': account_id, 'is_customer': True})
            
            lines = data.get('lines') or data.get('Lines') or []
            order_lines = []
            Product = self.env['product.product'].sudo()
            for line in lines:
                item_code = (line.get('itemCode') or line.get('ItemCode') or '').strip()
                qty = line.get('quantity') or line.get('Quantity') or 1.0
                price = line.get('price') or line.get('Price') or 0.0
                
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
                        new_order = order_obj.with_context(skip_sage_sync=True).create({
                            'partner_id': customer.id,
                            'client_order_ref': order_ref,
                            'is_sage_synced': True,
                            'order_line': order_lines,
                        })
                        if not is_quote and not data.get('isQuotation'):
                            new_order.with_context(skip_sage_sync=True).action_confirm()
                            invoices = new_order.with_context(skip_sage_sync=True)._create_invoices()
                            for inv in invoices:
                                inv.with_context(skip_sage_sync=True).action_post()
                            _logger.info("Created and invoiced missing Odoo Sales Order %s from Sage", order_ref)
                        else:
                            _logger.info("Created missing Odoo Sales Quotation %s from Sage", order_ref)
                except Exception as e:
                    _logger.warning("Failed to create missing sales order %s: %s", order_ref, str(e))

    def _sync_purchase_order(self, order_ref, data, action):
        """Update a Purchase Order in Odoo when Sage reports a change (e.g. invoice number assigned)."""
        if not data:
            return
        order_obj = self.env['purchase.order'].sudo()
        order = None
        if order_ref:
            order = order_obj.search(['|', ('name', '=', order_ref), ('partner_ref', '=', order_ref)], limit=1)
            
        if not order:
            order_no = data.get('orderNo') or data.get('OrderNo') or data.get('orderNumber') or data.get('OrderNumber')
            if order_no:
                order = order_obj.search(['|', ('name', '=', order_no), ('partner_ref', '=', order_no)], limit=1)
                
        if order:
            sage_inv_no = data.get('invoiceNumber') or data.get('InvoiceNumber') or data.get('orderNumber') or data.get('OrderNumber') or order_ref
            grv_number = data.get('grvNumber') or data.get('GrvNumber') or data.get('grv') or data.get('GRV') or data.get('grvNo') or data.get('GrvNo')
            write_vals = {'is_sage_synced': True}
            if sage_inv_no:
                write_vals['sage_invoice_number'] = sage_inv_no
            if grv_number:
                write_vals['sage_grv_number'] = grv_number
            order.with_context(skip_sage_sync=True).write(write_vals)
            _logger.info("Updated Odoo Purchase Order %s from Sage change (Sage Inv: %s, GRV: %s).", order.name, sage_inv_no, grv_number)
        else:
            # Auto-create missing Purchase Order
            account_id = (data.get('accountID') or data.get('AccountID') or '').strip()
            if not account_id:
                _logger.warning("Cannot create missing purchase order %s without an account ID", order_ref)
                return
            
            Partner = self.env['res.partner'].sudo()
            supplier = Partner.search(['|', ('ref', '=', account_id), ('name', '=ilike', account_id)], limit=1)
            if not supplier:
                supplier = Partner.with_context(skip_sage_sync=True).create({'name': account_id, 'ref': account_id, 'is_supplier': True})
            
            lines = data.get('lines') or data.get('Lines') or []
            order_lines = []
            Product = self.env['product.product'].sudo()
            for line in lines:
                item_code = (line.get('itemCode') or line.get('ItemCode') or '').strip()
                qty = line.get('quantity') or line.get('Quantity') or 1.0
                price = line.get('price') or line.get('Price') or 0.0
                
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
                        new_po = order_obj.with_context(skip_sage_sync=True).create({
                            'partner_id': supplier.id,
                            'partner_ref': order_ref,
                            'is_sage_synced': True,
                            'order_line': order_lines,
                        })
                        new_po.with_context(skip_sage_sync=True).button_confirm()
                        new_po.with_context(skip_sage_sync=True).action_create_invoice()
                        for inv in new_po.invoice_ids:
                            inv.with_context(skip_sage_sync=True).action_post()
                        _logger.info("Created and billed missing Odoo Purchase Order %s from Sage", order_ref)
                except Exception as e:
                    _logger.warning("Failed to create missing purchase order %s: %s", order_ref, str(e))

    def _sync_order(self, sage_id, data, action):
        """Legacy dispatcher — kept for backward compatibility."""
        is_purchase = data.get('IsPurchaseOrder') or data.get('isPurchaseOrder') or False
        if is_purchase:
            self._sync_purchase_order(sage_id, data, action)
        else:
            self._sync_sales_order(sage_id, data, action)
