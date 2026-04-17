import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)


class DevopsStageMapping(models.Model):
    """Mapping between local stage (project.task.type) and remote stage ID on production Odoo."""
    _name = 'devops.stage.mapping'
    _description = 'Mapeo de etapa local ↔ remota (sync producción)'
    _rec_name = 'local_stage_id'

    project_id = fields.Many2one(
        'devops.project', string='Proyecto DevOps',
        required=True, ondelete='cascade',
    )
    local_stage_id = fields.Many2one(
        'project.task.type', string='Etapa local',
        required=True, ondelete='cascade',
    )
    remote_stage_id = fields.Integer(
        string='ID etapa remota', required=True,
        help='ID de project.task.type en la BD de producción',
    )
    name_snapshot = fields.Char(
        string='Nombre al crearse',
        help='Nombre que tenía la etapa al crearse el mapeo (referencia/debug)',
    )

    _sql_constraints = [
        ('unique_project_local_stage',
         'UNIQUE(project_id, local_stage_id)',
         'Ya existe un mapeo para esta etapa en este proyecto.'),
    ]
