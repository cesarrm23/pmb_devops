import logging

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

_logger = logging.getLogger(__name__)


class ProjectTaskInherit(models.Model):
    _inherit = 'project.task'

    pmb_remote_task_id = fields.Integer(
        string='Remote task ID (pmb_devops sync)',
        help='ID de la tarea en la BD de producción remota (XML-RPC sync)',
        copy=False,
    )

    @api.model_create_multi
    def create(self, vals_list):
        """Auto-route client tasks to review stage; sync to production if enabled."""
        tasks = super().create(vals_list)
        if self.env.context.get('skip_task_sync'):
            return tasks
        is_devops = self.env.user.has_group('pmb_devops.group_devops_admin') or \
                    self.env.user.has_group('pmb_devops.group_devops_developer')
        for task in tasks:
            devops_proj = self.env['devops.project'].sudo().search([
                ('odoo_project_id', '=', task.project_id.id),
            ], limit=1)
            if not devops_proj:
                continue
            # Auto-route client-created tasks to "Pendiente de revisión"
            if not is_devops:
                pending = self.env['project.task.type'].sudo().search([
                    ('name', '=', 'Pendiente de revisión'),
                    ('project_ids', 'in', task.project_id.id),
                ], limit=1)
                if pending:
                    task.sudo().write({'stage_id': pending.id})
            # Mirror to remote production Odoo if sync enabled
            if devops_proj.sync_tasks_to_production:
                try:
                    devops_proj._sync_task_create_to_production(task)
                except Exception as e:
                    _logger.warning("Failed to sync task to production: %s", e)
        return tasks

    def write(self, vals):
        """Sync field updates to remote production Odoo. Upsert: create on remote if no remote_id yet."""
        result = super().write(vals)
        if self.env.context.get('skip_task_sync'):
            return result
        # Skip if this write is only setting the remote_id (prevents recursion)
        if set(vals.keys()) == {'pmb_remote_task_id'}:
            return result
        synced_fields = {'name', 'date_deadline', 'priority', 'description', 'stage_id', 'user_ids'}
        if not any(k in vals for k in synced_fields):
            return result
        for task in self:
            devops_proj = self.env['devops.project'].sudo().search([
                ('odoo_project_id', '=', task.project_id.id),
                ('sync_tasks_to_production', '=', True),
            ], limit=1)
            if not devops_proj:
                continue
            try:
                if task.pmb_remote_task_id:
                    devops_proj._sync_task_update_to_production(task, vals)
                else:
                    # Upsert: create on remote if it doesn't exist yet
                    devops_proj._sync_task_create_to_production(task)
            except Exception as e:
                _logger.warning("Failed to sync task to production: %s", e)
        return result

    def unlink(self):
        """Propagate deletion to remote production Odoo (forward sync).

        Only DevOps admins may delete tasks that belong to a `devops.project`.
        Regular Odoo project tasks (no linked devops.project) are unaffected.
        The `skip_task_sync` context bypasses the admin check — it's only set by
        the pull cron reconciling a remote deletion back to local.
        """
        skip = self.env.context.get('skip_task_sync')
        if not skip:
            is_admin = self.env.user.has_group('pmb_devops.group_devops_admin')
            if not is_admin:
                devops_linked = self.env['devops.project'].sudo().search_count([
                    ('odoo_project_id', 'in', self.mapped('project_id').ids),
                ])
                if devops_linked:
                    raise AccessError(_(
                        "Solo administradores DevOps pueden eliminar tareas de pmb_devops."
                    ))
        if skip:
            return super().unlink()
        pending_deletes = []
        for task in self:
            if not task.pmb_remote_task_id:
                continue
            devops_proj = self.env['devops.project'].sudo().search([
                ('odoo_project_id', '=', task.project_id.id),
                ('sync_tasks_to_production', '=', True),
            ], limit=1)
            if devops_proj:
                pending_deletes.append((devops_proj, task.pmb_remote_task_id))
        result = super().unlink()
        for devops_proj, remote_id in pending_deletes:
            try:
                devops_proj._sync_task_delete_to_production(remote_id)
            except Exception as e:
                _logger.warning("Failed to delete remote task %s: %s", remote_id, e)
        return result
