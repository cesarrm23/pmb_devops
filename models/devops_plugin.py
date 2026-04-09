import json
import logging
import subprocess

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Official / well-known Claude Code plugins (MCP servers)
KNOWN_PLUGINS = [
    {
        'name': 'Filesystem',
        'package_name': 'filesystem',
        'description': 'Acceso al sistema de archivos local',
        'category': 'filesystem',
    },
    {
        'name': 'GitHub',
        'package_name': 'github',
        'description': 'Integración con GitHub (issues, PRs, repos)',
        'category': 'git',
    },
    {
        'name': 'PostgreSQL',
        'package_name': 'postgres',
        'description': 'Acceso directo a bases de datos PostgreSQL',
        'category': 'database',
    },
    {
        'name': 'Puppeteer',
        'package_name': 'puppeteer',
        'description': 'Automatización de navegador web',
        'category': 'web',
    },
    {
        'name': 'Brave Search',
        'package_name': 'brave-search',
        'description': 'Búsqueda web con Brave',
        'category': 'web',
    },
    {
        'name': 'Memory',
        'package_name': 'memory',
        'description': 'Memoria persistente entre sesiones',
        'category': 'utility',
    },
    {
        'name': 'Fetch',
        'package_name': 'fetch',
        'description': 'HTTP fetch para APIs y páginas web',
        'category': 'web',
    },
    {
        'name': 'Sequential Thinking',
        'package_name': 'sequential-thinking',
        'description': 'Razonamiento paso a paso mejorado',
        'category': 'ai',
    },
]


class DevopsPlugin(models.Model):
    _name = 'devops.plugin'
    _description = 'Plugin de Claude Code (MCP)'
    _order = 'category, name'
    _rec_name = 'name'

    name = fields.Char(string='Nombre', required=True)
    package_name = fields.Char(string='Nombre del Paquete', required=True)
    description = fields.Text(string='Descripción')
    category = fields.Selection([
        ('filesystem', 'Sistema de Archivos'),
        ('git', 'Git / SCM'),
        ('database', 'Base de Datos'),
        ('web', 'Web / HTTP'),
        ('ai', 'IA / ML'),
        ('monitoring', 'Monitoreo'),
        ('utility', 'Utilidad'),
        ('other', 'Otro'),
    ], string='Categoría', default='other')
    is_installed = fields.Boolean(
        string='Instalado', compute='_compute_installed',
    )
    is_enabled = fields.Boolean(string='Habilitado', default=True)
    version = fields.Char(string='Versión')
    use_in_staging = fields.Boolean(string='Usar en Staging', default=True)
    use_in_production = fields.Boolean(string='Usar en Producción', default=False)
    use_in_development = fields.Boolean(string='Usar en Development', default=True)

    _unique_package = models.Constraint(
        'UNIQUE(package_name)',
        'Ya existe un plugin con este nombre de paquete.',
    )

    # ------------------------------------------------------------------
    # Computed
    # ------------------------------------------------------------------

    def _compute_installed(self):
        """Check if each plugin is in the `claude mcp list` output.

        Runs LOCALLY, not via SSH.
        """
        installed_names = set()
        try:
            proc = subprocess.run(
                ['claude', 'mcp', 'list'],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if proc.returncode == 0:
                output = proc.stdout.strip()
                # Parse output - each line typically: "name: description" or JSON
                for line in output.split('\n'):
                    line = line.strip()
                    if not line:
                        continue
                    # Try to extract plugin name from line
                    if ':' in line:
                        name_part = line.split(':')[0].strip().lower()
                        installed_names.add(name_part)
                    elif line:
                        installed_names.add(line.strip().lower())
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception) as e:
            _logger.debug("Could not list MCP plugins: %s", e)

        for rec in self:
            rec.is_installed = (rec.package_name or '').lower() in installed_names

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_install(self):
        """Install plugin via `claude mcp add`. Runs LOCALLY."""
        self.ensure_one()
        try:
            proc = subprocess.run(
                ['claude', 'mcp', 'add', self.package_name],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.returncode != 0:
                raise UserError(
                    f"Error instalando plugin '{self.package_name}':\n"
                    f"{proc.stderr or proc.stdout}"
                )
            _logger.info("Plugin instalado: %s", self.package_name)
            self.invalidate_recordset(['is_installed'])
        except FileNotFoundError:
            raise UserError(
                "Claude CLI no encontrado. Instale claude para gestionar plugins."
            )
        except subprocess.TimeoutExpired:
            raise UserError("Timeout instalando plugin.")

    def action_uninstall(self):
        """Uninstall plugin via `claude mcp remove`. Runs LOCALLY."""
        self.ensure_one()
        try:
            proc = subprocess.run(
                ['claude', 'mcp', 'remove', self.package_name],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode != 0:
                raise UserError(
                    f"Error desinstalando plugin '{self.package_name}':\n"
                    f"{proc.stderr or proc.stdout}"
                )
            _logger.info("Plugin desinstalado: %s", self.package_name)
            self.invalidate_recordset(['is_installed'])
        except FileNotFoundError:
            raise UserError(
                "Claude CLI no encontrado. Instale claude para gestionar plugins."
            )
        except subprocess.TimeoutExpired:
            raise UserError("Timeout desinstalando plugin.")

    @api.model
    def action_sync_plugins(self):
        """Create records for known/official plugins if they don't exist yet."""
        existing = {
            rec.package_name: rec
            for rec in self.search([])
        }
        created = 0
        for plugin_data in KNOWN_PLUGINS:
            pkg = plugin_data['package_name']
            if pkg not in existing:
                self.create({
                    'name': plugin_data['name'],
                    'package_name': pkg,
                    'description': plugin_data.get('description', ''),
                    'category': plugin_data.get('category', 'other'),
                })
                created += 1
                _logger.info("Plugin sincronizado: %s", pkg)

        if created:
            _logger.info("Sincronizados %d plugins nuevos", created)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Plugins sincronizados',
                'message': f'{created} plugins nuevos agregados.',
                'type': 'success',
                'sticky': False,
            },
        }
