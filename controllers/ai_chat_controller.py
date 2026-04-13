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

        # Determine working directory and SSH config
        cwd = '/opt/odooAL'
        ssh_config = None
        project = None
        if instance_id:
            try:
                instance = request.env['devops.instance'].browse(instance_id)
                if instance.exists():
                    project = instance.project_id
                    if instance.instance_path and os.path.isdir(instance.instance_path):
                        cwd = instance.instance_path
                    elif project.repo_path and os.path.isdir(project.repo_path):
                        cwd = project.repo_path
            except Exception:
                pass
        elif project_id:
            try:
                project = request.env['devops.project'].browse(project_id)
                if project.exists() and project.repo_path and os.path.isdir(project.repo_path):
                    cwd = project.repo_path
            except Exception:
                pass

        # SSH projects: unique cwd + pass SSH info to ws_terminal
        if project and project.connection_type == 'ssh' and project.ssh_host:
            cwd = os.path.join('/opt/odooAL/.pmb_ssh', f'project_{project.id}')
            os.makedirs(cwd, exist_ok=True)
            ssh_config = {
                'host': project.ssh_host,
                'user': project.ssh_user or 'root',
                'port': project.ssh_port or 22,
                'key': project.ssh_key_path or '',
                'remote_cwd': project.repo_path or '/opt',
            }

        # Determine instance type for isolation
        instance_type = 'production'
        if instance_id:
            try:
                instance = request.env['devops.instance'].browse(instance_id)
                if instance.exists():
                    instance_type = instance.instance_type or 'production'
            except Exception:
                pass

        # Generate token
        token = secrets.token_urlsafe(32)
        token_data = {
            'uid': uid,
            'cmd': 'claude',
            'cwd': cwd,
            'instance_type': instance_type,
            'allowed_path': cwd,
            'created': time.time(),
        }
        if ssh_config:
            token_data['ssh'] = ssh_config

        token_path = os.path.join(TOKEN_DIR, token)
        with open(token_path, 'w') as f:
            json.dump(token_data, f)

        _logger.info("AI token generated: uid=%s, cwd=%s, type=%s", uid, cwd, instance_type)

        return {
            'token': token,
            'ws_url': '/ws/terminal',
        }
