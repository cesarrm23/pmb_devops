import logging
import time

import markupsafe

from odoo import api, fields, models
from odoo.exceptions import UserError

from ..utils import git_utils

_logger = logging.getLogger(__name__)


class DevopsAiAssistantWizard(models.TransientModel):
    _name = 'devops.ai.assistant.wizard'
    _description = 'Asistente IA — Wizard'

    # ── Relationships & core input ──────────────────────────────────
    project_id = fields.Many2one(
        'devops.project', string='Proyecto', required=True,
    )
    prompt = fields.Text(string='Prompt')
    context_type = fields.Selection([
        ('general', 'General'),
        ('log', 'Análisis de Log'),
        ('build', 'Análisis de Build'),
        ('branch', 'Análisis de Branch'),
        ('deploy', 'Consulta de Deploy'),
        ('error', 'Diagnóstico de Error'),
        ('performance', 'Rendimiento'),
        ('security', 'Seguridad'),
    ], string='Tipo de Contexto', default='general', required=True)
    use_claude_code_cli = fields.Boolean(
        string='Usar Claude Code CLI',
        help='Ejecuta con cwd en el repositorio del proyecto.',
    )

    # ── Extra-context toggles ───────────────────────────────────────
    include_recent_logs = fields.Boolean(string='Incluir logs recientes')
    include_git_status = fields.Boolean(string='Incluir git status')
    include_last_build = fields.Boolean(string='Incluir último build')

    # ── State machine ───────────────────────────────────────────────
    state = fields.Selection([
        ('input', 'Entrada'),
        ('processing', 'Procesando'),
        ('result', 'Resultado'),
    ], string='Estado', default='input', required=True)

    # ── Output ──────────────────────────────────────────────────────
    response = fields.Text(string='Respuesta')
    response_html = fields.Html(
        string='Respuesta HTML',
        compute='_compute_response_html',
        sanitize=False,
    )
    error_message = fields.Text(string='Mensaje de Error')
    tokens_used = fields.Integer(string='Tokens Usados')
    duration = fields.Float(string='Duración (s)')

    # ── History (computed) ──────────────────────────────────────────
    history_ids = fields.Many2many(
        'devops.ai.assistant',
        string='Historial Reciente',
        compute='_compute_history',
    )

    # ── Computed methods ────────────────────────────────────────────

    @api.depends('response')
    def _compute_response_html(self):
        for rec in self:
            if rec.response:
                escaped = markupsafe.escape(rec.response)
                html = str(escaped).replace('\n', '<br/>')
                rec.response_html = (
                    f'<div class="o_devops_ai_response">{html}</div>'
                )
            else:
                rec.response_html = ''

    @api.depends('project_id')
    def _compute_history(self):
        AiAssistant = self.env['devops.ai.assistant']
        for rec in self:
            if rec.project_id:
                rec.history_ids = AiAssistant.search([
                    ('project_id', '=', rec.project_id.id),
                ], limit=10, order='create_date desc')
            else:
                rec.history_ids = AiAssistant.browse()

    # ── Actions ─────────────────────────────────────────────────────

    def action_send(self):
        """Send the prompt to Claude and display the result."""
        self.ensure_one()
        if not self.prompt:
            raise UserError("Ingrese un prompt antes de enviar.")

        self.write({'state': 'processing', 'error_message': False})

        # Build extra context to append to the prompt
        extra = self._build_extra_context()
        full_prompt = self.prompt
        if extra:
            full_prompt = f"{self.prompt}\n\n{extra}"

        # Create the AI assistant record
        ai_record = self.env['devops.ai.assistant'].create({
            'project_id': self.project_id.id,
            'prompt': full_prompt,
            'context_type': self.context_type if self.context_type in (
                'general', 'log', 'build', 'branch', 'deploy', 'error',
            ) else 'general',
            'state': 'draft',
            'user_id': self.env.user.id,
        })

        # Dispatch to the appropriate method
        try:
            if self.use_claude_code_cli:
                ai_record.action_run_claude_code()
            else:
                ai_record.action_send_to_claude()
        except Exception as e:
            _logger.exception("Error in AI assistant wizard")
            self.write({
                'state': 'result',
                'error_message': str(e),
            })
            return self._reopen()

        # Read results back from the AI record
        self.write({
            'state': 'result',
            'response': ai_record.response or '',
            'error_message': ai_record.error_message or '',
            'tokens_used': ai_record.tokens_used or 0,
            'duration': ai_record.duration or 0.0,
        })
        return self._reopen()

    def action_new_question(self):
        """Reset the wizard to input state for a new question."""
        self.ensure_one()
        self.write({
            'state': 'input',
            'prompt': '',
            'response': '',
            'response_html': '',
            'error_message': '',
            'tokens_used': 0,
            'duration': 0.0,
        })
        return self._reopen()

    # ── Helpers ─────────────────────────────────────────────────────

    def _build_extra_context(self):
        """Build extra context string based on the selected checkboxes."""
        self.ensure_one()
        parts = []
        project = self.project_id

        if self.include_git_status and project:
            try:
                status = git_utils.git_status(project)
                if status:
                    parts.append(f"=== GIT STATUS ===\n{status}\n=== FIN GIT STATUS ===")
            except Exception as e:
                _logger.warning("Error obteniendo git status: %s", e)

        if self.include_recent_logs and project:
            try:
                recent_logs = self.env['devops.log'].search([
                    ('project_id', '=', project.id),
                ], limit=5, order='timestamp desc')
                if recent_logs:
                    log_text = '\n---\n'.join(
                        f"[{log.level}] {log.timestamp}: {log.message or ''}"
                        for log in recent_logs
                    )
                    parts.append(
                        f"=== LOGS RECIENTES ===\n{log_text}\n=== FIN LOGS ==="
                    )
            except Exception as e:
                _logger.warning("Error obteniendo logs recientes: %s", e)

        if self.include_last_build and project:
            try:
                last_build = self.env['devops.build'].search([
                    ('project_id', '=', project.id),
                ], limit=1, order='started_at desc')
                if last_build:
                    build_info = (
                        f"Estado: {last_build.state}\n"
                        f"Commit: {last_build.commit_hash or 'N/A'}\n"
                        f"Mensaje: {last_build.commit_message or 'N/A'}\n"
                        f"Log:\n{last_build.build_log or 'Sin log'}"
                    )
                    parts.append(
                        f"=== ÚLTIMO BUILD ===\n{build_info}\n=== FIN BUILD ==="
                    )
            except Exception as e:
                _logger.warning("Error obteniendo último build: %s", e)

        return '\n\n'.join(parts)

    def _reopen(self):
        """Return an action to reopen this wizard."""
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }
