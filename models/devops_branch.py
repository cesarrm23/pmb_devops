import json
import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..utils import ssh_utils

_logger = logging.getLogger(__name__)


class DevopsBranch(models.Model):
    _name = 'devops.branch'
    _description = 'Git Branch'
    _order = 'branch_type, name'
    _rec_name = 'display_name'

    # ── Relationships ───────────────────────────────────────────────
    project_id = fields.Many2one(
        'devops.project', string='Proyecto',
        required=True, ondelete='cascade',
    )
    build_ids = fields.One2many(
        'devops.build', 'branch_id', string='Builds',
    )

    # ── Core fields ─────────────────────────────────────────────────
    name = fields.Char('Nombre', required=True)
    display_name = fields.Char(
        'Nombre Completo', compute='_compute_display_name',
        store=True,
    )
    branch_type = fields.Selection([
        ('production', 'Production'),
        ('staging', 'Staging'),
        ('development', 'Development'),
    ], string='Tipo', default='development', required=True)
    is_remote = fields.Boolean('Es Remota', default=False)
    is_current = fields.Boolean(
        'Es Actual', compute='_compute_is_current',
    )

    # ── Commit info ─────────────────────────────────────────────────
    last_commit_hash = fields.Char('Último Commit Hash')
    last_commit_message = fields.Text('Último Commit Mensaje')
    last_commit_date = fields.Datetime('Fecha Último Commit')
    last_commit_author = fields.Char('Autor Último Commit')
    commit_history = fields.Text('Historial de Commits (JSON)')

    # ── Build info ──────────────────────────────────────────────────
    last_build_id = fields.Many2one(
        'devops.build', string='Último Build',
        compute='_compute_last_build', store=True,
    )
    last_build_state = fields.Selection(
        related='last_build_id.state', string='Estado Último Build',
        store=True,
    )

    # ── Diff stats ──────────────────────────────────────────────────
    commits_ahead = fields.Integer(
        'Commits Adelante', compute='_compute_diff_stats',
    )
    commits_behind = fields.Integer(
        'Commits Atrás', compute='_compute_diff_stats',
    )

    _sql_constraints = [
        ('unique_branch', 'unique(project_id, name)',
         'Ya existe una rama con este nombre en el proyecto.'),
    ]

    # ── Computed fields ─────────────────────────────────────────────

    @api.depends('name', 'branch_type')
    def _compute_display_name(self):
        type_labels = dict(self._fields['branch_type'].selection)
        for rec in self:
            label = type_labels.get(rec.branch_type, '')
            rec.display_name = f"[{label}] {rec.name}" if label else rec.name

    @api.depends('name', 'project_id.repo_current_branch')
    def _compute_is_current(self):
        for rec in self:
            rec.is_current = (
                rec.name and rec.project_id.repo_current_branch
                and rec.name == rec.project_id.repo_current_branch
            )

    @api.depends('build_ids', 'build_ids.started_at')
    def _compute_last_build(self):
        for rec in self:
            builds = rec.build_ids.sorted('started_at', reverse=True)
            rec.last_build_id = builds[:1].id if builds else False

    def _compute_diff_stats(self):
        """Compute commits ahead/behind relative to production branch."""
        for rec in self:
            rec.commits_ahead = 0
            rec.commits_behind = 0
            if not rec.project_id or not rec.name:
                continue
            # Find the production branch name for this project
            prod_branch = rec.project_id.branch_ids.filtered(
                lambda b: b.branch_type == 'production'
            )
            if not prod_branch:
                continue
            prod_name = prod_branch[0].name
            if rec.name == prod_name:
                continue
            try:
                result = ssh_utils.execute_command(
                    rec.project_id,
                    ['git', 'rev-list', '--left-right', '--count',
                     f'{prod_name}...{rec.name}'],
                    cwd=rec.project_id.repo_path,
                )
                if result.returncode == 0 and result.stdout.strip():
                    parts = result.stdout.strip().split()
                    if len(parts) == 2:
                        rec.commits_behind = int(parts[0])
                        rec.commits_ahead = int(parts[1])
            except Exception:
                _logger.warning(
                    'Could not compute diff stats for branch %s', rec.name,
                    exc_info=True,
                )

    # ── Actions ─────────────────────────────────────────────────────

    def action_checkout(self):
        """Switch to this branch via git checkout."""
        self.ensure_one()
        try:
            result = ssh_utils.execute_command(
                self.project_id,
                ['git', 'checkout', self.name],
                cwd=self.project_id.repo_path,
            )
            if result.returncode != 0:
                raise UserError(
                    _('Error al cambiar de rama:\n%s') %
                    (result.stderr or result.stdout)
                )
            # Invalidate computed fields that depend on current branch
            self.project_id._compute_repo_current_branch()
            self.project_id.branch_ids._compute_is_current()
        except UserError:
            raise
        except Exception as e:
            raise UserError(_('Error al cambiar de rama: %s') % str(e))

    def action_pull(self):
        """Pull latest changes for this branch."""
        self.ensure_one()
        try:
            result = ssh_utils.execute_command(
                self.project_id,
                ['git', 'pull', 'origin', self.name],
                cwd=self.project_id.repo_path,
                timeout=60,
            )
            if result.returncode != 0:
                raise UserError(
                    _('Error al hacer pull:\n%s') %
                    (result.stderr or result.stdout)
                )
            self._refresh_commit_info()
        except UserError:
            raise
        except Exception as e:
            raise UserError(_('Error al hacer pull: %s') % str(e))

    def action_build(self):
        """Create and run a new build for this branch."""
        self.ensure_one()
        Build = self.env['devops.build']
        return Build.action_create_build(self)

    def action_deploy(self):
        """Open deploy wizard for this branch."""
        self.ensure_one()
        return {
            'name': _('Deploy'),
            'type': 'ir.actions.act_window',
            'res_model': 'devops.deploy.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_project_id': self.project_id.id,
                'default_branch_id': self.id,
            },
        }

    def action_view_commits(self):
        """Show commit history in a readable format."""
        self.ensure_one()
        self._refresh_commit_info()
        return {
            'name': _('Historial de Commits — %s') % self.name,
            'type': 'ir.actions.act_window',
            'res_model': 'devops.branch',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    # ── Helpers ─────────────────────────────────────────────────────

    def _refresh_commit_info(self):
        """Update last commit info and JSON commit history via git log."""
        for rec in self:
            if not rec.project_id or not rec.name:
                continue
            try:
                # Get last commit info
                fmt = '%H%n%s%n%ai%n%an'
                result = ssh_utils.execute_command(
                    rec.project_id,
                    ['git', 'log', '-1', f'--format={fmt}', rec.name],
                    cwd=rec.project_id.repo_path,
                )
                if result.returncode == 0 and result.stdout.strip():
                    lines = result.stdout.strip().split('\n')
                    if len(lines) >= 4:
                        rec.last_commit_hash = lines[0]
                        rec.last_commit_message = lines[1]
                        rec.last_commit_date = fields.Datetime.to_datetime(
                            lines[2][:19].replace('T', ' ')
                        ) if lines[2] else False
                        rec.last_commit_author = lines[3]

                # Get recent commit history (last 20 commits) as JSON
                fmt_json = '{"hash":"%H","short":"%h","message":"%s","date":"%ai","author":"%an"}'
                result = ssh_utils.execute_command(
                    rec.project_id,
                    ['git', 'log', '-20', f'--format={fmt_json}', rec.name],
                    cwd=rec.project_id.repo_path,
                )
                if result.returncode == 0 and result.stdout.strip():
                    commits = []
                    for line in result.stdout.strip().split('\n'):
                        if line.strip():
                            try:
                                commits.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass
                    rec.commit_history = json.dumps(commits)
            except Exception:
                _logger.warning(
                    'Could not refresh commit info for branch %s',
                    rec.name, exc_info=True,
                )
