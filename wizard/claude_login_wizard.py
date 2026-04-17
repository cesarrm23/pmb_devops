import json
import logging
import subprocess

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class DevopsClaudeLoginWizard(models.TransientModel):
    _name = 'devops.claude.login.wizard'
    _description = 'Configuración de Claude CLI'

    state = fields.Selection([
        ('start', 'Inicio'),
        ('success', 'Éxito'),
        ('error', 'Error'),
    ], string='Estado', default='start', required=True)

    api_key = fields.Char(string='API Key')
    message = fields.Text(string='Mensaje')
    auth_method = fields.Selection([
        ('api_key', 'API Key'),
        ('cli', 'Claude CLI (ya autenticado)'),
    ], string='Método de Autenticación', default='api_key')

    # ── Default values ──────────────────────────────────────────────

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)

        # Check current authentication status
        auth_status = self.env['devops.ai.assistant'].get_claude_auth_status()
        existing_key = self.env['ir.config_parameter'].sudo().get_param(
            'pmb_devops.claude_api_key', '',
        )

        messages = []
        if auth_status.get('installed'):
            messages.append(
                f"Claude CLI instalado: {auth_status.get('version', 'si')}"
            )
            if auth_status.get('authenticated'):
                messages.append("Claude CLI autenticado correctamente.")
                res['auth_method'] = 'cli'
            else:
                messages.append("Claude CLI no autenticado.")
        else:
            messages.append(
                "Claude CLI no instalado. Se usará la API directamente."
            )

        if existing_key:
            messages.append("API Key configurada (ya guardada).")
            # Show masked key
            masked = existing_key[:8] + '...' + existing_key[-4:]
            res['api_key'] = masked
        else:
            messages.append("No hay API Key configurada.")

        res['message'] = '\n'.join(messages)
        return res

    # ── Actions ─────────────────────────────────────────────────────

    def action_save_api_key(self):
        """Validate and save the API key."""
        self.ensure_one()
        if not self.api_key:
            raise UserError("Ingrese una API Key.")

        # Don't save if it's a masked key (unchanged)
        if '...' in self.api_key:
            self.write({
                'state': 'success',
                'message': 'API Key sin cambios (ya guardada).',
            })
            return self._reopen()

        api_key = self.api_key.strip()

        # Validate key format
        if not api_key.startswith('sk-ant-'):
            raise UserError(
                "La API Key debe comenzar con 'sk-ant-'. "
                "Obtenga una en console.anthropic.com"
            )

        # Test the key
        test_ok = False
        test_message = ''

        # Try with Claude CLI first
        try:
            import os
            env = os.environ.copy()
            env['ANTHROPIC_API_KEY'] = api_key
            proc = subprocess.run(
                ['claude', '-p', 'Responde solo: OK', '--output-format', 'text'],
                capture_output=True, text=True, timeout=30,
                env=env,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                test_ok = True
                test_message = f'Validada con Claude CLI: {proc.stdout.strip()}'
        except FileNotFoundError:
            # Claude CLI not installed, try curl fallback
            _logger.info("Claude CLI not found, falling back to curl")
            try:
                payload = json.dumps({
                    'model': 'claude-haiku-4-5',
                    'max_tokens': 16,
                    'messages': [
                        {'role': 'user', 'content': 'Responde solo: OK'},
                    ],
                })
                proc = subprocess.run(
                    [
                        'curl', '-s',
                        'https://api.anthropic.com/v1/messages',
                        '-H', 'Content-Type: application/json',
                        '-H', f'x-api-key: {api_key}',
                        '-H', 'anthropic-version: 2023-06-01',
                        '-d', payload,
                    ],
                    capture_output=True, text=True, timeout=30,
                )
                if proc.returncode == 0 and proc.stdout:
                    response = json.loads(proc.stdout)
                    if response.get('content'):
                        test_ok = True
                        test_message = 'Validada con API (curl).'
                    elif response.get('error'):
                        test_message = (
                            f"Error de API: {response['error'].get('message', '')}"
                        )
                    else:
                        test_message = 'Respuesta inesperada de la API.'
                else:
                    test_message = f'Error en curl: {proc.stderr or "sin respuesta"}'
            except Exception as e:
                test_message = f'Error validando API key: {e}'
        except subprocess.TimeoutExpired:
            test_message = 'Timeout validando la API key.'
        except Exception as e:
            test_message = f'Error inesperado: {e}'

        if test_ok:
            # Save to config parameters
            self.env['ir.config_parameter'].sudo().set_param(
                'pmb_devops.claude_api_key', api_key,
            )
            self.write({
                'state': 'success',
                'message': f'API Key guardada correctamente.\n{test_message}',
            })
        else:
            self.write({
                'state': 'error',
                'message': f'No se pudo validar la API Key.\n{test_message}',
            })

        return self._reopen()

    def action_open_console(self):
        """Open the Anthropic console in a new browser tab."""
        return {
            'type': 'ir.actions.act_url',
            'url': 'https://console.anthropic.com/settings/keys',
            'target': 'new',
        }

    def action_open_terminal(self):
        """Open the DevOps terminal client action."""
        return {
            'type': 'ir.actions.client',
            'tag': 'devops_terminal',
        }

    # ── Helpers ─────────────────────────────────────────────────────

    def _reopen(self):
        """Return an action to reopen this wizard."""
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }
