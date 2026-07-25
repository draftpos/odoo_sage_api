import logging
import requests
from odoo import models, fields, api

_logger = logging.getLogger(__name__)

class ResUsers(models.Model):
    _inherit = 'res.users'

    sage_agent_id = fields.Integer(string="Sage Agent ID", copy=False)

    def _push_user_to_sage_as_agent(self):
        """Push this Odoo user to Sage as a Sales Agent and save the returned ID."""
        api_url = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.api_url', default='')
        enabled = self.env['ir.config_parameter'].sudo().get_param('havano_sage_sync.enabled', default='True')
        if not api_url or str(enabled).lower() != 'true':
            return

        for user in self:
            # Skip system/portal users
            if user.login in ('__system__', 'public', '__public__') or not user.name:
                continue
            # Skip if already mapped to a Sage agent
            if user.sage_agent_id:
                continue

            try:
                url = f"{api_url.rstrip('/')}/Agents"
                # Use login as code (max safe length) and full name as name
                code = (user.login or user.name)[:50]
                payload = {"code": code, "name": user.name}
                response = requests.post(
                    url, json=payload,
                    headers={"Content-Type": "application/json", "Connection": "close"},
                    timeout=10
                )
                if response.status_code in (200, 201):
                    data = response.json()
                    new_sage_id = data.get('id')
                    if new_sage_id:
                        user.with_context(skip_sage_sync=True).write({'sage_agent_id': new_sage_id})
                        _logger.info("Pushed Odoo user '%s' to Sage as Agent ID %s", user.name, new_sage_id)
                else:
                    _logger.warning("Failed to push user '%s' to Sage Agents: HTTP %s %s", user.name, response.status_code, response.text[:200])
            except Exception as e:
                _logger.warning("Error pushing user '%s' to Sage Agents: %s", user.name, str(e))

    @api.model_create_multi
    def create(self, vals_list):
        users = super(ResUsers, self).create(vals_list)
        if not self.env.context.get('skip_sage_sync'):
            users._push_user_to_sage_as_agent()
        return users

    def write(self, vals):
        res = super(ResUsers, self).write(vals)
        # If the name changed and user has no sage_agent_id yet, try to push them now
        if not self.env.context.get('skip_sage_sync') and 'name' in vals:
            for user in self:
                if not user.sage_agent_id:
                    user._push_user_to_sage_as_agent()
        return res
