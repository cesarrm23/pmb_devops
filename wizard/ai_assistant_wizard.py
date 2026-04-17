import base64
import logging
import re
import time
from html import unescape

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
    prompt = fields.Html(
        string='Prompt',
        sanitize=False,
        sanitize_attributes=False,
        sanitize_style=False,
        strip_classes=False,
    )
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

        # Extract images + plain text from the Html prompt (paste-image support)
        plain_prompt, image_attachments = self._extract_prompt_images(self.prompt)
        if not plain_prompt and not image_attachments:
            raise UserError("El prompt está vacío.")

        # Build extra context to append to the plain-text prompt
        extra = self._build_extra_context()
        full_prompt = plain_prompt
        if extra:
            full_prompt = f"{plain_prompt}\n\n{extra}"

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

        # Re-link pasted images to the ai_record so _call_claude_api can find them
        if image_attachments:
            image_attachments.write({
                'res_model': 'devops.ai.assistant',
                'res_id': ai_record.id,
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

    # ── Paste-image support ─────────────────────────────────────────

    _IMG_TAG_RE = re.compile(r'<img\b[^>]*>', re.IGNORECASE)
    _DATA_URI_RE = re.compile(
        r'data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)', re.IGNORECASE,
    )
    _WEB_IMAGE_RE = re.compile(r'/web/image/(\d+)', re.IGNORECASE)
    _SRC_RE = re.compile(r'\bsrc\s*=\s*"([^"]+)"', re.IGNORECASE)
    _TAG_RE = re.compile(r'<[^>]+>')

    def _extract_prompt_images(self, html):
        """Pull <img> tags out of `html`, store them as ir.attachment records,
        and return (plain_text, attachment_recordset).

        Supports two image sources the html_editor produces:
        * data:image/...;base64,... (image pasted, not yet uploaded)
        * /web/image/<attachment_id> (image uploaded via the editor's uploader)
        """
        Attachment = self.env['ir.attachment']
        if not html:
            return '', Attachment.browse()

        collected_ids = []

        def _handle_img(match):
            tag = match.group(0)
            src_match = self._SRC_RE.search(tag)
            if not src_match:
                return ''
            src = src_match.group(1)
            data_uri = self._DATA_URI_RE.match(src)
            if data_uri:
                mime = data_uri.group(1).lower()
                b64_data = re.sub(r'\s+', '', data_uri.group(2))
                try:
                    base64.b64decode(b64_data, validate=True)
                except Exception:
                    _logger.warning("Invalid base64 in pasted image, skipping")
                    return ''
                ext = mime.split('/', 1)[1].split('+', 1)[0] or 'png'
                att = Attachment.create({
                    'name': f'ai_prompt_image.{ext}',
                    'datas': b64_data,
                    'mimetype': mime,
                    'type': 'binary',
                })
                collected_ids.append(att.id)
                return f'[imagen adjunta #{att.id}]'
            web_img = self._WEB_IMAGE_RE.search(src)
            if web_img:
                att_id = int(web_img.group(1))
                att = Attachment.sudo().browse(att_id).exists()
                if att and att.datas:
                    collected_ids.append(att.id)
                    return f'[imagen adjunta #{att.id}]'
            return ''

        text_with_markers = self._IMG_TAG_RE.sub(_handle_img, str(html))
        text_with_markers = re.sub(r'<br\s*/?>', '\n', text_with_markers, flags=re.IGNORECASE)
        text_with_markers = re.sub(r'</(p|div|li|h[1-6])>', '\n', text_with_markers, flags=re.IGNORECASE)
        plain = self._TAG_RE.sub('', text_with_markers)
        plain = unescape(plain)
        plain = re.sub(r'\n{3,}', '\n\n', plain).strip()
        return plain, Attachment.browse(collected_ids)
