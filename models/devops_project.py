import logging
import subprocess

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..utils import ssh_utils
from ..utils import git_utils

_logger = logging.getLogger(__name__)


class DevopsProject(models.Model):
    _name = 'devops.project'
    _description = 'Proyecto DevOps'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'name'

    # ---- Basic info ----
    name = fields.Char(string='Nombre', required=True, tracking=True)
    active = fields.Boolean(default=True)
    description = fields.Html(string='Descripción')
    color = fields.Integer(string='Color')

    # ---- Repository ----
    repo_path = fields.Char(string='Ruta del Repositorio', required=True, tracking=True)
    repo_url = fields.Char(
        string='URL del Repositorio', compute='_compute_repo_info',
        store=True, readonly=True,
    )
    repo_current_branch = fields.Char(
        string='Rama Actual', compute='_compute_repo_info',
        store=True, readonly=True,
    )

    # ---- Domain & Instances ----
    domain = fields.Char(string='Dominio', help='ej: cremara.com')
    instance_ids = fields.One2many('devops.instance', 'project_id', string='Instancias')
    production_instance_id = fields.Many2one('devops.instance', string='Instancia Producción')
    max_staging = fields.Integer(string='Max Staging', default=3)
    max_development = fields.Integer(string='Max Development', default=5)
    auto_destroy_hours = fields.Integer(string='Auto-destroy (horas)', default=24)
    enterprise_path = fields.Char(
        string='Ruta Enterprise Addons',
        help='Ruta a los addons enterprise (ej: /opt/odoo19Test/enterprise)',
    )

    # ---- Odoo service (DEPRECATED — will be replaced by instance fields) ----
    odoo_service_name = fields.Char(string='Nombre del Servicio', default='odoo19')
    odoo_config_path = fields.Char(string='Ruta Config Odoo')
    odoo_url = fields.Char(string='URL de Odoo', tracking=True)
    odoo_version = fields.Char(
        string='Versión Odoo', compute='_compute_odoo_version',
        store=True, readonly=True,
    )

    # ---- Database (DEPRECATED — will be replaced by instance fields) ----
    database_name = fields.Char(string='Base de Datos', tracking=True)

    # ---- Environment & state (DEPRECATED — will be replaced by instance fields) ----
    environment = fields.Selection([
        ('production', 'Producción'),
        ('staging', 'Staging'),
        ('development', 'Desarrollo'),
    ], string='Entorno', default='development', tracking=True)
    state = fields.Selection([
        ('draft', 'Borrador'),
        ('running', 'Ejecutando'),
        ('stopped', 'Detenido'),
        ('error', 'Error'),
    ], string='Estado', compute='_compute_state', store=True, readonly=True,
       default='draft',
    )
    last_status_check = fields.Datetime(string='Último Chequeo')

    # ---- Related records (One2many) ----
    branch_ids = fields.One2many('devops.branch', 'project_id', string='Ramas')
    build_ids = fields.One2many('devops.build', 'project_id', string='Builds')
    log_ids = fields.One2many('devops.log', 'project_id', string='Logs')
    backup_ids = fields.One2many('devops.backup', 'project_id', string='Backups')

    # ---- Counts ----
    branch_count = fields.Integer(
        string='Ramas', compute='_compute_counts',
    )
    build_count = fields.Integer(
        string='Builds', compute='_compute_counts',
    )
    backup_count = fields.Integer(
        string='Backups', compute='_compute_counts',
    )
    log_count = fields.Integer(
        string='Logs', compute='_compute_counts',
    )

    # ---- AI ----
    ai_api_key = fields.Char(
        string='API Key IA',
        groups='pmb_devops.group_devops_admin',
    )

    # ---- Branch config ----
    production_branch = fields.Char(string='Rama Producción', default='main')
    staging_branch = fields.Char(string='Rama Staging', default='staging')
    auto_deploy = fields.Boolean(string='Auto Deploy', default=False)

    # ---- Connection (NEW) ----
    connection_type = fields.Selection([
        ('local', 'Local'),
        ('ssh', 'SSH'),
    ], string='Tipo de Conexión', default='local', required=True)
    ssh_host = fields.Char(string='SSH Host')
    ssh_user = fields.Char(string='SSH Usuario')
    ssh_port = fields.Integer(string='SSH Puerto', default=22)
    ssh_key_path = fields.Char(string='Ruta Llave SSH')

    # ---- Members (NEW) ----
    member_ids = fields.One2many(
        'devops.project.member', 'project_id', string='Miembros',
    )

    # ------------------------------------------------------------------
    # Compute methods
    # ------------------------------------------------------------------

    @api.depends('repo_path')
    def _compute_repo_info(self):
        for rec in self:
            rec.repo_url = ''
            rec.repo_current_branch = ''
            if not rec.repo_path:
                continue
            # Remote URL
            try:
                result = ssh_utils.execute_command(
                    rec,
                    ['git', 'remote', 'get-url', 'origin'],
                    cwd=rec.repo_path,
                )
                if result.returncode == 0:
                    rec.repo_url = result.stdout.strip()
            except Exception as e:
                _logger.warning("Error obteniendo repo URL: %s", e)
            # Current branch
            try:
                rec.repo_current_branch = git_utils.git_current_branch(rec)
            except Exception as e:
                _logger.warning("Error obteniendo rama actual: %s", e)

    @api.depends('odoo_service_name')
    def _compute_state(self):
        for rec in self:
            if not rec.odoo_service_name:
                rec.state = 'draft'
                continue
            try:
                result = ssh_utils.execute_command(
                    rec,
                    ['systemctl', 'is-active', rec.odoo_service_name],
                )
                output = result.stdout.strip()
                if output == 'active':
                    rec.state = 'running'
                elif output in ('inactive', 'deactivating'):
                    rec.state = 'stopped'
                else:
                    rec.state = 'error'
            except Exception:
                rec.state = 'error'

    @api.depends('repo_path')
    def _compute_odoo_version(self):
        for rec in self:
            rec.odoo_version = '19.0'

    def _compute_counts(self):
        for rec in self:
            rec.branch_count = len(rec.branch_ids)
            rec.build_count = len(rec.build_ids)
            rec.backup_count = len(rec.backup_ids)
            rec.log_count = len(rec.log_ids)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_refresh_status(self):
        """Refresh project state and repo info."""
        self.ensure_one()
        self._compute_state()
        self._compute_repo_info()
        self.last_status_check = fields.Datetime.now()

    def action_restart_service(self):
        """Restart the Odoo service. NEVER stop, always restart."""
        self.ensure_one()
        if not self.odoo_service_name:
            raise UserError(_("No se ha configurado el nombre del servicio."))
        try:
            result = ssh_utils.execute_command(
                self,
                ['sudo', 'systemctl', 'restart', self.odoo_service_name],
                timeout=60,
            )
            if result.returncode != 0:
                raise UserError(
                    _("Error reiniciando servicio: %s") % result.stderr.strip()
                )
            self.message_post(
                body=_("Servicio '%s' reiniciado correctamente.") % self.odoo_service_name,
            )
        except subprocess.TimeoutExpired:
            raise UserError(_("Timeout reiniciando el servicio."))
        except UserError:
            raise
        except Exception as e:
            raise UserError(_("Error reiniciando servicio: %s") % str(e))
        self._compute_state()

    def action_sync_branches(self):
        """Fetch remotes and sync branch list."""
        self.ensure_one()
        if not self.repo_path:
            raise UserError(_("No se ha configurado la ruta del repositorio."))

        git_utils.git_fetch(self)
        branches_data = git_utils.git_list_branches(self)

        BranchModel = self.env['devops.branch']
        existing = {b.name: b for b in self.branch_ids}

        for bdata in branches_data:
            branch_name = bdata.get('name', '')
            if not branch_name:
                continue
            if branch_name in existing:
                existing[branch_name].write({
                    'last_commit_hash': bdata.get('hash', ''),
                    'last_commit_message': bdata.get('message', ''),
                    'last_commit_author': bdata.get('author', ''),
                    'last_commit_date': bdata.get('date'),
                    'is_remote': bdata.get('is_remote', False),
                })
            else:
                # Determine branch type
                branch_type = 'development'
                if branch_name == self.production_branch:
                    branch_type = 'production'
                elif branch_name == self.staging_branch:
                    branch_type = 'staging'

                BranchModel.create({
                    'project_id': self.id,
                    'name': branch_name,
                    'branch_type': branch_type,
                    'last_commit_hash': bdata.get('hash', ''),
                    'last_commit_message': bdata.get('message', ''),
                    'last_commit_author': bdata.get('author', ''),
                    'last_commit_date': bdata.get('date'),
                    'is_remote': bdata.get('is_remote', False),
                })

        # Remove branches that no longer exist
        current_names = {b.get('name') for b in branches_data if b.get('name')}
        to_delete = self.branch_ids.filtered(lambda b: b.name not in current_names)
        to_delete.unlink()

        self._compute_repo_info()
        self.message_post(
            body=_("Ramas sincronizadas: %d encontradas.") % len(branches_data),
        )

    def action_view_branches(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Ramas'),
            'res_model': 'devops.branch',
            'view_mode': 'list,form',
            'domain': [('project_id', '=', self.id)],
            'context': {'default_project_id': self.id},
        }

    def action_view_builds(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Builds'),
            'res_model': 'devops.build',
            'view_mode': 'list,form',
            'domain': [('project_id', '=', self.id)],
            'context': {'default_project_id': self.id},
        }

    def action_view_logs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Logs'),
            'res_model': 'devops.log',
            'view_mode': 'list,form',
            'domain': [('project_id', '=', self.id)],
            'context': {'default_project_id': self.id},
        }

    def action_view_backups(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Backups'),
            'res_model': 'devops.backup',
            'view_mode': 'list,form',
            'domain': [('project_id', '=', self.id)],
            'context': {'default_project_id': self.id},
        }

    def action_open_ai_assistant(self):
        """Open the AI assistant wizard for this project."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Asistente IA'),
            'res_model': 'devops.ai.assistant.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_project_id': self.id},
        }

    def action_create_backup(self):
        """Create a database backup using pg_dump."""
        self.ensure_one()
        if not self.database_name:
            raise UserError(_("No se ha configurado la base de datos."))

        timestamp = fields.Datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_name = f"{self.database_name}_{timestamp}"
        backup_dir = '/opt/backups'
        backup_file = f"{backup_dir}/{backup_name}.sql.gz"

        # Ensure backup directory exists
        ssh_utils.execute_command(
            self, ['mkdir', '-p', backup_dir],
        )

        # pg_dump piped through gzip
        cmd_str = (
            f"pg_dump -Fc {self.database_name} | gzip > {backup_file}"
        )
        try:
            result = ssh_utils.execute_command_shell(
                self, cmd_str, timeout=300,
            )
            if result.returncode != 0:
                raise UserError(
                    _("Error creando backup: %s") % result.stderr.strip()
                )
        except Exception as e:
            raise UserError(_("Error creando backup: %s") % str(e))

        # Create backup record
        self.env['devops.backup'].create({
            'project_id': self.id,
            'name': backup_name,
            'file_path': backup_file,
            'database_name': self.database_name,
            'state': 'done',
        })

        self.message_post(
            body=_("Backup creado: %s") % backup_name,
        )

    def action_fetch_logs(self):
        """Fetch recent journalctl logs for the Odoo service."""
        self.ensure_one()
        if not self.odoo_service_name:
            raise UserError(_("No se ha configurado el nombre del servicio."))

        try:
            result = ssh_utils.execute_command(
                self,
                [
                    'journalctl', '-u', self.odoo_service_name,
                    '--no-pager', '-n', '200', '--output', 'short-iso',
                ],
                timeout=30,
            )
            log_content = result.stdout if result.returncode == 0 else result.stderr
        except Exception as e:
            log_content = str(e)

        self.env['devops.log'].create({
            'project_id': self.id,
            'name': f"Logs {self.odoo_service_name} - {fields.Datetime.now()}",
            'log_type': 'service',
            'content': log_content,
        })

        self.message_post(body=_("Logs del servicio obtenidos."))
