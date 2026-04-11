from odoo import fields, models


class ResUsers(models.Model):
    _inherit = 'res.users'

    devops_git_panel_width = fields.Integer(
        string='DevOps Git Panel Width',
        default=280,
    )
