import json
import logging
import os
import secrets
import time

from odoo import fields, http
from odoo.http import request

_logger = logging.getLogger(__name__)

TOKEN_DIR = '/tmp/pmb_ws_tokens'


class DevopsAiChatController(http.Controller):

    @http.route('/devops/ai/token', type='json', auth='user')
    def ai_token(self, instance_id=None, project_id=None, cmd_type='claude', force_new=False):
        """Generate a one-time token for WebSocket terminal authentication.

        The token is stored as a file in TOKEN_DIR, read by the WS bridge.
        """
        uid = request.env.uid
        os.makedirs(TOKEN_DIR, exist_ok=True)

        # ---- Security: validate instance state ----
        instance = None
        project = None
        if instance_id:
            instance = request.env['devops.instance'].browse(instance_id)
            if not instance.exists():
                return {'error': 'Instancia no encontrada'}
            project = instance.project_id
            # Block terminal for stopped staging/dev (production always allowed)
            if instance.instance_type != 'production' and instance.state != 'running':
                return {'error': 'La instancia debe estar en ejecucion para usar el terminal'}
        elif project_id:
            project = request.env['devops.project'].browse(project_id)
            if not project.exists():
                return {'error': 'Proyecto no encontrado'}

        # Touch activity
        if instance and instance.state == 'running':
            try:
                instance.sudo().write({'last_activity': fields.Datetime.now()})
            except Exception:
                pass

        # ---- Determine working directory ----
        ssh_config = None
        is_ssh = project and project.connection_type == 'ssh' and project.ssh_host

        if is_ssh:
            # SSH: never use local paths, always remote
            remote_cwd = project.repo_path or '/opt'
            if instance and instance.instance_path:
                remote_cwd = instance.instance_path
            cwd = os.path.join('/opt/odooAL/.pmb_ssh', f'instance_{instance_id or "proj_" + str(project.id)}')
            os.makedirs(cwd, exist_ok=True)
        elif instance and instance.instance_type != 'production':
            # Staging/dev local: MUST use instance_path, never fall back to production
            if instance.instance_path and os.path.isdir(instance.instance_path):
                cwd = instance.instance_path
            else:
                return {'error': f'Directorio de instancia no encontrado: {instance.instance_path}'}
        elif instance and instance.instance_type == 'production':
            # Production: use repo_path or instance_path
            cwd = project.repo_path or instance.instance_path or '/opt/odooAL'
            if not os.path.isdir(cwd):
                cwd = '/opt/odooAL'
        elif project:
            # Project-level (no instance): use repo_path
            cwd = project.repo_path or '/opt/odooAL'
            if not os.path.isdir(cwd):
                cwd = '/opt/odooAL'
        else:
            cwd = '/opt/odooAL'

        # SSH projects: build SSH config
        if is_ssh:
            # Detect instance OS user for staging/dev
            instance_user = ''
            if instance and instance.instance_type != 'production' and instance.service_name:
                try:
                    from ..utils import ssh_utils
                    r = ssh_utils.execute_command_shell(
                        project,
                        f"systemctl show {instance.service_name} -p User --value 2>/dev/null",
                    )
                    u = r.stdout.strip() if r.returncode == 0 else ''
                    if u and u != 'root':
                        instance_user = u
                except Exception:
                    pass

            ssh_config = {
                'host': project.ssh_host,
                'user': project.ssh_user or 'root',
                'port': project.ssh_port or 22,
                'key': project.ssh_key_path or '',
                'remote_cwd': remote_cwd,
                'instance_user': instance_user,
            }

        # Determine instance type for isolation
        instance_type = instance.instance_type if instance else 'production'

        # Validate cmd_type
        if cmd_type not in ('claude', 'shell'):
            cmd_type = 'claude'

        # Generate token
        token = secrets.token_urlsafe(32)
        token_data = {
            'uid': uid,
            'cmd': cmd_type,
            'cwd': cwd,
            'instance_type': instance_type,
            'allowed_path': cwd,
            'created': time.time(),
            'force_new': bool(force_new),
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
