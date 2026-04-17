from odoo import api, fields, models, _
from odoo.exceptions import UserError


class DevopsCommitLink(models.Model):
    """Many-to-many link between a project.task and a git commit.

    The commit itself lives in git (not in the DB); each row stores the
    triple (project_id, repo_path, commit_hash) plus a snapshot of
    message/author/date so the UI can render without re-running git.
    """
    _name = 'devops.commit.link'
    _description = 'Task ↔ Commit link'
    _order = 'commit_date desc, id desc'

    task_id = fields.Many2one(
        'project.task', string='Tarea',
        required=True, ondelete='cascade', index=True)
    project_id = fields.Many2one(
        'devops.project', string='Proyecto',
        required=True, ondelete='cascade', index=True)
    repo_path = fields.Char(string='Repo', required=True)
    commit_hash = fields.Char(string='Commit', required=True, index=True)
    short_hash = fields.Char(string='Short')
    commit_message = fields.Char(string='Mensaje')
    commit_author = fields.Char(string='Autor')
    commit_date = fields.Char(string='Fecha')

    _sql_constraints = [
        ('uniq_task_project_repo_commit',
         'unique(task_id, project_id, repo_path, commit_hash)',
         'Este commit ya está enlazado a esa tarea.'),
    ]

    @api.model
    def _link(self, task_id, project_id, repo_path, commit_hash,
              short_hash='', message='', author='', date=''):
        """Idempotent: return existing link if any, else create."""
        if not (task_id and project_id and repo_path and commit_hash):
            raise UserError(_("Datos insuficientes para enlazar commit."))
        existing = self.sudo().search([
            ('task_id', '=', int(task_id)),
            ('project_id', '=', int(project_id)),
            ('repo_path', '=', repo_path),
            ('commit_hash', '=', commit_hash),
        ], limit=1)
        if existing:
            return existing
        return self.sudo().create({
            'task_id': int(task_id),
            'project_id': int(project_id),
            'repo_path': repo_path,
            'commit_hash': commit_hash,
            'short_hash': short_hash or commit_hash[:7],
            'commit_message': (message or '')[:200],
            'commit_author': (author or '')[:100],
            'commit_date': date or '',
        })
