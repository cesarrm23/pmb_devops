import logging

from odoo import api, fields, models

from ..utils import ssh_utils

_logger = logging.getLogger(__name__)


class DevopsLog(models.Model):
    _name = 'devops.log'
    _description = 'Log de Servidor DevOps'
    _order = 'timestamp desc, id desc'

    project_id = fields.Many2one(
        'devops.project', string='Proyecto',
        required=True, ondelete='cascade', index=True,
    )
    name = fields.Char(
        string='Nombre', compute='_compute_name', store=True,
    )
    timestamp = fields.Datetime(
        string='Fecha/Hora', default=fields.Datetime.now, required=True,
    )
    level = fields.Selection([
        ('debug', 'Debug'),
        ('info', 'Info'),
        ('warning', 'Warning'),
        ('error', 'Error'),
        ('critical', 'Critical'),
    ], string='Nivel', default='info', required=True)
    source = fields.Selection([
        ('odoo', 'Odoo'),
        ('systemd', 'Systemd'),
        ('git', 'Git'),
        ('build', 'Build'),
        ('backup', 'Backup'),
        ('ai', 'AI'),
        ('cron', 'Cron'),
    ], string='Fuente', default='odoo', required=True)
    message = fields.Text(string='Mensaje')
    full_log = fields.Text(string='Log Completo')
    ai_analysis = fields.Text(string='Análisis IA')
    ai_suggestion = fields.Text(string='Sugerencia IA')

    @api.depends('level', 'source', 'timestamp')
    def _compute_name(self):
        for rec in self:
            ts = fields.Datetime.to_string(rec.timestamp) if rec.timestamp else ''
            rec.name = f"[{(rec.level or '').upper()}] [{rec.source or ''}] {ts}"

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_fetch_logs(self, project=None, lines=100):
        """Fetch recent journalctl logs from the project's service.

        Can be called as:
        - self.action_fetch_logs()  (uses self.project_id)
        - DevopsLog.action_fetch_logs(project=project_record, lines=50)
        """
        if project is None:
            project = self.project_id
        if not project:
            return False

        service = project.service_name or 'odoo'
        try:
            result = ssh_utils.execute_command(project, [
                'journalctl', '-u', f'{service}.service',
                '-n', str(lines),
                '--no-pager',
                '--output=short-iso',
            ], timeout=30)

            output = result.stdout or ''
            stderr = result.stderr or ''
            # Determine level based on output content
            level = 'info'
            if result.returncode != 0:
                level = 'error'
                output = f"STDERR:\n{stderr}\n\nSTDOUT:\n{output}"
            elif 'error' in output.lower():
                level = 'warning'

            # Extract first meaningful line as message
            lines_list = [ln for ln in output.strip().split('\n') if ln.strip()]
            message = lines_list[-1] if lines_list else 'Sin logs'

            log = self.env['devops.log'].create({
                'project_id': project.id,
                'timestamp': fields.Datetime.now(),
                'level': level,
                'source': 'systemd',
                'message': message[:500] if message else '',
                'full_log': output,
            })
            return log
        except Exception as e:
            _logger.exception("Error fetching logs for project %s", project.name)
            return self.env['devops.log'].create({
                'project_id': project.id,
                'timestamp': fields.Datetime.now(),
                'level': 'error',
                'source': 'systemd',
                'message': f"Error obteniendo logs: {e}",
            })

    def action_analyze_with_ai(self):
        """Open AI assistant wizard pre-filled with log context."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'devops.ai.assistant.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_project_id': self.project_id.id,
                'default_context_type': 'log',
                'default_log_id': self.id,
                'default_prompt': (
                    f"Analiza el siguiente log del servicio y sugiere soluciones:\n\n"
                    f"{self.full_log or self.message or ''}"
                ),
            },
        }
