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
    subdomain_base = fields.Char(string='Dominio para subdominios',
        help='Dominio base para staging/dev. Ej: maha.com.mx. Si vacío, usa el dominio principal.')
    instance_ids = fields.One2many('devops.instance', 'project_id', string='Instancias')
    production_instance_id = fields.Many2one('devops.instance', string='Instancia Producción')
    max_staging = fields.Integer(string='Max Staging', default=3)
    max_development = fields.Integer(string='Max Development', default=5)
    auto_destroy_hours = fields.Integer(string='Auto-destroy (horas)', default=24)
    odoo_project_id = fields.Many2one('project.project', string='Proyecto Odoo',
        help='Proyecto de Odoo para crear tareas desde transcripciones')
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

    # ---- Server metrics (updated by cron) ----
    server_metrics = fields.Text(string='Server Metrics JSON')
    server_metrics_updated = fields.Datetime(string='Metrics Updated')

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

    # ---- GitHub OAuth ----
    github_client_id = fields.Char(string='GitHub Client ID')
    github_client_secret = fields.Char(string='GitHub Client Secret')

    # ---- Production Odoo sync (XML-RPC) ----
    sync_tasks_to_production = fields.Boolean(
        string='Sincronizar tareas con producción',
        help='Sincroniza tareas creadas/modificadas en pmb_devops con la BD de Odoo en producción vía XML-RPC',
    )
    production_admin_login = fields.Char(string='Login admin producción', help='Usuario admin del Odoo remoto (XML-RPC)')
    production_admin_password = fields.Char(string='Password admin producción', help='Password admin del Odoo remoto (XML-RPC)')
    production_project_id_remote = fields.Integer(
        string='ID proyecto remoto',
        help='ID del project.project en la base de datos remota donde se crean las tareas sincronizadas',
    )

    # ---- Post-clone script ----
    post_clone_script = fields.Text(
        string='Script Post-Clonacion',
        help='Python que se ejecuta en el odoo-bin shell de la instancia clonada (staging/dev) '
             'despues del SQL por defecto (web.base.url, mail/crons off). '
             'Vars disponibles: env, instance_name, instance_type, domain, port, db_name, service_name. '
             'Commit automatico al final.',
    )

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

    def action_upgrade_claude_cli(self):
        """Upgrade @anthropic-ai/claude-code to latest via npm on this host.

        Returns a dict with status, version and stdout/stderr so the caller
        can surface the result. Host-global: one upgrade covers every
        instance sharing the host.
        """
        self.ensure_one()
        try:
            result = ssh_utils.execute_command_shell(
                self,
                'npm install -g @anthropic-ai/claude-code@latest 2>&1 && '
                'claude --version',
                timeout=300,
            )
            stdout = (result.stdout or '').strip()
            stderr = (result.stderr or '').strip()
            version = ''
            for line in reversed(stdout.splitlines()):
                line = line.strip()
                if line and line[0].isdigit():
                    version = line
                    break
            ok = result.returncode == 0 and bool(version)
            if not ok:
                _logger.warning(
                    "Project %s: claude upgrade failed rc=%s stderr=%s",
                    self.name, result.returncode, stderr[:500],
                )
                raise UserError(_(
                    "Error actualizando Claude CLI (rc=%s):\n%s"
                ) % (result.returncode, stderr or stdout))
            self.message_post(
                body=_("Claude CLI actualizado a %s en %s.") % (version, self.ssh_host or 'local'),
            )
            return {'status': 'ok', 'version': version, 'stdout': stdout, 'stderr': stderr}
        except subprocess.TimeoutExpired:
            raise UserError(_("Timeout actualizando Claude CLI."))
        except UserError:
            raise
        except Exception as e:
            raise UserError(_("Error actualizando Claude CLI: %s") % str(e))

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

    @api.model
    def _cron_collect_server_metrics(self):
        """Collect disk, memory, CPU metrics for all projects."""
        import json
        for project in self.search([]):
            try:
                metrics = self._collect_metrics(project)
                project.write({
                    'server_metrics': json.dumps(metrics),
                    'server_metrics_updated': fields.Datetime.now(),
                })
                project._check_metrics_thresholds(metrics)
            except Exception as e:
                _logger.warning("Metrics collection failed for %s: %s", project.name, e)

    def _check_metrics_thresholds(self, metrics):
        """Create an activity alert if any metric exceeds 80%."""
        self.ensure_one()
        alerts = []

        # Disk > 80%
        disk_pct = float(metrics.get('disk', {}).get('percent', 0))
        if disk_pct > 80:
            alerts.append(_("Disk usage at %s%%") % disk_pct)

        # Memory > 80%
        mem_pct = float(metrics.get('memory', {}).get('percent', 0))
        if mem_pct > 80:
            alerts.append(_("Memory usage at %s%%") % mem_pct)

        # CPU load1 > 80% of cores
        cpu = metrics.get('cpu', {})
        cores = cpu.get('cores', 1)
        load1 = cpu.get('load1', 0)
        if cores and load1 > (cores * 0.8):
            alerts.append(
                _("CPU load1 %(load)s exceeds 80%% of %(cores)s cores")
                % {'load': load1, 'cores': cores}
            )

        if not alerts:
            return

        # Check for existing active alert activity (not done) to avoid spam
        existing = self.env['mail.activity'].search([
            ('res_model', '=', self._name),
            ('res_id', '=', self.id),
            ('user_id', '=', 2),
            ('summary', 'like', 'Server Alert'),
        ], limit=1)
        if existing:
            return

        # Create alert activity for admin (uid=2)
        self.activity_schedule(
            'mail.mail_activity_data_todo',
            user_id=2,
            summary=_("Server Alert: %s") % self.name,
            note=_("Thresholds exceeded:<br/>%s") % '<br/>'.join(alerts),
            date_deadline=fields.Date.today(),
        )

    def _collect_metrics(self, project):
        """Collect system metrics via local commands or SSH."""
        import json
        metrics = {}

        # Disk
        result = ssh_utils.execute_command(project, [
            'df', '-B1', '--output=size,used,avail,pcent', '/',
        ], timeout=5)
        if result.returncode == 0:
            lines = result.stdout.strip().split('\n')
            if len(lines) >= 2:
                parts = lines[1].split()
                if len(parts) >= 4:
                    metrics['disk'] = {
                        'total': int(parts[0]),
                        'used': int(parts[1]),
                        'free': int(parts[2]),
                        'percent': parts[3].replace('%', ''),
                    }

        # Memory
        result = ssh_utils.execute_command(project, [
            'free', '-b',
        ], timeout=5)
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if line.startswith('Mem:'):
                    parts = line.split()
                    if len(parts) >= 4:
                        metrics['memory'] = {
                            'total': int(parts[1]),
                            'used': int(parts[2]),
                            'free': int(parts[3]),
                            'percent': round(int(parts[2]) / int(parts[1]) * 100, 1) if int(parts[1]) else 0,
                        }

        # CPU load
        result = ssh_utils.execute_command(project, [
            'cat', '/proc/loadavg',
        ], timeout=5)
        if result.returncode == 0:
            parts = result.stdout.strip().split()
            if len(parts) >= 3:
                metrics['cpu'] = {
                    'load1': float(parts[0]),
                    'load5': float(parts[1]),
                    'load15': float(parts[2]),
                }
        # CPU count
        result = ssh_utils.execute_command(project, ['nproc'], timeout=5)
        if result.returncode == 0:
            metrics.setdefault('cpu', {})['cores'] = int(result.stdout.strip())

        # Uptime
        result = ssh_utils.execute_command(project, ['uptime', '-p'], timeout=5)
        if result.returncode == 0:
            metrics['uptime'] = result.stdout.strip()

        # Hostname
        result = ssh_utils.execute_command(project, ['hostname'], timeout=5)
        if result.returncode == 0:
            metrics['hostname'] = result.stdout.strip()

        # IP address
        result = ssh_utils.execute_command(project, ['hostname', '-I'], timeout=5)
        if result.returncode == 0:
            ips = result.stdout.strip().split()
            metrics['ip'] = ips[0] if ips else ''

        return metrics

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

    # ---- Production sync helpers (XML-RPC) ----
    def _get_production_xmlrpc(self):
        """Authenticate against the remote Odoo and return (uid, models, db, login, password).
        Returns None if not configured or auth fails."""
        self.ensure_one()
        if not self.sync_tasks_to_production:
            return None
        if not self.production_admin_login or not self.production_admin_password:
            _logger.warning("Project %s: production sync enabled but credentials missing", self.name)
            return None
        # Determine connection URL — for SSH projects use the domain, for local use localhost
        if self.connection_type == 'ssh' and self.ssh_host:
            url = f'https://{self.domain or self.ssh_host}'
        else:
            url = f'https://{self.domain}' if self.domain else 'http://localhost:8069'
        # Get database name from production instance
        prod = self.instance_ids.filtered(lambda i: i.instance_type == 'production')
        db = (prod and prod[0].database_name) or self.database_name
        if not db:
            _logger.warning("Project %s: no production database name", self.name)
            return None
        try:
            import xmlrpc.client
            common = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/common', allow_none=True)
            uid = common.authenticate(db, self.production_admin_login, self.production_admin_password, {})
            if not uid:
                _logger.warning("Project %s: production XML-RPC auth failed", self.name)
                return None
            models_proxy = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/object', allow_none=True)
            return (uid, models_proxy, db, self.production_admin_login, self.production_admin_password)
        except Exception as e:
            _logger.warning("Project %s: production XML-RPC error: %s", self.name, e)
            return None

    def _resolve_remote_stage_id(self, conn, local_stage, remote_project_id):
        """Return the remote project.task.type ID for a local stage, creating the mapping on demand.

        Handles deletion scenarios:
          - If the cached mapping points to a remote stage that no longer exists,
            drop the stale mapping and re-resolve (search by name, then create).
          - If the remote project itself has been replaced, this still works because
            mappings are scoped per (devops.project, local_stage_id).
        """
        self.ensure_one()
        if not local_stage:
            return False
        uid, models_proxy, db, login, password = conn
        StageMap = self.env['devops.stage.mapping'].sudo()
        # Check cached mapping
        mapping = StageMap.search([
            ('project_id', '=', self.id),
            ('local_stage_id', '=', local_stage.id),
        ], limit=1)
        if mapping:
            # Verify the cached remote stage still exists
            try:
                exists = models_proxy.execute_kw(
                    db, uid, password, 'project.task.type', 'search_count',
                    [[('id', '=', mapping.remote_stage_id)]],
                )
                if exists:
                    return mapping.remote_stage_id
                # Stale — remote stage was deleted. Drop mapping and fall through.
                _logger.info("Project %s: remote stage %s no longer exists, re-resolving",
                             self.name, mapping.remote_stage_id)
                mapping.unlink()
            except Exception as e:
                _logger.warning("Project %s: error verifying remote stage: %s", self.name, e)
                # If we can't verify, trust the cache (avoid breaking sync on transient errors)
                return mapping.remote_stage_id
        # Either no mapping existed, or it was stale — create a fresh one
        try:
            existing = models_proxy.execute_kw(
                db, uid, password, 'project.task.type', 'search',
                [[('name', '=', local_stage.name), ('project_ids', 'in', remote_project_id)]],
                {'limit': 1},
            )
            if existing:
                remote_stage_id = existing[0]
            else:
                remote_stage_id = models_proxy.execute_kw(
                    db, uid, password, 'project.task.type', 'create',
                    [{
                        'name': local_stage.name,
                        'sequence': local_stage.sequence or 10,
                        'project_ids': [(4, remote_project_id)],
                        'fold': local_stage.fold or False,
                    }],
                )
            # Persist the (new) mapping
            StageMap.create({
                'project_id': self.id,
                'local_stage_id': local_stage.id,
                'remote_stage_id': remote_stage_id,
                'name_snapshot': local_stage.name,
            })
            _logger.info("Project %s: mapped stage '%s' local=%s ↔ remote=%s",
                         self.name, local_stage.name, local_stage.id, remote_stage_id)
            return remote_stage_id
        except Exception as e:
            _logger.warning("Project %s: failed to resolve remote stage '%s': %s",
                            self.name, local_stage.name, e)
            return False

    def _sync_task_create_to_production(self, task):
        """Create a matching task in the remote Odoo and store the remote ID."""
        self.ensure_one()
        conn = self._get_production_xmlrpc()
        if not conn:
            return
        uid, models_proxy, db, login, password = conn
        # Determine (and validate) target project on remote
        remote_project_id = self.production_project_id_remote
        if remote_project_id:
            try:
                exists = models_proxy.execute_kw(
                    db, uid, password, 'project.project', 'search_count',
                    [[('id', '=', remote_project_id)]],
                )
                if not exists:
                    _logger.info("Project %s: remote project %s deleted, resetting + recreating",
                                 self.name, remote_project_id)
                    # Clear the stale pointer AND any mappings tied to the dead remote
                    self.env['devops.stage.mapping'].sudo().search([
                        ('project_id', '=', self.id),
                    ]).unlink()
                    # Also clear pmb_remote_task_id on all local tasks — their remote
                    # parents are gone too, so we must re-create them.
                    if self.odoo_project_id:
                        self.env['project.task'].sudo().search([
                            ('project_id', '=', self.odoo_project_id.id),
                            ('pmb_remote_task_id', '!=', 0),
                        ]).write({'pmb_remote_task_id': 0})
                    self.sudo().write({'production_project_id_remote': 0})
                    remote_project_id = 0
            except Exception as e:
                _logger.warning("Project %s: error validating remote project: %s", self.name, e)
        if not remote_project_id:
            try:
                remote_project_id = models_proxy.execute_kw(
                    db, uid, password, 'project.project', 'create',
                    [{'name': f'[DevOps] {self.name}'}],
                )
                self.sudo().write({'production_project_id_remote': remote_project_id})
                _logger.info("Project %s: auto-created remote project id=%s", self.name, remote_project_id)
            except Exception as e:
                _logger.warning("Project %s: failed to create remote project: %s", self.name, e)
                return
        # Create task on remote
        try:
            desc_str = str(task.description) if task.description else False
            vals = {
                'name': task.name,
                'project_id': remote_project_id,
                'description': desc_str,
            }
            if task.date_deadline:
                vals['date_deadline'] = str(task.date_deadline)
            if task.priority:
                vals['priority'] = task.priority
            if task.stage_id:
                remote_stage_id = self._resolve_remote_stage_id(conn, task.stage_id, remote_project_id)
                if remote_stage_id:
                    vals['stage_id'] = remote_stage_id
            if task.user_ids:
                remote_user_ids = self._translate_local_users_to_remote(conn, task.user_ids.ids)
                if remote_user_ids:
                    vals['user_ids'] = [(6, 0, remote_user_ids)]
            remote_task_id = models_proxy.execute_kw(
                db, uid, password, 'project.task', 'create', [vals],
            )
            # Persist remote ID on local task via dedicated field
            task.sudo().write({'pmb_remote_task_id': remote_task_id})
            _logger.info("Project %s: synced task %s → remote id=%s", self.name, task.name, remote_task_id)
            return remote_task_id
        except Exception as e:
            _logger.warning("Project %s: failed to sync task to production: %s", self.name, e)
            return None

    @api.model
    def _propagate_claude_model_to_all(self, target_model='claude-opus-4-7'):
        """Propagate the Claude model param to every project's every instance DB.

        For each project with production admin creds, connects via XML-RPC to each
        instance DB (production + staging + dev) and updates every ir.config_parameter
        matching '%claude%model%' to target_model.

        Returns a dict summary: {project_name: {db_name: status}}.
        """
        import xmlrpc.client
        summary = {}
        projects = self.search([('production_admin_login', '!=', False)])
        for project in projects:
            project_summary = {}
            if not project.production_admin_password:
                project_summary['_error'] = 'No password configured'
                summary[project.name] = project_summary
                continue
            # Determine base URL for remote or localhost for local projects
            if project.connection_type == 'ssh' and project.ssh_host:
                base_url = f'https://{project.domain or project.ssh_host}'
            else:
                base_url = f'https://{project.domain}' if project.domain else 'http://localhost:8069'
            login = project.production_admin_login
            password = project.production_admin_password
            # Derive the per-project config key convention: <slug>_odoo_sh.claude_model
            slug = (project.name or '').lower().strip().replace(' ', '_').replace('-', '_')
            project_key = f'{slug}_odoo_sh.claude_model' if slug else None
            # Iterate every instance DB (production + staging + development)
            for instance in project.instance_ids:
                db = instance.database_name
                if not db:
                    continue
                try:
                    common = xmlrpc.client.ServerProxy(f'{base_url}/xmlrpc/2/common', allow_none=True)
                    uid = common.authenticate(db, login, password, {})
                    if not uid:
                        project_summary[db] = 'auth failed'
                        continue
                    proxy = xmlrpc.client.ServerProxy(f'{base_url}/xmlrpc/2/object', allow_none=True)
                    # 1) Update every existing *claude*model* param
                    ids = proxy.execute_kw(db, uid, password, 'ir.config_parameter', 'search',
                                           [[('key', 'ilike', 'claude%model')]])
                    if ids:
                        proxy.execute_kw(db, uid, password, 'ir.config_parameter', 'write',
                                         [ids, {'value': target_model}])
                    # 2) Ensure the per-project <slug>_odoo_sh.claude_model exists
                    created = False
                    if project_key:
                        exists = proxy.execute_kw(db, uid, password, 'ir.config_parameter', 'search_count',
                                                  [[('key', '=', project_key)]])
                        if not exists:
                            proxy.execute_kw(db, uid, password, 'ir.config_parameter', 'set_param',
                                             [project_key, target_model])
                            created = True
                    project_summary[db] = f'updated {len(ids)} param(s)' + (f', created {project_key}' if created else '')
                except Exception as e:
                    project_summary[db] = f'error: {str(e)[:80]}'
            summary[project.name] = project_summary
        # Also update the local odooAL database params (this very DB)
        local = {}
        local_params = self.env['ir.config_parameter'].sudo().search(
            [('key', 'ilike', 'claude%model')]
        )
        for p in local_params:
            p.value = target_model
            local[p.key] = target_model
        summary['_local_odooal'] = local
        _logger.info("Claude model propagation summary: %s", summary)
        return summary

    def action_propagate_claude_model(self):
        """UI button: propagate claude_model to all instances."""
        target = self.env['ir.config_parameter'].sudo().get_param(
            'pmb_devops.claude_model', 'claude-opus-4-7',
        )
        result = self._propagate_claude_model_to_all(target)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Modelo Claude propagado',
                'message': f'Actualizado a {target}. Ver logs para detalles por instancia.',
                'type': 'success',
                'sticky': False,
            },
        }

    def _ensure_remote_project_id(self, conn):
        """Return a live remote project ID — creates on remote if missing, and
        clears stale mappings/pointers if the cached remote project was deleted."""
        self.ensure_one()
        uid, models_proxy, db, login, pw = conn
        remote_pid = self.production_project_id_remote
        if remote_pid:
            try:
                exists = models_proxy.execute_kw(
                    db, uid, pw, 'project.project', 'search_count',
                    [[('id', '=', remote_pid)]],
                )
                if not exists:
                    self.env['devops.stage.mapping'].sudo().search([
                        ('project_id', '=', self.id),
                    ]).unlink()
                    if self.odoo_project_id:
                        self.env['project.task'].sudo().search([
                            ('project_id', '=', self.odoo_project_id.id),
                            ('pmb_remote_task_id', '!=', 0),
                        ]).write({'pmb_remote_task_id': 0})
                    self.sudo().write({'production_project_id_remote': 0})
                    remote_pid = 0
            except Exception as e:
                _logger.warning('Project %s: remote project validation error: %s', self.name, e)
        if not remote_pid:
            remote_pid = models_proxy.execute_kw(
                db, uid, pw, 'project.project', 'create',
                [{'name': f'[DevOps] {self.name}'}],
            )
            self.sudo().write({'production_project_id_remote': remote_pid})
            _logger.info('Project %s: auto-created remote project id=%s', self.name, remote_pid)
        return remote_pid

    def action_resync_stages_to_production(self):
        """Push ALL local stages of this project to the remote and validate existing mappings.

        Per local stage:
          - Cached mapping + remote alive → repair name/sequence/fold drift.
          - Cached mapping + remote missing → drop stale mapping, re-resolve by
            name on remote, or create fresh.
          - No mapping → match-by-name on remote or create.

        Idempotent. Call at the start of any bulk task sync so create finds the
        right remote stage_id. Returns a summary dict."""
        self.ensure_one()
        if not self.sync_tasks_to_production:
            return {'status': 'disabled', 'errors': ['sync_tasks_to_production is disabled']}
        if not self.odoo_project_id:
            return {'status': 'no_project', 'errors': ['no odoo_project_id linked']}
        conn = self._get_production_xmlrpc()
        if not conn:
            return {'status': 'auth_failed', 'errors': ['could not connect/authenticate']}
        uid, models_proxy, db, login, pw = conn
        try:
            remote_pid = self._ensure_remote_project_id(conn)
        except Exception as e:
            return {'status': 'error', 'errors': [f'remote project: {e}']}

        local_stages = self.env['project.task.type'].sudo().search([
            ('project_ids', 'in', self.odoo_project_id.id),
        ])
        created, matched, repaired, errors = 0, 0, 0, []
        StageMap = self.env['devops.stage.mapping'].sudo()

        for stage in local_stages:
            try:
                mapping = StageMap.search([
                    ('project_id', '=', self.id),
                    ('local_stage_id', '=', stage.id),
                ], limit=1)
                if mapping:
                    alive = models_proxy.execute_kw(
                        db, uid, pw, 'project.task.type', 'search_count',
                        [[('id', '=', mapping.remote_stage_id)]],
                    )
                    if alive:
                        info = models_proxy.execute_kw(
                            db, uid, pw, 'project.task.type', 'read',
                            [[mapping.remote_stage_id], ['name', 'sequence', 'fold']],
                        )[0]
                        update_vals = {}
                        if info.get('name') != stage.name:
                            update_vals['name'] = stage.name
                        if (info.get('sequence') or 0) != (stage.sequence or 10):
                            update_vals['sequence'] = stage.sequence or 10
                        if bool(info.get('fold')) != bool(stage.fold):
                            update_vals['fold'] = bool(stage.fold)
                        if update_vals:
                            models_proxy.execute_kw(
                                db, uid, pw, 'project.task.type', 'write',
                                [[mapping.remote_stage_id], update_vals],
                            )
                            repaired += 1
                        mapping.write({'name_snapshot': stage.name})
                        continue
                    mapping.unlink()
                existing = models_proxy.execute_kw(
                    db, uid, pw, 'project.task.type', 'search',
                    [[('name', '=', stage.name), ('project_ids', 'in', remote_pid)]],
                    {'limit': 1},
                )
                if existing:
                    remote_stage_id = existing[0]
                    matched += 1
                else:
                    remote_stage_id = models_proxy.execute_kw(
                        db, uid, pw, 'project.task.type', 'create',
                        [{
                            'name': stage.name,
                            'sequence': stage.sequence or 10,
                            'project_ids': [(4, remote_pid)],
                            'fold': bool(stage.fold),
                        }],
                    )
                    created += 1
                StageMap.create({
                    'project_id': self.id,
                    'local_stage_id': stage.id,
                    'remote_stage_id': remote_stage_id,
                    'name_snapshot': stage.name,
                })
            except Exception as e:
                errors.append(f'{stage.name}: {e}')
                _logger.warning('Resync stages: %s failed: %s', stage.name, e)

        return {
            'status': 'ok',
            'stages_local': len(local_stages),
            'created': created, 'matched': matched, 'repaired': repaired,
            'mappings': StageMap.search_count([('project_id', '=', self.id)]),
            'errors': errors[:20],
        }

    def action_resync_unsynced_tasks(self):
        """Push every local task that lacks pmb_remote_task_id to the remote.

        Always syncs stages first (so task create finds the right remote stage_id).
        Used to recover from tasks that were created with `skip_task_sync` context
        (e.g. meeting analysis seeding) or before sync was enabled. Safe to run
        repeatedly — already-synced tasks are skipped. Returns a summary dict."""
        self.ensure_one()
        if not self.sync_tasks_to_production:
            return {'status': 'disabled', 'synced': 0, 'failed': 0, 'skipped': 0,
                    'errors': ['sync_tasks_to_production is disabled']}
        if not self.odoo_project_id:
            return {'status': 'no_project', 'synced': 0, 'failed': 0, 'skipped': 0,
                    'errors': ['no odoo_project_id linked']}
        stages_summary = self.action_resync_stages_to_production()
        tasks = self.env['project.task'].sudo().search([
            ('project_id', '=', self.odoo_project_id.id),
            ('pmb_remote_task_id', 'in', [0, False]),
        ])
        synced, failed, errors = 0, 0, []
        for t in tasks:
            try:
                remote_id = self._sync_task_create_to_production(t)
                if remote_id:
                    synced += 1
                else:
                    failed += 1
                    errors.append(f'{t.name}: sync returned no remote id')
            except Exception as e:
                failed += 1
                errors.append(f'{t.name}: {e}')
                _logger.warning('Resync: task %s failed: %s', t.id, e)
        return {'status': 'ok', 'synced': synced, 'failed': failed,
                'skipped': 0, 'total': len(tasks),
                'errors': errors[:20],
                'stages': stages_summary}

    def _sync_task_update_to_production(self, task, vals):
        """Update the matching task on the remote Odoo (looked up by stored remote ID).

        If the remote task no longer exists (deleted), clear the stale ID and re-create it
        via `_sync_task_create_to_production`.
        """
        self.ensure_one()
        conn = self._get_production_xmlrpc()
        if not conn:
            return
        remote_task_id = task.pmb_remote_task_id
        if not remote_task_id:
            return
        uid, models_proxy, db, login, password = conn
        # Verify remote task still exists; otherwise reset pointer and re-create
        try:
            still_exists = models_proxy.execute_kw(
                db, uid, password, 'project.task', 'search_count',
                [[('id', '=', remote_task_id)]],
            )
            if not still_exists:
                _logger.info("Project %s: remote task %s no longer exists, re-creating",
                             self.name, remote_task_id)
                task.sudo().write({'pmb_remote_task_id': 0})
                self._sync_task_create_to_production(task)
                return
        except Exception as e:
            _logger.warning("Project %s: error verifying remote task: %s", self.name, e)
        remote_vals = {}
        if 'name' in vals:
            remote_vals['name'] = vals['name']
        if 'description' in vals:
            remote_vals['description'] = str(vals['description']) if vals['description'] else False
        if 'date_deadline' in vals:
            remote_vals['date_deadline'] = str(vals['date_deadline']) if vals['date_deadline'] else False
        if 'priority' in vals:
            remote_vals['priority'] = vals['priority']
        if 'stage_id' in vals and vals['stage_id']:
            # Resolve the mapped remote stage (creates mapping on first encounter)
            local_stage = self.env['project.task.type'].sudo().browse(int(vals['stage_id']))
            if local_stage.exists():
                remote_stage_id = self._resolve_remote_stage_id(
                    conn, local_stage, self.production_project_id_remote,
                )
                if remote_stage_id:
                    remote_vals['stage_id'] = remote_stage_id
        if 'user_ids' in vals:
            # vals['user_ids'] here comes from a task.write() side effect; read the
            # resulting assignee set from the task itself and translate by email.
            remote_user_ids = self._translate_local_users_to_remote(conn, task.user_ids.ids)
            remote_vals['user_ids'] = [(6, 0, remote_user_ids)] if remote_user_ids else [(5,)]
        if not remote_vals:
            return
        try:
            models_proxy.execute_kw(
                db, uid, password, 'project.task', 'write', [[remote_task_id], remote_vals],
            )
            _logger.info("Project %s: updated remote task %s (vals=%s)",
                         self.name, remote_task_id, list(remote_vals.keys()))
        except Exception as e:
            _logger.warning("Project %s: failed to update remote task: %s", self.name, e)

    @staticmethod
    def _is_real_email_login(login):
        """True when login looks like a real email (has '@'). Excludes Odoo
        internal users like __system__, public, default, etc."""
        return bool(login) and '@' in login

    def _translate_local_users_to_remote(self, conn, local_user_ids):
        """Map local res.users ids → remote res.users ids by matching login (which
        in Odoo is typically the email). Only real-email logins are considered, so
        __system__ / public / default are never pushed to the remote. Unmatched
        logins are silently skipped — we do not create users on the remote."""
        self.ensure_one()
        if not local_user_ids:
            return []
        uid, models_proxy, db, login, password = conn
        local_logins = [
            u.login for u in self.env['res.users'].sudo().browse(local_user_ids)
            if self._is_real_email_login(u.login)
        ]
        if not local_logins:
            return []
        try:
            remote_users = models_proxy.execute_kw(
                db, uid, password, 'res.users', 'search_read',
                [[('login', 'in', local_logins)]], {'fields': ['id', 'login']},
            )
        except Exception as e:
            _logger.warning("Project %s: error translating local→remote users: %s",
                            self.name, e)
            return []
        return [u['id'] for u in remote_users if self._is_real_email_login(u.get('login'))]

    def _translate_remote_users_to_local(self, conn, remote_user_ids):
        """Map remote res.users ids → local res.users ids, filtered to pmb_devops
        dev/admin group members. Only real-email logins are considered so system
        users like __system__ are ignored."""
        self.ensure_one()
        if not remote_user_ids:
            return []
        uid, models_proxy, db, login, password = conn
        try:
            remote_users = models_proxy.execute_kw(
                db, uid, password, 'res.users', 'read',
                [list(remote_user_ids), ['login']],
            )
        except Exception as e:
            _logger.warning("Project %s: error reading remote users: %s", self.name, e)
            return []
        remote_logins = [
            u['login'] for u in remote_users
            if self._is_real_email_login(u.get('login'))
        ]
        if not remote_logins:
            return []
        group_ids = []
        for xmlid in ('pmb_devops.group_devops_developer',
                      'pmb_devops.group_devops_admin'):
            grp = self.env.ref(xmlid, raise_if_not_found=False)
            if grp:
                group_ids.append(grp.id)
        domain = [('login', 'in', remote_logins)]
        if group_ids:
            domain.append(('all_group_ids', 'in', group_ids))
        return self.env['res.users'].sudo().search(domain).ids

    def _apply_client_tag(self, task, client_name):
        """Set/replace the `Cliente: <name>` tag on a task (mirrors the logic in
        project_task_assign controller)."""
        self.ensure_one()
        stale = task.tag_ids.filtered(lambda t: t.name.startswith('Cliente:'))
        if stale:
            task.write({'tag_ids': [(3, t.id) for t in stale]})
        if not client_name:
            return
        Tag = self.env['project.tags'].sudo()
        tag = Tag.search([('name', '=', f'Cliente: {client_name}')], limit=1)
        if not tag:
            tag = Tag.create({'name': f'Cliente: {client_name}'})
        task.write({'tag_ids': [(4, tag.id)]})

    def _sync_task_delete_to_production(self, remote_task_id):
        """Delete a task on the remote Odoo (forward deletion)."""
        self.ensure_one()
        conn = self._get_production_xmlrpc()
        if not conn or not remote_task_id:
            return
        uid, models_proxy, db, login, password = conn
        try:
            models_proxy.execute_kw(
                db, uid, password, 'project.task', 'unlink', [[remote_task_id]],
            )
            _logger.info("Project %s: deleted remote task %s", self.name, remote_task_id)
        except Exception as e:
            _logger.warning("Project %s: failed to delete remote task %s: %s",
                            self.name, remote_task_id, e)

    def _resolve_local_stage_id(self, conn, remote_stage_id, remote_stage_name=''):
        """Reverse of _resolve_remote_stage_id: given a remote stage id, return the
        local project.task.type id. Uses devops.stage.mapping cache; falls back to
        name match within the local project; creates the stage locally on demand.
        """
        self.ensure_one()
        if not remote_stage_id or not self.odoo_project_id:
            return False
        StageMap = self.env['devops.stage.mapping'].sudo()
        mapping = StageMap.search([
            ('project_id', '=', self.id),
            ('remote_stage_id', '=', remote_stage_id),
        ], limit=1)
        if mapping and mapping.local_stage_id and mapping.local_stage_id.exists():
            return mapping.local_stage_id.id
        # No mapping (or stale) — fetch the remote name if not provided
        if not remote_stage_name:
            uid, models_proxy, db, login, password = conn
            try:
                recs = models_proxy.execute_kw(
                    db, uid, password, 'project.task.type', 'read',
                    [[remote_stage_id], ['name']],
                )
                remote_stage_name = recs[0]['name'] if recs else ''
            except Exception as e:
                _logger.warning("Project %s: error reading remote stage %s: %s",
                                self.name, remote_stage_id, e)
                return False
        if not remote_stage_name:
            return False
        TaskType = self.env['project.task.type'].sudo()
        local_stage = TaskType.search([
            ('name', '=', remote_stage_name),
            ('project_ids', 'in', self.odoo_project_id.id),
        ], limit=1)
        if not local_stage:
            local_stage = TaskType.create({
                'name': remote_stage_name,
                'project_ids': [(4, self.odoo_project_id.id)],
            })
        # Drop any stale mapping for this local stage before inserting (unique constraint
        # is on (project_id, local_stage_id))
        StageMap.search([
            ('project_id', '=', self.id),
            ('local_stage_id', '=', local_stage.id),
        ]).unlink()
        StageMap.create({
            'project_id': self.id,
            'local_stage_id': local_stage.id,
            'remote_stage_id': remote_stage_id,
            'name_snapshot': remote_stage_name,
        })
        _logger.info("Project %s: mapped remote stage '%s' remote=%s ↔ local=%s",
                     self.name, remote_stage_name, remote_stage_id, local_stage.id)
        return local_stage.id

    def _pull_remote_tasks(self):
        """Pull tasks from the remote Odoo into the local pmb_devops DB.

        Performs a 3-way reconcile per project:
          - Remote exists, local has matching pmb_remote_task_id → update local fields
          - Remote exists, no local match                          → create local mirror
          - Local has pmb_remote_task_id but remote is gone        → delete local mirror

        Writes are done with `skip_task_sync=True` in the context so the inherit
        overrides on project.task do not re-push the same changes back.
        """
        self.ensure_one()
        if not self.sync_tasks_to_production or not self.odoo_project_id:
            return
        conn = self._get_production_xmlrpc()
        if not conn:
            return
        remote_project_id = self.production_project_id_remote
        if not remote_project_id:
            return
        uid, models_proxy, db, login, password = conn
        # Validate remote project still exists
        try:
            exists = models_proxy.execute_kw(
                db, uid, password, 'project.project', 'search_count',
                [[('id', '=', remote_project_id)]],
            )
            if not exists:
                return
        except Exception as e:
            _logger.warning("Project %s: pull — error validating remote project: %s",
                            self.name, e)
            return
        # Fetch all remote tasks for this project
        try:
            remote_tasks = models_proxy.execute_kw(
                db, uid, password, 'project.task', 'search_read',
                [[('project_id', '=', remote_project_id)]],
                {'fields': ['id', 'name', 'description', 'date_deadline',
                            'priority', 'stage_id', 'user_ids', 'create_uid']},
            )
        except Exception as e:
            _logger.warning("Project %s: pull — error fetching remote tasks: %s",
                            self.name, e)
            return
        remote_ids = {rt['id'] for rt in remote_tasks}
        Task = self.env['project.task'].sudo().with_context(skip_task_sync=True)
        # Pre-resolve creator logins so we can filter out system users (__system__,
        # public, etc.) — clients must be real people with email-shaped logins.
        creator_ids = {rt['create_uid'][0] for rt in remote_tasks
                       if isinstance(rt.get('create_uid'), (list, tuple)) and rt['create_uid']}
        creator_login_by_id = {}
        if creator_ids:
            try:
                rows = models_proxy.execute_kw(
                    db, uid, password, 'res.users', 'read',
                    [list(creator_ids), ['login']],
                )
                creator_login_by_id = {r['id']: r.get('login') or '' for r in rows}
            except Exception as e:
                _logger.warning("Project %s: pull — error reading creator logins: %s",
                                self.name, e)
        # Upsert locals
        for rt in remote_tasks:
            local = Task.search([
                ('project_id', '=', self.odoo_project_id.id),
                ('pmb_remote_task_id', '=', rt['id']),
            ], limit=1)
            vals = {
                'name': rt.get('name') or '/',
                'description': rt.get('description') or False,
                'date_deadline': rt.get('date_deadline') or False,
                'priority': rt.get('priority') or '0',
            }
            remote_stage = rt.get('stage_id')
            if remote_stage:
                remote_stage_id = remote_stage[0] if isinstance(remote_stage, (list, tuple)) else remote_stage
                remote_stage_name = (remote_stage[1]
                                     if isinstance(remote_stage, (list, tuple)) and len(remote_stage) > 1
                                     else '')
                local_stage_id = self._resolve_local_stage_id(
                    conn, remote_stage_id, remote_stage_name,
                )
                if local_stage_id:
                    vals['stage_id'] = local_stage_id
            # Dev assignees: remote user_ids → local dev/admin users matched by login
            remote_user_ids = rt.get('user_ids') or []
            local_user_ids = self._translate_remote_users_to_local(conn, remote_user_ids)
            vals['user_ids'] = [(6, 0, local_user_ids)] if local_user_ids else [(5,)]
            if local:
                local.write(vals)
                task_rec = local
            else:
                vals['project_id'] = self.odoo_project_id.id
                vals['pmb_remote_task_id'] = rt['id']
                task_rec = Task.create(vals)
            # Client field: creator of the remote task. Only apply when the creator
            # has a real email-shaped login (skips __system__, public, etc.).
            create_uid = rt.get('create_uid')
            creator_name = ''
            if isinstance(create_uid, (list, tuple)) and create_uid:
                creator_id = create_uid[0]
                creator_login = creator_login_by_id.get(creator_id, '')
                if self._is_real_email_login(creator_login):
                    creator_name = create_uid[1] if len(create_uid) > 1 else ''
            self._apply_client_tag(task_rec, creator_name)
        # Reconcile deletions: local tasks with pmb_remote_task_id set but not in remote
        stale_domain = [
            ('project_id', '=', self.odoo_project_id.id),
            ('pmb_remote_task_id', '!=', 0),
        ]
        if remote_ids:
            stale_domain.append(('pmb_remote_task_id', 'not in', list(remote_ids)))
        stale = Task.search(stale_domain)
        if stale:
            _logger.info("Project %s: pull — deleting %d stale local tasks",
                         self.name, len(stale))
            stale.unlink()

    @api.model
    def _cron_pull_remote_tasks(self):
        """Cron entry point: pull remote tasks for every project with sync enabled."""
        projects = self.search([('sync_tasks_to_production', '=', True)])
        for project in projects:
            try:
                project._pull_remote_tasks()
            except Exception as e:
                _logger.warning("Project %s: pull cron failed: %s", project.name, e)
