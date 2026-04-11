import json
import logging
import os
import secrets
import time

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

TOKEN_DIR = '/tmp/pmb_ws_tokens'


class DevopsAiChatController(http.Controller):

    @http.route('/devops/ai/token', type='json', auth='user')
    def ai_token(self, instance_id=None, project_id=None):
        """Generate a one-time token for WebSocket terminal authentication.

        The token is stored as a file in TOKEN_DIR, read by the WS bridge.
        """
        uid = request.env.uid
        os.makedirs(TOKEN_DIR, exist_ok=True)

        # Determine working directory
        cwd = '/opt/odooAL'
        if instance_id:
            try:
                instance = request.env['devops.instance'].browse(instance_id)
                if instance.exists():
                    if instance.instance_path and os.path.isdir(instance.instance_path):
                        cwd = instance.instance_path
                    elif instance.project_id.repo_path and os.path.isdir(instance.project_id.repo_path):
                        cwd = instance.project_id.repo_path
            except Exception:
                pass
        elif project_id:
            try:
                project = request.env['devops.project'].browse(project_id)
                if project.exists() and project.repo_path and os.path.isdir(project.repo_path):
                    cwd = project.repo_path
            except Exception:
                pass

        # Generate token
        token = secrets.token_urlsafe(32)
        token_data = {
            'uid': uid,
            'cmd': 'claude',
            'cwd': cwd,
            'created': time.time(),
        }

        token_path = os.path.join(TOKEN_DIR, token)
        with open(token_path, 'w') as f:
            json.dump(token_data, f)

        _logger.info("AI token generated: uid=%s, cwd=%s", uid, cwd)

        return {
            'token': token,
            'ws_url': '/ws/terminal',
        }
