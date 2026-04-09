import logging
import subprocess

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # ------------------------------------------------------------------
    # Claude AI configuration
    # ------------------------------------------------------------------

    devops_claude_api_key = fields.Char(
        string='Claude API Key',
        config_parameter='pmb_devops.claude_api_key',
    )
    devops_claude_model = fields.Selection(
        [
            ('claude-opus-4-6-20250414', 'Claude Opus 4.6'),
            ('claude-sonnet-4-6-20250414', 'Claude Sonnet 4.6'),
            ('claude-sonnet-4-20250514', 'Claude Sonnet 4'),
        ],
        string='Modelo Claude',
        config_parameter='pmb_devops.claude_model',
        default='claude-opus-4-6-20250414',
    )
    devops_claude_installed = fields.Boolean(
        string='Claude CLI Instalado',
        compute='_compute_claude_status',
    )
    devops_claude_version = fields.Char(
        string='Claude CLI Version',
        compute='_compute_claude_status',
    )
    devops_claude_logged_in = fields.Boolean(
        string='Claude Autenticado',
        compute='_compute_claude_status',
    )
    devops_claude_auth_method = fields.Char(
        string='Metodo de Autenticacion',
        compute='_compute_claude_status',
    )
    devops_claude_status_message = fields.Char(
        string='Estado de Claude',
        compute='_compute_claude_status',
    )

    # ------------------------------------------------------------------
    # Backup configuration
    # ------------------------------------------------------------------

    devops_backup_path = fields.Char(
        string='Ruta de Backups',
        config_parameter='pmb_devops.backup_path',
        default='/var/backups/odoo',
    )
    devops_backup_retention_days = fields.Integer(
        string='Dias de Retencion',
        config_parameter='pmb_devops.backup_retention_days',
        default=7,
    )
    devops_auto_backup = fields.Boolean(
        string='Backup Automatico',
        config_parameter='pmb_devops.auto_backup',
        default=True,
    )

    # ------------------------------------------------------------------
    # Repository defaults
    # ------------------------------------------------------------------

    devops_default_repo_path = fields.Char(
        string='Ruta por Defecto de Repositorios',
        config_parameter='pmb_devops.default_repo_path',
        default='/opt/odooAL',
    )

    # ------------------------------------------------------------------
    # Computed: Claude CLI status
    # ------------------------------------------------------------------

    def _compute_claude_status(self):
        """Query Claude CLI auth status via the ai.assistant model."""
        status = {}
        try:
            status = self.env['devops.ai.assistant'].get_claude_auth_status()
        except Exception as e:
            _logger.warning("Error checking Claude status: %s", e)

        installed = status.get('installed', False)
        version = status.get('version', '')
        authenticated = status.get('authenticated', False)
        error = status.get('error', '')

        # Determine auth method
        api_key = self.env['ir.config_parameter'].sudo().get_param(
            'pmb_devops.claude_api_key', ''
        )
        if authenticated and api_key:
            auth_method = 'API Key'
        elif authenticated:
            auth_method = 'Claude CLI'
        else:
            auth_method = ''

        # Build status message
        if not installed:
            status_message = error or 'Claude CLI no instalado'
        elif not authenticated:
            status_message = error or 'Claude CLI instalado pero no autenticado'
        else:
            status_message = 'Claude CLI operativo'

        for rec in self:
            rec.devops_claude_installed = installed
            rec.devops_claude_version = version
            rec.devops_claude_logged_in = authenticated
            rec.devops_claude_auth_method = auth_method
            rec.devops_claude_status_message = status_message

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_open_claude_login(self):
        """Open the Claude login wizard."""
        return {
            'type': 'ir.actions.act_window',
            'name': 'Claude Login',
            'res_model': 'devops.claude.login.wizard',
            'view_mode': 'form',
            'target': 'new',
        }

    def action_test_claude_connection(self):
        """Test Claude CLI connection with a simple prompt (LOCAL subprocess)."""
        self.ensure_one()
        try:
            proc = subprocess.run(
                ['claude', '-p', 'Responde solo: OK', '--output-format', 'text'],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': 'Conexion exitosa',
                        'message': f'Claude respondio: {proc.stdout.strip()}',
                        'type': 'success',
                        'sticky': False,
                    },
                }
            else:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': 'Error de conexion',
                        'message': proc.stderr or 'Claude no respondio',
                        'type': 'danger',
                        'sticky': True,
                    },
                }
        except FileNotFoundError:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Claude CLI no encontrado',
                    'message': 'Claude CLI no esta instalado en el servidor.',
                    'type': 'danger',
                    'sticky': True,
                },
            }
        except subprocess.TimeoutExpired:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Timeout',
                    'message': 'Claude no respondio en 30 segundos.',
                    'type': 'warning',
                    'sticky': True,
                },
            }
        except Exception as e:
            _logger.exception("Error testing Claude connection")
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Error',
                    'message': str(e),
                    'type': 'danger',
                    'sticky': True,
                },
            }
