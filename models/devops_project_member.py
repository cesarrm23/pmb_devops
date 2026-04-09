from odoo import api, fields, models


class DevopsProjectMember(models.Model):
    _name = 'devops.project.member'
    _description = 'Miembro del Proyecto DevOps'
    _rec_name = 'user_id'

    project_id = fields.Many2one(
        'devops.project', string='Proyecto',
        required=True, ondelete='cascade',
    )
    user_id = fields.Many2one(
        'res.users', string='Usuario',
        required=True, ondelete='cascade',
    )
    role = fields.Selection([
        ('admin', 'Admin'),
        ('developer', 'Developer'),
        ('viewer', 'Viewer'),
    ], string='Rol', required=True, default='developer')

    _sql_constraints = [
        ('unique_member', 'unique(project_id, user_id)',
         'El usuario ya es miembro de este proyecto.'),
    ]
