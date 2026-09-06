import logging
import requests
import json
import re
from odoo import models, fields, api

_logger = logging.getLogger(__name__)

# Errors that will NEVER succeed no matter how many times we retry.
# These indicate a permanent Sage data or configuration problem.
FATAL_ERROR_PATTERNS = [
    'no detail record was found',      # Corrupt item in Sage - needs Sage admin to fix
    'purchase order.*not found as an unprocessed order',  # Already processed/archived in Sage
]

MAX_RETRIES = 5  # Maximum attempts before auto-resolving a failed item


def _sage_acc_code(odoo_code):
    """Strip Odoo sub-account prefix so '2000>2060' or '>040' becomes '2060' / '040'.
    Sage's PostBatch stored procedure tries CAST(Account AS INT) so any '>' causes a crash."""
    if odoo_code and '>' in odoo_code:
        return odoo_code.split('>')[-1]
    return odoo_code or ''


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

    def _is_fatal_error(self, error_text):
        """Returns True if this error will never resolve itself and should be skipped."""
        lower = error_text.lower()
        for pattern in FATAL_ERROR_PATTERNS:
            if re.search(pattern, lower):
                return True
        return False

    def process_queue(self):
        api_url = self.env['ir.config_parameter'].sudo().get_param(
            'havano_sage_sync.api_url', default='http://localhost:5062/api')
        timeout = int(self.env['ir.config_parameter'].sudo().get_param(
            'havano_sage_sync.timeout', default=10))
        headers = {"Content-Type": "application/json", "Connection": "close"}

        # Step 1: Auto-resolve any items that have exceeded max retries or have fatal errors
        # This MUST run first so they never block the queue
        stuck = self.search([
            ('state', 'in', ['failed', 'pending']),
            ('retry_count', '>', MAX_RETRIES)
        ])
        if stuck:
            stuck.write({
                'state': 'done',
                'error_message': f'Auto-resolved after {MAX_RETRIES} failed attempts. Check Sage data integrity.'
            })
            self.env.cr.commit()
            _logger.warning("Auto-resolved %d items that exceeded %d retries", len(stuck), MAX_RETRIES)

        # Step 2: Process each pending/failed record INDEPENDENTLY
        # Each record is committed separately so one failure never affects others
        records = self.search([('state', 'in', ['pending', 'failed'])], limit=50)

        for record in records:
            try:
                self._process_single_record(record, api_url, timeout, headers)
                self.env.cr.commit()
            except Exception as e:
                # Roll back only this record's transaction, not the whole batch
                self.env.cr.rollback()
                _logger.error("Unexpected error processing queue record %s (%s id=%s): %s",
                              record.id, record.res_model, record.res_id, str(e))
                try:
                    record.write({
                        'state': 'failed',
                        'error_message': f"Unexpected error: {str(e)}",
                        'retry_count': record.retry_count + 1
                    })
                    self.env.cr.commit()
                except Exception:
                    self.env.cr.rollback()

    def _process_single_record(self, record, api_url, timeout, headers):
        """Process a single queue record. Raises on unrecoverable errors."""
        url = f"{api_url.rstrip('/')}{record.endpoint}"
        if record.endpoint == '/GLAccounts':
            base_url = api_url.rstrip('/')
            if '/api' in base_url.lower():
                base_url = base_url.lower().replace('/api', '')
            url = f"{base_url}/HavanaStores/GLAccounts"
        elif record.endpoint == '/HavanaStores/Projects':
            base_url = api_url.rstrip('/')
            if '/api' in base_url.lower():
                base_url = base_url.lower().replace('/api', '')
            url = f"{base_url}/HavanaStores/Projects"

        # --- GRV: wait for parent purchase order to get a Sage invoice number first ---
        if record.res_model == 'stock.picking' and record.endpoint == '/Purchase/orders/grv':
            payload_dict = json.loads(record.payload)
            if not payload_dict.get("orderNumber"):
                picking = self.env['stock.picking'].sudo().browse(record.res_id)
                if picking.exists() and picking.purchase_id and picking.purchase_id.sage_invoice_number:
                    payload_dict["orderNumber"] = picking.purchase_id.sage_invoice_number
                    record.payload = json.dumps(payload_dict)
                else:
                    # Not ready yet, leave as pending, will retry next cron run
                    record.write({'error_message': 'Waiting: parent PO not yet synced to Sage.'})
                    return

        # --- Vendor Bill: wait for parent purchase order to get a Sage invoice number first ---
        if record.res_model == 'account.move' and '/invoice' in record.endpoint:
            payload_dict = json.loads(record.payload)
            move = self.env['account.move'].sudo().browse(record.res_id)
            if move.exists() and move.move_type == 'in_invoice':
                po = move.invoice_line_ids.mapped('purchase_line_id.order_id')
                if po:
                    po = po[0]
                    # Check if the payload's orderNumber is not the real Sage number yet
                    if payload_dict.get("orderNumber") == po.name and po.sage_invoice_number and po.sage_invoice_number != po.name:
                        # Update the payload and endpoint with the real Sage number
                        payload_dict["orderNumber"] = po.sage_invoice_number
                        record.payload = json.dumps(payload_dict)
                        # The endpoint might be /Purchase/orders/{P00151}/invoice, update it too
                        if po.name in record.endpoint:
                            record.endpoint = record.endpoint.replace(po.name, po.sage_invoice_number)
                    elif not po.sage_invoice_number:
                        # Parent PO is not synced yet, wait
                        record.write({'error_message': 'Waiting: parent PO not yet synced to Sage.'})
                        return

        # --- Pre-warm warehouse links for all line items in orders ---
        # This prevents the "no detail record" error on orders by ensuring
        # every product is properly linked to the warehouse before the order is sent.
        if record.res_model in ('sale.order', 'purchase.order'):
            payload_check = json.loads(record.payload)
            for line in payload_check.get('lines', []):
                item_code = (line.get('itemCode') or line.get('ItemCode', '')).strip()
                if not item_code:
                    continue
                try:
                    wh_url = f"{api_url.rstrip('/')}/Inventory/warehouse"
                    wh_resp = requests.post(
                        wh_url,
                        json={"itemCode": item_code, "warehouseCode": "Mstr"},
                        headers=headers, timeout=timeout
                    )
                    # 400/500 with "already" means it's already linked - that's fine
                    already_linked = (
                        wh_resp.status_code in (400, 500) and
                        ('already' in wh_resp.text.lower() or 'detail record' not in wh_resp.text.lower())
                    )
                    if not already_linked and wh_resp.status_code not in (200, 201):
                        _logger.warning("Pre-warm warehouse link for item %s: %s %s",
                                        item_code, wh_resp.status_code, wh_resp.text[:200])
                except Exception as wh_e:
                    _logger.warning("Pre-warm warehouse link failed for item %s: %s", item_code, str(wh_e))

        # --- Make the API call ---
        # Special case for Cashbook Batches (multi-step process)
        if record.res_model == 'cashbook.batch' and record.endpoint == '/HavanaStores/CashBook/Batches':
            batch = self.env['cashbook.batch'].sudo().browse(record.res_id)
            if not batch.exists() or batch.is_sage_synced:
                self._mark_done(record)
                return
            
            # Increase timeout for Cashbook batches because creating batch + lines + posting is slow
            timeout = max(timeout, 60)

            sage_gl_by_link = {}
            sage_gl_by_code = {}
            sage_gl_by_sub = {}
            sage_gl_by_desc = {}
            sage_customers = {}
            sage_customers_by_name = {}
            sage_suppliers = {}
            sage_suppliers_by_name = {}
            
            # Extract base URL (remove /api if it exists)
            base_url = api_url
            if base_url.endswith('/api'):
                base_url = base_url[:-4]
            base_url = base_url.rstrip('/')
            
            # Lookup mappings
            gl_resp = requests.get(f"{base_url}/HavanaStores/GLAccounts", timeout=timeout)
            if gl_resp.ok:
                for acc in gl_resp.json():
                    acc_key = acc.get('account') or acc.get('Account')
                    acc_sub = acc.get('master_Sub_Account') or acc.get('Master_Sub_Account')
                    acc_desc = (acc.get('description') or acc.get('Description') or '').strip().lower()
                    acc_link = acc.get('accountLink') or acc.get('AccountLink')
                    if acc_link is not None:
                        sage_gl_by_link[acc_link] = acc_link
                        if acc_key:
                            sage_gl_by_code[str(acc_key).strip()] = acc_link
                        if acc_sub:
                            sage_gl_by_sub[str(acc_sub).strip()] = acc_link
                        if acc_desc:
                            sage_gl_by_desc[acc_desc] = acc_link
            
            cust_resp = requests.get(f"{base_url}/api/Customers", timeout=timeout)
            if not cust_resp.ok:
                cust_resp = requests.get(f"{api_url}/Customers", timeout=timeout)
            if cust_resp.ok:
                for c in cust_resp.json():
                    cid = c.get('id')
                    code = c.get('code')
                    desc = (c.get('description') or c.get('name') or '').strip().lower()
                    if code and cid:
                        sage_customers[code] = cid
                    if desc and cid:
                        sage_customers_by_name[desc] = (code, cid)
                        
            supp_resp = requests.get(f"{base_url}/api/Suppliers", timeout=timeout)
            if not supp_resp.ok:
                supp_resp = requests.get(f"{api_url}/Suppliers", timeout=timeout)
            if supp_resp.ok:
                for s in supp_resp.json():
                    sid = s.get('id')
                    code = s.get('code')
                    desc = (s.get('description') or s.get('name') or '').strip().lower()
                    if code and sid:
                        sage_suppliers[code] = sid
                    if desc and sid:
                        sage_suppliers_by_name[desc] = (code, sid)

            bank_acc_code = _sage_acc_code(batch.journal_id.default_account_id.code if batch.journal_id.default_account_id else "")
            bank_acc_ref = batch.journal_id.default_account_id
            bank_acc_id = None
            if bank_acc_ref and hasattr(bank_acc_ref, 'sage_account_link') and bank_acc_ref.sage_account_link in sage_gl_by_link:
                bank_acc_id = bank_acc_ref.sage_account_link
            elif bank_acc_code in sage_gl_by_code:
                bank_acc_id = sage_gl_by_code[bank_acc_code]
            elif bank_acc_code in sage_gl_by_sub:
                bank_acc_id = sage_gl_by_sub[bank_acc_code]
            else:
                # Dynamically create bank GL account in Sage
                _logger.info("Bank GL Account '%s' not in Sage — creating it now...", bank_acc_code)
                bank_create_payload = {
                    "master_Sub_Account": bank_acc_code,
                    "account": bank_acc_code,
                    "description": bank_acc_ref.name if bank_acc_ref else bank_acc_code,
                }
                bank_create_resp = requests.post(
                    f"{base_url}/HavanaStores/GLAccounts",
                    json=bank_create_payload, headers=headers, timeout=timeout
                )
                if bank_create_resp.ok:
                    new_link = bank_create_resp.json().get('accountLink') or bank_create_resp.json().get('AccountLink')
                    if new_link:
                        if bank_acc_ref:
                            bank_acc_ref.with_context(skip_sage_sync=True).write({'sage_account_link': new_link})
                        sage_gl_by_link[new_link] = new_link
                        bank_acc_id = new_link
                    else:
                        bank_acc_id = 93  # last resort fallback
                else:
                    bank_acc_id = 93  # last resort fallback

            # Default tax account in Sage: Vat Control (9600 / link 105)
            vat_control_acc_id = sage_gl_by_code.get('9600') or sage_gl_by_desc.get('vat control') or 105
            input_tax_acc_id = batch.sage_input_tax_acc_id if batch.sage_input_tax_acc_id and batch.sage_input_tax_acc_id != 1 else vat_control_acc_id
            output_tax_acc_id = batch.sage_output_tax_acc_id if batch.sage_output_tax_acc_id and batch.sage_output_tax_acc_id != 1 else vat_control_acc_id
            
            first_debit_tax = next((l.sage_tax_id.tax_type_id for l in batch.line_ids if l.debit > 0 and l.sage_tax_id), None)
            first_credit_tax = next((l.sage_tax_id.tax_type_id for l in batch.line_ids if l.credit > 0 and l.sage_tax_id), None)

            input_tax_type_id = batch.sage_input_tax.tax_type_id if batch.sage_input_tax else (first_debit_tax or batch.sage_input_tax_id or 1)
            output_tax_type_id = batch.sage_output_tax.tax_type_id if batch.sage_output_tax else (first_credit_tax or batch.sage_output_tax_id or 1)
            
            batch_payload = {
                "cBatchNo": batch.name,
                "cBatchDesc": f"Cashbook Batch {batch.name}",
                "bModuleGL": True,
                "bModuleAR": True,
                "bModuleAP": True,
                "inputTaxID": input_tax_type_id,
                "iInputTaxAccID": input_tax_acc_id,
                "iOutputTaxID": output_tax_type_id,
                "iOutputTaxAccID": output_tax_acc_id,
                "bCalcTax": batch.sage_calc_tax,
                "iGLBankAccID": bank_acc_id
            }
            
            # 1. POST Header (or reuse existing batch ID)
            sage_batch_id = batch.sage_batch_id
            if not sage_batch_id:
                batch_resp = requests.post(f"{base_url}/HavanaStores/CashBook/Batches", json=batch_payload, headers=headers, timeout=timeout)
                batch_resp.raise_for_status()
                sage_batch_id = batch_resp.json().get('idBatches')
                if not sage_batch_id:
                    raise Exception("Sage API did not return idBatches")
                batch.write({'sage_batch_id': sage_batch_id})
            
            # 2. POST Lines
            lines_endpoint = f"{base_url}/HavanaStores/CashBook/BatchLines"
            for line in batch.line_ids:
                module_id = 0
                account_id = None
                
                if line.type == 'gl':
                    module_id = 0
                    acc_rec = line.account_ref
                    if not acc_rec:
                        raise Exception(f"GL line '{line.name or line.reference}' has no Account selected.")
                    
                    # 1. Check existing valid sage_account_link on account
                    if hasattr(acc_rec, 'sage_account_link') and acc_rec.sage_account_link:
                        if acc_rec.sage_account_link in sage_gl_by_link:
                            account_id = acc_rec.sage_account_link
                    
                    # 2. Match by code / normalized code / suffix
                    if not account_id and acc_rec.code:
                        raw_code = str(acc_rec.code).strip()
                        norm_code = raw_code.replace('>', '/')
                        sub_code = raw_code.split('>')[-1] if '>' in raw_code else raw_code
                        
                        account_id = (
                            sage_gl_by_code.get(raw_code) or
                            sage_gl_by_sub.get(raw_code) or
                            sage_gl_by_code.get(norm_code) or
                            sage_gl_by_sub.get(norm_code) or
                            sage_gl_by_code.get(sub_code) or
                            sage_gl_by_sub.get(sub_code)
                        )
                        if not account_id:
                            for scode, slink in sage_gl_by_code.items():
                                if scode.endswith(f"/{sub_code}") or scode.endswith(f">{sub_code}"):
                                    account_id = slink
                                    break
                    
                    # 3. Match by description
                    if not account_id and acc_rec.name:
                        desc_key = acc_rec.name.strip().lower()
                        account_id = sage_gl_by_desc.get(desc_key)
                        if not account_id:
                            for sdesc, slink in sage_gl_by_desc.items():
                                if len(sdesc) > 3 and (sdesc in desc_key or desc_key in sdesc):
                                    account_id = slink
                                    break
                    
                    # 4. If still not found, auto-create in Sage
                    if not account_id:
                        acc_code = _sage_acc_code(acc_rec.code if acc_rec.code else "")
                        _logger.info("GL Account '%s' not in Sage — creating it now...", acc_code)
                        gl_create_payload = {
                            "master_Sub_Account": acc_code,
                            "account": acc_code,
                            "description": acc_rec.name or acc_code,
                        }
                        gl_create_resp = requests.post(
                            f"{base_url}/HavanaStores/GLAccounts",
                            json=gl_create_payload, headers=headers, timeout=timeout
                        )
                        if not gl_create_resp.ok:
                            raise Exception(f"GL Account '{acc_rec.display_name}' not found in Sage and auto-creation failed: {gl_create_resp.text}")
                        new_link = gl_create_resp.json().get('accountLink') or gl_create_resp.json().get('AccountLink')
                        if not new_link:
                            raise Exception(f"GL Account '{acc_rec.display_name}' created in Sage but no accountLink returned.")
                        acc_rec.with_context(skip_sage_sync=True).write({'sage_account_link': new_link})
                        sage_gl_by_link[new_link] = new_link
                        account_id = new_link
                    else:
                        if hasattr(acc_rec, 'sage_account_link') and acc_rec.sage_account_link != account_id:
                            acc_rec.with_context(skip_sage_sync=True).write({'sage_account_link': account_id})

                    if not account_id:
                        raise Exception(f"Failed to resolve Sage GL account for '{acc_rec.display_name}'. Cannot post empty account to Sage.")

                elif line.type == 'ap':
                    module_id = 2  # AP (Supplier) is module 2 in Sage
                    partner = line.account_ref
                    candidates = [
                        partner.ref if partner else "",
                        f"SUPP{partner.id:05d}" if partner else "",
                        f"SUPP{partner.id}" if partner else "",
                        f"CUST{partner.id:05d}" if partner else "",
                        f"CUST{partner.id}" if partner else ""
                    ]
                    account_id = None
                    for cand in candidates:
                        if cand and cand in sage_suppliers:
                            account_id = sage_suppliers[cand]
                            if partner and not partner.ref:
                                partner.with_context(skip_sage_sync=True).write({'ref': cand})
                            break
                    if not account_id and partner and partner.name:
                        name_match = sage_suppliers_by_name.get(partner.name.strip().lower())
                        if name_match:
                            cand_code, account_id = name_match
                            if not partner.ref:
                                partner.with_context(skip_sage_sync=True).write({'ref': cand_code})
                    if not account_id:
                        raise Exception(f"Supplier '{partner.name if partner else ''}' ({partner.ref if partner else ''}) not found in Sage. Please sync Suppliers first.")

                elif line.type == 'ar':
                    module_id = 1  # AR (Customer) is module 1 in Sage
                    partner = line.account_ref
                    candidates = [
                        partner.ref if partner else "",
                        f"CUST{partner.id:05d}" if partner else "",
                        f"CUST{partner.id}" if partner else ""
                    ]
                    account_id = None
                    for cand in candidates:
                        if cand and cand in sage_customers:
                            account_id = sage_customers[cand]
                            if partner and not partner.ref:
                                partner.with_context(skip_sage_sync=True).write({'ref': cand})
                            break
                    if not account_id and partner and partner.name:
                        name_match = sage_customers_by_name.get(partner.name.strip().lower())
                        if name_match:
                            cand_code, account_id = name_match
                            if not partner.ref:
                                partner.with_context(skip_sage_sync=True).write({'ref': cand_code})
                    if not account_id:
                        raise Exception(f"Customer '{partner.name if partner else ''}' ({partner.ref if partner else ''}) not found in Sage. Please sync Customers first.")
                    
                # Determine tax ID based on line selection or batch settings
                if line.sage_tax_id:
                    tax_id = line.sage_tax_id.tax_type_id
                elif line.debit and batch.sage_input_tax:
                    tax_id = batch.sage_input_tax.tax_type_id
                elif line.credit and batch.sage_output_tax:
                    tax_id = batch.sage_output_tax.tax_type_id
                else:
                    tax_id = 0
                    
                tx_date = line.date.strftime('%Y-%m-%dT00:00:00') if line.date else f"{fields.Date.context_today(self)}T00:00:00"
                line_payload = {
                    "iBatchesID": sage_batch_id,
                    "iSplitType": 0,
                    "iSplitGroup": 0,
                    "dTxDate": tx_date,
                    "iModule": module_id,
                    "iAccountID": int(account_id),
                    "accountId": int(account_id),
                    "accountLink": int(account_id),
                    "AccountLink": int(account_id),
                    "cDescription": line.name or line.reference or '/',
                    "cReference": line.reference or '',
                    "fDebit": float(line.debit or 0.0),
                    "fCredit": float(line.credit or 0.0),
                    "iTaxTypeID": int(tax_id)
                }
                line_resp = requests.post(lines_endpoint, json=line_payload, headers=headers, timeout=timeout)
                line_resp.raise_for_status()
            
            # 3. POST Finalize
            post_resp = requests.post(f"{base_url}/HavanaStores/CashBook/Batches/{sage_batch_id}/post", headers=headers, timeout=timeout)
            if not post_resp.ok:
                _logger.warning("Auto-post for Sage Batch %s returned %s: %s (Batch and lines successfully synced to Sage)",
                                sage_batch_id, post_resp.status_code, post_resp.text[:300])
            
            # Mark queue item and batch as synced
            self._mark_done(record)
            batch.write({
                'is_sage_synced': True,
                'sage_batch_id': sage_batch_id,
            })
            return

        # --- Standard API Call ---
        if record.method == 'post':
            response = requests.post(url, data=record.payload, headers=headers, timeout=timeout)
        else:
            response = requests.put(url, data=record.payload, headers=headers, timeout=timeout)

        # --- Smart upsert: "already exists" on POST → switch to PUT ---
        if response.status_code == 400 and 'already exists' in response.text.lower():
            if record.res_model == 'product.template':
                # Product is in Sage already - mark done and ensure warehouse link
                self._mark_done(record)
                self._link_product_to_warehouse(record, api_url, headers, timeout)
                return
            else:
                response = requests.put(url, data=record.payload, headers=headers, timeout=timeout)

        # --- Smart upsert: "not found" on PUT → switch to POST ---
        if record.res_model == 'product.template' and record.method == 'put':
            if response.status_code not in (200, 201, 204) and (
                'not found' in response.text.lower() or 'stock item' in response.text.lower()
            ):
                response = requests.post(url, data=record.payload, headers=headers, timeout=timeout)

        # --- Check for fatal errors BEFORE raise_for_status ---
        if not response.ok:
            error_body = response.text or ''
            friendly_msg = ''
            try:
                err_json = json.loads(error_body)
                friendly_msg = err_json.get('message') or err_json.get('error') or err_json.get('title') or ''
            except Exception:
                pass
            error_string = friendly_msg or error_body

            if self._is_fatal_error(error_string):
                # Fatal: will never succeed. Mark done to unblock the queue.
                record.write({
                    'state': 'done',
                    'error_message': f'Skipped (fatal Sage error): {error_string[:500]}'
                })
                _logger.warning("Fatal Sage error for %s id=%s - skipped: %s",
                                record.res_model, record.res_id, error_string[:200])
                return

            # Non-fatal: let raise_for_status handle it for retry
            response.raise_for_status()

        # --- Success ---
        self._mark_done(record)
        self._on_success(record, response, api_url, headers, timeout)

    def _mark_done(self, record):
        record.write({'state': 'done', 'error_message': False})

    def _on_success(self, record, response, api_url, headers, timeout):
        """Handle post-success actions: write back Sage ref numbers, link products."""
        target_record = self.env[record.res_model].sudo().browse(record.res_id)
        if not target_record.exists():
            return

        vals = {}
        if hasattr(target_record, 'is_sage_synced'):
            vals['is_sage_synced'] = True

        resp_data = {}
        try:
            resp_data = response.json() if response.text else {}
        except Exception:
            pass

        if record.res_model in ('sale.order', 'purchase.order'):
            sage_no = resp_data.get('orderNumber')
            if sage_no and hasattr(target_record, 'sage_invoice_number') and not target_record.sage_invoice_number:
                vals['sage_invoice_number'] = sage_no

        elif record.res_model == 'account.move':
            sage_no = resp_data.get('invoiceNumber')
            if sage_no and hasattr(target_record, 'sage_invoice_no') and not target_record.sage_invoice_no:
                vals['sage_invoice_no'] = sage_no

        elif record.res_model == 'stock.picking' and record.endpoint == '/Purchase/orders/grv':
            grv_number = (resp_data.get('grvNumber') or resp_data.get('GrvNumber')
                          or resp_data.get('grv_number'))
            if grv_number and hasattr(target_record, 'purchase_id') and target_record.purchase_id:
                try:
                    target_record.purchase_id.with_context(
                        skip_sage_sync=True, skip_duplicate_check=True
                    ).write({'sage_grv_number': grv_number})
                except Exception:
                    pass

        elif record.res_model == 'account.account':
            account_link = resp_data.get('accountLink') or resp_data.get('AccountLink')
            if account_link and hasattr(target_record, 'sage_account_link') and not target_record.sage_account_link:
                vals['sage_account_link'] = account_link
        elif record.res_model == 'account.analytic.account':
            project_link = resp_data.get('projectLink') or resp_data.get('ProjectLink')
            if project_link and hasattr(target_record, 'sage_project_id') and not target_record.sage_project_id:
                vals['sage_project_id'] = project_link

        if vals:
            try:
                target_record.with_context(
                    skip_sage_sync=True, skip_duplicate_check=True
                ).write(vals)
            except Exception as e:
                _logger.error("Failed to write back sync vals %s to %s id=%s: %s",
                              vals, record.res_model, record.res_id, str(e))

        # Link product to warehouse on successful creation
        if record.res_model == 'product.template' and response.status_code == 201:
            self._link_product_to_warehouse(record, api_url, headers, timeout)

    def _link_product_to_warehouse(self, record, api_url, headers, timeout):
        """Ensure a product is linked to the Mstr warehouse in Sage."""
        try:
            payload_dict = json.loads(record.payload)
            item_code = payload_dict.get("code")
            if not item_code:
                return
            wh_url = f"{api_url.rstrip('/')}/Inventory/warehouse"
            wh_resp = requests.post(
                wh_url,
                json={"itemCode": item_code, "warehouseCode": "Mstr"},
                headers=headers, timeout=timeout
            )
            if wh_resp.status_code not in (200, 201) and not (
                wh_resp.status_code in (400, 500) and 'already' in wh_resp.text.lower()
            ):
                _logger.warning("Could not link product %s to warehouse Mstr: %s %s",
                                item_code, wh_resp.status_code, wh_resp.text[:200])
        except Exception as e:
            _logger.warning("Exception linking product to warehouse: %s", str(e))
