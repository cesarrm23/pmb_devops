import logging
import time

from odoo import api, fields, models
from odoo.exceptions import UserError

from ..utils import ssh_utils

_logger = logging.getLogger(__name__)


class DevopsDeployAi(models.Model):
    _name = 'devops.deploy.ai'
    _description = 'Deploy Asistido por IA'
    _inherit = ['mail.thread']
    _order = 'create_date desc, id desc'

    name = fields.Char(
        string='Nombre', compute='_compute_name', store=True,
    )
    project_id = fields.Many2one(
        'devops.project', string='Proyecto',
        required=True, ondelete='cascade', index=True, tracking=True,
    )
    source_branch_id = fields.Many2one(
        'devops.branch', string='Branch Origen',
        ondelete='set null',
    )
    target_branch_id = fields.Many2one(
        'devops.branch', string='Branch Destino',
        ondelete='set null',
    )
    state = fields.Selection([
        ('draft', 'Borrador'),
        ('testing', 'Probando IA'),
        ('test_passed', 'Tests Pasados'),
        ('test_failed', 'Tests Fallidos'),
        ('deploying', 'Desplegando'),
        ('verifying', 'Verificando'),
        ('done', 'Completado'),
        ('failed', 'Fallido'),
        ('reverted', 'Revertido'),
    ], string='Estado', default='draft', required=True, tracking=True)
    deploy_type = fields.Selection([
        ('staging', 'Staging'),
        ('production', 'Producción'),
    ], string='Tipo de Deploy', required=True, default='staging', tracking=True)
    create_backup = fields.Boolean(
        string='Crear Backup', default=True,
    )
    run_ai_tests = fields.Boolean(
        string='Ejecutar Tests IA', default=True,
    )
    ai_test_prompt = fields.Text(
        string='Prompt de Tests IA',
        default=(
            "Revisa los cambios recientes en el repositorio. "
            "¿Es seguro hacer deploy? Responde SI o NO con explicación."
        ),
    )
    test_log = fields.Text(string='Log de Tests')
    deploy_log = fields.Text(string='Log de Deploy')
    ai_analysis = fields.Text(string='Análisis IA')
    error_message = fields.Text(string='Mensaje de Error')
    backup_id = fields.Many2one(
        'devops.backup', string='Backup Asociado', ondelete='set null',
    )
    started_at = fields.Datetime(string='Inicio')
    finished_at = fields.Datetime(string='Fin')
    duration = fields.Float(
        string='Duración (s)', compute='_compute_duration', store=True,
    )
    rollback_commit = fields.Char(string='Commit de Rollback')
    was_reverted = fields.Boolean(string='Fue Revertido', default=False)
    triggered_by = fields.Many2one(
        'res.users', string='Iniciado por',
        default=lambda self: self.env.user,
    )

    @api.depends('project_id', 'deploy_type', 'create_date')
    def _compute_name(self):
        for rec in self:
            project_name = rec.project_id.name or 'N/A'
            dtype = dict(rec._fields['deploy_type'].selection).get(
                rec.deploy_type, rec.deploy_type or ''
            )
            ts = fields.Datetime.to_string(rec.create_date) if rec.create_date else ''
            rec.name = f"Deploy {dtype} - {project_name} - {ts}"

    @api.depends('started_at', 'finished_at')
    def _compute_duration(self):
        for rec in self:
            if rec.started_at and rec.finished_at:
                delta = rec.finished_at - rec.started_at
                rec.duration = delta.total_seconds()
            else:
                rec.duration = 0.0

    # ------------------------------------------------------------------
    # Permission check
    # ------------------------------------------------------------------

    def _check_deploy_permission(self):
        """Check if the current user can perform this deploy."""
        self.ensure_one()
        if self.deploy_type == 'production':
            is_superadmin = self.env.user.has_group('pmb_devops.group_devops_admin')
            is_project_admin = self.env['devops.project.member'].search_count([
                ('project_id', '=', self.project_id.id),
                ('user_id', '=', self.env.user.id),
                ('role', '=', 'admin'),
            ]) > 0
            if not is_superadmin and not is_project_admin:
                raise UserError("Solo administradores pueden deployar a producción.")

    # ------------------------------------------------------------------
    # AI Tests
    # ------------------------------------------------------------------

    def action_run_ai_tests(self):
        """Run AI tests using Claude to evaluate deploy safety."""
        self.ensure_one()
        self.write({'state': 'testing', 'test_log': '', 'ai_analysis': ''})

        import subprocess as sp

        prompt = self.ai_test_prompt or (
            "¿Es seguro hacer deploy de los cambios recientes? Responde SI o NO."
        )

        # Add project context to prompt
        full_prompt = (
            f"Proyecto: {self.project_id.name}\n"
            f"Tipo de deploy: {self.deploy_type}\n"
            f"Branch origen: {self.source_branch_id.name if self.source_branch_id else 'N/A'}\n"
            f"Branch destino: {self.target_branch_id.name if self.target_branch_id else 'N/A'}\n\n"
            f"{prompt}"
        )

        try:
            env = self.env['devops.ai.assistant']._get_claude_env()
            cmd = ['claude', '-p', full_prompt, '--output-format', 'text']

            cwd = self.project_id.repo_path if self.project_id.repo_path else None
            proc = sp.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
                cwd=cwd,
            )

            response = proc.stdout.strip() if proc.returncode == 0 else ''
            error = proc.stderr.strip() if proc.returncode != 0 else ''

            self.write({
                'test_log': response or error,
                'ai_analysis': response,
            })

            # Check if AI approved - look for SI/SÍ in response
            response_upper = response.upper()
            if 'SI' in response_upper or 'SÍ' in response_upper:
                self.write({'state': 'test_passed'})
                self.message_post(body="Tests IA: APROBADO")
            else:
                self.write({'state': 'test_failed'})
                self.message_post(body=f"Tests IA: RECHAZADO\n{response[:500]}")

        except sp.TimeoutExpired:
            self.write({
                'state': 'test_failed',
                'test_log': 'Timeout: Claude no respondió en 120 segundos',
            })
        except FileNotFoundError:
            self.write({
                'state': 'test_failed',
                'test_log': 'Claude CLI no instalado. Instale claude o configure API key.',
            })
        except Exception as e:
            _logger.exception("Error running AI tests")
            self.write({
                'state': 'test_failed',
                'test_log': str(e),
            })

    # ------------------------------------------------------------------
    # Deploy
    # ------------------------------------------------------------------

    def action_deploy(self):
        """Execute the full deploy pipeline:

        1. Permission check (production needs admin)
        2. Create backup (if enabled)
        3. Git pull via ssh_utils
        4. Restart service via ssh_utils (NEVER stop)
        5. Verify service is active
        6. Verify HTTP response
        7. Auto-rollback on failure
        """
        self.ensure_one()
        self._check_deploy_permission()

        self.write({
            'state': 'deploying',
            'started_at': fields.Datetime.now(),
            'deploy_log': '',
            'error_message': False,
        })
        log_parts = []

        project = self.project_id
        service = project.service_name or 'odoo'

        try:
            # 1. Save current commit for rollback
            commit_result = ssh_utils.execute_command(
                project, ['git', 'rev-parse', 'HEAD'],
                cwd=project.repo_path, timeout=10,
            )
            if commit_result.returncode == 0:
                self.rollback_commit = commit_result.stdout.strip()
                log_parts.append(f"Commit actual: {self.rollback_commit[:12]}")

            # 2. Create backup
            if self.create_backup:
                log_parts.append("Creando backup...")
                try:
                    backup = self.env['devops.backup'].action_create_backup(
                        project=project, trigger='pre_deploy',
                    )
                    self.backup_id = backup.id
                    if backup.state == 'done':
                        log_parts.append(f"Backup completado: {backup.name}")
                    else:
                        log_parts.append(f"Backup falló: {backup.error_message}")
                        # Don't abort for backup failure on staging
                        if self.deploy_type == 'production':
                            raise UserError(
                                f"Backup falló, deploy a producción abortado: "
                                f"{backup.error_message}"
                            )
                except UserError:
                    raise
                except Exception as e:
                    log_parts.append(f"Error en backup: {e}")
                    if self.deploy_type == 'production':
                        raise

            # 3. Git pull
            log_parts.append("Ejecutando git pull...")
            branch = self.target_branch_id.name if self.target_branch_id else None
            pull_cmd = ['git', 'pull', 'origin']
            if branch:
                pull_cmd.append(branch)

            pull_result = ssh_utils.execute_command(
                project, pull_cmd, cwd=project.repo_path, timeout=60,
            )
            if pull_result.returncode != 0:
                error = pull_result.stderr or 'git pull falló'
                log_parts.append(f"Git pull falló: {error}")
                raise UserError(f"Git pull falló:\n{error}")
            log_parts.append(f"Git pull OK: {pull_result.stdout.strip()[:200]}")

            # 4. Restart service (NEVER stop, always restart)
            log_parts.append(f"Reiniciando {service}.service...")
            restart_result = ssh_utils.execute_command(
                project,
                ['sudo', 'systemctl', 'restart', f'{service}.service'],
                timeout=30,
            )
            if restart_result.returncode != 0:
                log_parts.append(f"Restart falló: {restart_result.stderr}")
                raise UserError(
                    f"Restart del servicio falló:\n{restart_result.stderr}"
                )
            log_parts.append("Servicio reiniciado OK")

            # 5. Verify service is active
            self.write({'state': 'verifying'})
            log_parts.append("Verificando servicio...")
            time.sleep(5)  # Wait for service to start

            status_result = ssh_utils.execute_command(
                project,
                ['systemctl', 'is-active', f'{service}.service'],
                timeout=10,
            )
            is_active = (
                status_result.returncode == 0 and
                'active' in (status_result.stdout or '').strip()
            )

            if not is_active:
                log_parts.append(
                    f"Servicio NO activo: {status_result.stdout} {status_result.stderr}"
                )
                raise UserError("El servicio no está activo después del restart.")
            log_parts.append("Servicio activo: OK")

            # 6. Verify HTTP
            if project.odoo_url:
                log_parts.append(f"Verificando HTTP: {project.odoo_url}...")
                http_result = ssh_utils.execute_command_shell(
                    project,
                    f"curl -s -o /dev/null -w '%{{http_code}}' "
                    f"--max-time 10 {project.odoo_url}/web/login",
                    timeout=15,
                )
                http_code = http_result.stdout.strip() if http_result.returncode == 0 else ''
                if http_code and http_code.startswith(('2', '3')):
                    log_parts.append(f"HTTP OK: {http_code}")
                else:
                    log_parts.append(f"HTTP falló: código={http_code}")
                    raise UserError(
                        f"Verificación HTTP falló (código: {http_code}). "
                        f"Ejecutando rollback..."
                    )

            # Success
            self.write({
                'state': 'done',
                'finished_at': fields.Datetime.now(),
                'deploy_log': '\n'.join(log_parts),
            })
            self.message_post(
                body=f"Deploy completado exitosamente.\n{''.join(log_parts[-3:])}"
            )

        except (UserError, Exception) as e:
            log_parts.append(f"\nERROR: {e}")
            self.write({
                'state': 'failed',
                'finished_at': fields.Datetime.now(),
                'deploy_log': '\n'.join(log_parts),
                'error_message': str(e),
            })
            self.message_post(body=f"Deploy FALLIDO: {e}")

            # Auto-rollback
            if self.rollback_commit:
                try:
                    self.action_rollback()
                except Exception as rb_err:
                    _logger.exception("Rollback also failed")
                    self.write({
                        'error_message': f"{e}\n\nRollback también falló: {rb_err}",
                    })

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    def action_rollback(self):
        """Rollback to previous commit and restart service."""
        self.ensure_one()
        if not self.rollback_commit:
            raise UserError("No hay commit de rollback registrado.")

        project = self.project_id
        service = project.service_name or 'odoo'
        log_parts = [f"Iniciando rollback a {self.rollback_commit[:12]}..."]

        try:
            # git reset --hard to rollback commit
            reset_result = ssh_utils.execute_command(
                project,
                ['git', 'reset', '--hard', self.rollback_commit],
                cwd=project.repo_path,
                timeout=30,
            )
            if reset_result.returncode != 0:
                raise UserError(f"Git reset falló: {reset_result.stderr}")
            log_parts.append("Git reset OK")

            # Restart service (NEVER stop)
            restart_result = ssh_utils.execute_command(
                project,
                ['sudo', 'systemctl', 'restart', f'{service}.service'],
                timeout=30,
            )
            if restart_result.returncode != 0:
                raise UserError(f"Restart falló: {restart_result.stderr}")
            log_parts.append("Servicio reiniciado OK")

            self.write({
                'state': 'reverted',
                'was_reverted': True,
                'deploy_log': (self.deploy_log or '') + '\n\n' + '\n'.join(log_parts),
            })
            self.message_post(body=f"Rollback ejecutado a commit {self.rollback_commit[:12]}")

        except Exception as e:
            log_parts.append(f"Error en rollback: {e}")
            self.write({
                'deploy_log': (self.deploy_log or '') + '\n\n' + '\n'.join(log_parts),
            })
            raise
