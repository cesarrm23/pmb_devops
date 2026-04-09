import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..utils import ssh_utils

_logger = logging.getLogger(__name__)


class DevopsBuild(models.Model):
    _name = 'devops.build'
    _description = 'Build / Deployment'
    _order = 'started_at desc'
    _rec_name = 'name'

    # ── Relationships ───────────────────────────────────────────────
    project_id = fields.Many2one(
        'devops.project', string='Proyecto',
        required=True, ondelete='cascade',
    )
    branch_id = fields.Many2one(
        'devops.branch', string='Rama',
        required=True, ondelete='cascade',
    )
    triggered_by = fields.Many2one(
        'res.users', string='Ejecutado por',
        default=lambda self: self.env.user,
    )

    # ── Core fields ─────────────────────────────────────────────────
    name = fields.Char(
        'Nombre', compute='_compute_name', store=True,
    )
    state = fields.Selection([
        ('pending', 'Pendiente'),
        ('building', 'Construyendo'),
        ('testing', 'Probando'),
        ('success', 'Exitoso'),
        ('warning', 'Con Advertencias'),
        ('failed', 'Fallido'),
    ], string='Estado', default='pending', required=True)
    build_type = fields.Selection([
        ('push', 'Push'),
        ('manual', 'Manual'),
        ('cron', 'Cron'),
        ('deploy', 'Deploy'),
    ], string='Tipo', default='manual')

    # ── Commit info ─────────────────────────────────────────────────
    commit_hash = fields.Char('Commit Hash')
    commit_message = fields.Char('Commit Mensaje')
    commit_author = fields.Char('Commit Autor')

    # ── Timing ──────────────────────────────────────────────────────
    started_at = fields.Datetime('Inicio')
    finished_at = fields.Datetime('Fin')
    duration = fields.Float(
        'Duración (min)', compute='_compute_duration', store=True,
    )

    # ── Logs ────────────────────────────────────────────────────────
    build_log = fields.Text('Log de Build')
    error_log = fields.Text('Log de Errores')
    modules_updated = fields.Text('Módulos Actualizados')
    modules_installed = fields.Text('Módulos Instalados')

    # ── Computed fields ─────────────────────────────────────────────

    @api.depends('branch_id.name', 'started_at')
    def _compute_name(self):
        for rec in self:
            branch_name = rec.branch_id.name or 'unknown'
            date_str = (
                fields.Datetime.context_timestamp(rec, rec.started_at).strftime('%Y%m%d-%H%M')
                if rec.started_at else 'pending'
            )
            rec.name = f"BUILD-{branch_name}-{date_str}"

    @api.depends('started_at', 'finished_at')
    def _compute_duration(self):
        for rec in self:
            if rec.started_at and rec.finished_at:
                delta = rec.finished_at - rec.started_at
                rec.duration = round(delta.total_seconds() / 60.0, 2)
            else:
                rec.duration = 0.0

    # ── Class-level action ──────────────────────────────────────────

    @api.model
    def action_create_build(self, branch):
        """Create a new build for the given branch and run it.

        Args:
            branch: devops.branch recordset (single record)
        Returns:
            Action to open the new build form
        """
        build = self.create({
            'project_id': branch.project_id.id,
            'branch_id': branch.id,
            'build_type': 'manual',
            'triggered_by': self.env.user.id,
        })
        build.action_run_build()
        return {
            'name': _('Build'),
            'type': 'ir.actions.act_window',
            'res_model': 'devops.build',
            'res_id': build.id,
            'view_mode': 'form',
            'target': 'current',
        }

    # ── Build execution ─────────────────────────────────────────────

    def action_run_build(self):
        """Execute the build: git pull + update commit info + restart service.

        Steps:
            1. git pull on the branch
            2. Update commit info from the branch
            3. Restart the Odoo service
            4. Update build state based on results
        """
        self.ensure_one()
        self.write({
            'state': 'building',
            'started_at': fields.Datetime.now(),
            'build_log': '',
            'error_log': '',
        })
        log_lines = []
        error_lines = []
        project = self.project_id
        branch = self.branch_id

        try:
            # Step 1: git pull
            log_lines.append('=== Step 1: git pull ===')
            result = ssh_utils.execute_command(
                project,
                ['git', 'pull', 'origin', branch.name],
                cwd=project.repo_path,
                timeout=120,
            )
            log_lines.append(result.stdout or '')
            if result.returncode != 0:
                error_lines.append(f'git pull failed:\n{result.stderr}')
                self.write({
                    'state': 'failed',
                    'finished_at': fields.Datetime.now(),
                    'build_log': '\n'.join(log_lines),
                    'error_log': '\n'.join(error_lines),
                })
                return

            # Step 2: Update commit info
            log_lines.append('\n=== Step 2: Update commit info ===')
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
                        self.write({
                            'commit_hash': lines[0],
                            'commit_message': lines[1],
                            'commit_author': lines[2],
                        })
                        log_lines.append(
                            f'Commit: {lines[0][:8]} — {lines[1]}'
                        )
                # Also refresh the branch commit info
                branch._refresh_commit_info()
            except Exception as e:
                error_lines.append(f'Warning: could not update commit info: {e}')

            # Step 3: Restart service
            log_lines.append('\n=== Step 3: Restart service ===')
            service_name = project.service_name
            if not service_name:
                error_lines.append('No service_name configured on project.')
                self.write({
                    'state': 'warning',
                    'finished_at': fields.Datetime.now(),
                    'build_log': '\n'.join(log_lines),
                    'error_log': '\n'.join(error_lines),
                })
                return

            restart_result = ssh_utils.execute_command(
                project,
                ['sudo', 'systemctl', 'restart', service_name],
                timeout=60,
            )
            log_lines.append(restart_result.stdout or '')
            if restart_result.returncode != 0:
                error_lines.append(
                    f'Service restart failed:\n{restart_result.stderr}'
                )
                self.write({
                    'state': 'failed',
                    'finished_at': fields.Datetime.now(),
                    'build_log': '\n'.join(log_lines),
                    'error_log': '\n'.join(error_lines),
                })
                return

            log_lines.append(f'Service {service_name} restarted successfully.')

            # Step 4: Success
            self.write({
                'state': 'success',
                'finished_at': fields.Datetime.now(),
                'build_log': '\n'.join(log_lines),
                'error_log': '\n'.join(error_lines) if error_lines else False,
            })

        except Exception as e:
            _logger.exception('Build %s failed with exception', self.name)
            error_lines.append(f'Exception: {e}')
            self.write({
                'state': 'failed',
                'finished_at': fields.Datetime.now(),
                'build_log': '\n'.join(log_lines),
                'error_log': '\n'.join(error_lines),
            })

    def action_rebuild(self):
        """Create a fresh build for the same branch and run it."""
        self.ensure_one()
        return self.action_create_build(self.branch_id)
