from odoo import api, models


class ProjectTaskInherit(models.Model):
    _inherit = 'project.task'

    @api.model_create_multi
    def create(self, vals_list):
        """Auto-route client-created tasks to 'Pendiente de revisión'."""
        tasks = super().create(vals_list)
        # Check if creator is a DevOps admin/developer
        is_devops = self.env.user.has_group('pmb_devops.group_devops_admin') or \
                    self.env.user.has_group('pmb_devops.group_devops_developer')
        if not is_devops:
            for task in tasks:
                # Only for DevOps-linked projects
                devops_proj = self.env['devops.project'].sudo().search([
                    ('odoo_project_id', '=', task.project_id.id),
                ], limit=1)
                if devops_proj:
                    pending = self.env['project.task.type'].sudo().search([
                        ('name', '=', 'Pendiente de revisión'),
                        ('project_ids', 'in', task.project_id.id),
                    ], limit=1)
                    if pending:
                        task.sudo().write({'stage_id': pending.id})
        return tasks
