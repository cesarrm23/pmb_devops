from odoo import fields, models


class ResUsers(models.Model):
    _inherit = 'res.users'

    devops_git_panel_width = fields.Integer(string='DevOps Git Panel Width', default=280)
    devops_sidebar_minimized = fields.Boolean(string='Sidebar Minimized', default=False)
    devops_git_collapsed = fields.Boolean(string='Git Panel Collapsed', default=False)
