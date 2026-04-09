import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class DevopsController(http.Controller):

    @http.route('/devops/project/status', type='json', auth='user')
    def project_status(self, project_id):
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}
        return {
            'name': project.name,
            'state': project.state,
            'branch': project.repo_current_branch,
            'environment': project.environment,
        }
