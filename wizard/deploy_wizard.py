import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..utils import ssh_utils

_logger = logging.getLogger(__name__)


class DevopsDeployWizard(models.TransientModel):
    _name = 'devops.deploy.wizard'
    _description = 'Wizard de Deploy'

    # ── Relationships ───────────────────────────────────────────────
    project_id = fields.Many2one(
        'devops.project', string='Proyecto', required=True,
    )
    branch_id = fields.Many2one(
        'devops.branch', string='Rama a Desplegar', required=True,
        domain="[('project_id', '=', project_id)]",
    )

    # ── Computed / config ───────────────────────────────────────────
    target_branch = fields.Char(
        string='Rama Destino',
        compute='_compute_target_branch',
    )
    create_backup = fields.Boolean(
        string='Crear Backup Antes', default=True,
    )
    restart_service = fields.Boolean(
        string='Reiniciar Servicio', default=True,
    )

    # ── State machine ───────────────────────────────────────────────
    state = fields.Selection([
        ('confirm', 'Confirmar'),
        ('deploying', 'Desplegando'),
        ('done', 'Completado'),
        ('failed', 'Fallido'),
    ], string='Estado', default='confirm', required=True)

    # ── Output ──────────────────────────────────────────────────────
    deploy_log = fields.Text(string='Log de Deploy')
    error_message = fields.Text(string='Mensaje de Error')
    commits_to_deploy = fields.Text(
        string='Commits a Desplegar',
        compute='_compute_commits_to_deploy',
    )

    # ── Computed methods ────────────────────────────────────────────

    @api.depends('project_id')
    def _compute_target_branch(self):
        for rec in self:
            if rec.project_id and rec.project_id.production_branch:
                rec.target_branch = rec.project_id.production_branch
            else:
                rec.target_branch = 'main'

    @api.depends('branch_id', 'project_id')
    def _compute_commits_to_deploy(self):
        for rec in self:
            rec.commits_to_deploy = ''
            if not rec.branch_id or not rec.project_id:
                continue
            prod = rec.target_branch or rec.project_id.production_branch or 'main'
            try:
                result = ssh_utils.execute_command(
                    rec.project_id,
                    ['git', 'log', '--oneline', f'{prod}..{rec.branch_id.name}'],
                    cwd=rec.project_id.repo_path,
                )
                if result.returncode == 0:
                    rec.commits_to_deploy = result.stdout.strip() or 'Sin commits nuevos'
                else:
                    rec.commits_to_deploy = (
                        f'Error obteniendo commits: {result.stderr or ""}'
                    )
            except Exception as e:
                rec.commits_to_deploy = f'Error: {e}'

    # ── Actions ─────────────────────────────────────────────────────

    def action_deploy(self):
        """Execute the deployment: backup + checkout prod + merge + restart."""
        self.ensure_one()
        self.write({
            'state': 'deploying',
            'deploy_log': '',
            'error_message': '',
        })

        project = self.project_id
        branch = self.branch_id
        prod = self.target_branch or project.production_branch or 'main'
        log_lines = []

        try:
            # Step 1: Create backup if requested
            if self.create_backup:
                log_lines.append('=== Paso 1: Creando backup ===')
                try:
                    backup = self.env['devops.backup'].action_create_backup(
                        project=project, trigger='pre_deploy',
                    )
                    if backup.state == 'done':
                        log_lines.append(
                            f'Backup creado: {backup.name} ({backup.file_size} MB)'
                        )
                    else:
                        log_lines.append(
                            f'Advertencia: Backup en estado {backup.state}'
                        )
                except Exception as e:
                    log_lines.append(f'Advertencia: Error creando backup: {e}')
            else:
                log_lines.append('=== Paso 1: Backup omitido ===')

            # Step 2: Checkout production branch
            log_lines.append(f'\n=== Paso 2: Checkout {prod} ===')
            result = ssh_utils.execute_command(
                project,
                ['git', 'checkout', prod],
                cwd=project.repo_path,
                timeout=30,
            )
            log_lines.append(result.stdout or '')
            if result.returncode != 0:
                raise UserError(
                    f"Error en checkout {prod}:\n{result.stderr}"
                )

            # Step 3: Pull latest on production
            log_lines.append(f'\n=== Paso 3: Pull {prod} ===')
            result = ssh_utils.execute_command(
                project,
                ['git', 'pull', 'origin', prod],
                cwd=project.repo_path,
                timeout=60,
            )
            log_lines.append(result.stdout or '')
            if result.returncode != 0:
                raise UserError(
                    f"Error en pull {prod}:\n{result.stderr}"
                )

            # Step 4: Merge branch into production
            log_lines.append(
                f'\n=== Paso 4: Merge {branch.name} -> {prod} ==='
            )
            result = ssh_utils.execute_command(
                project,
                ['git', 'merge', branch.name, '--no-edit'],
                cwd=project.repo_path,
                timeout=60,
            )
            log_lines.append(result.stdout or '')
            if result.returncode != 0:
                # Abort the merge to leave clean state
                ssh_utils.execute_command(
                    project,
                    ['git', 'merge', '--abort'],
                    cwd=project.repo_path,
                )
                raise UserError(
                    f"Error en merge:\n{result.stderr}\n\n"
                    "El merge fue abortado automáticamente."
                )

            # Step 5: Restart service if requested
            if self.restart_service:
                service_name = project.odoo_service_name
                if service_name:
                    log_lines.append(
                        f'\n=== Paso 5: Reiniciando {service_name} ==='
                    )
                    result = ssh_utils.execute_command(
                        project,
                        ['sudo', 'systemctl', 'restart', service_name],
                        timeout=60,
                    )
                    if result.returncode != 0:
                        log_lines.append(
                            f'Advertencia: Error reiniciando servicio: '
                            f'{result.stderr}'
                        )
                    else:
                        log_lines.append(
                            f'Servicio {service_name} reiniciado correctamente.'
                        )
                else:
                    log_lines.append(
                        '\n=== Paso 5: Sin servicio configurado, omitido ==='
                    )
            else:
                log_lines.append('\n=== Paso 5: Reinicio de servicio omitido ===')

            # Step 6: Create build record for traceability
            log_lines.append('\n=== Deploy completado exitosamente ===')
            build = self.env['devops.build'].create({
                'project_id': project.id,
                'branch_id': branch.id,
                'build_type': 'deploy',
                'state': 'success',
                'triggered_by': self.env.user.id,
                'started_at': fields.Datetime.now(),
                'finished_at': fields.Datetime.now(),
                'build_log': '\n'.join(log_lines),
            })
            # Refresh commit info on the build
            try:
                fmt = '%H%n%s%n%an'
                commit_result = ssh_utils.execute_command(
                    project,
                    ['git', 'log', '-1', f'--format={fmt}'],
                    cwd=project.repo_path,
                )
                if commit_result.returncode == 0 and commit_result.stdout.strip():
                    lines = commit_result.stdout.strip().split('\n')
                    if len(lines) >= 3:
                        build.write({
                            'commit_hash': lines[0],
                            'commit_message': lines[1],
                            'commit_author': lines[2],
                        })
            except Exception:
                pass

            self.write({
                'state': 'done',
                'deploy_log': '\n'.join(log_lines),
            })

        except UserError as e:
            log_lines.append(f'\n=== ERROR ===\n{e.args[0]}')
            self.write({
                'state': 'failed',
                'deploy_log': '\n'.join(log_lines),
                'error_message': e.args[0],
            })
        except Exception as e:
            _logger.exception("Deploy failed")
            log_lines.append(f'\n=== ERROR ===\n{e}')
            self.write({
                'state': 'failed',
                'deploy_log': '\n'.join(log_lines),
                'error_message': str(e),
            })

        return self._reopen()

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
