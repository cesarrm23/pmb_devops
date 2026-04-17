import json
import logging
import os
import subprocess
import time

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class DevopsAiAssistant(models.Model):
    _name = 'devops.ai.assistant'
    _description = 'Asistente IA DevOps'
    _order = 'create_date desc, id desc'

    project_id = fields.Many2one(
        'devops.project', string='Proyecto',
        ondelete='set null', index=True,
    )
    name = fields.Char(
        string='Nombre', compute='_compute_name', store=True,
    )
    prompt = fields.Text(string='Prompt', required=True)
    response = fields.Text(string='Respuesta')
    response_html = fields.Html(
        string='Respuesta HTML', compute='_compute_response_html',
    )
    context_type = fields.Selection([
        ('general', 'General'),
        ('log', 'Log'),
        ('build', 'Build'),
        ('branch', 'Branch'),
        ('deploy', 'Deploy'),
        ('error', 'Error'),
    ], string='Contexto', default='general')
    log_id = fields.Many2one('devops.log', string='Log Relacionado', ondelete='set null')
    build_id = fields.Many2one('devops.build', string='Build Relacionado', ondelete='set null')
    branch_id = fields.Many2one('devops.branch', string='Branch Relacionado', ondelete='set null')
    state = fields.Selection([
        ('draft', 'Borrador'),
        ('sending', 'Enviando'),
        ('done', 'Completado'),
        ('error', 'Error'),
    ], string='Estado', default='draft', required=True)
    error_message = fields.Text(string='Mensaje de Error')
    tokens_used = fields.Integer(string='Tokens Usados')
    model_used = fields.Char(string='Modelo Usado')
    duration = fields.Float(string='Duración (s)')
    user_id = fields.Many2one(
        'res.users', string='Usuario',
        default=lambda self: self.env.user,
        required=True, index=True,
    )

    @api.depends('prompt')
    def _compute_name(self):
        for rec in self:
            if rec.prompt:
                # First 80 chars of prompt as name
                clean = rec.prompt.strip().replace('\n', ' ')
                rec.name = clean[:80] + ('...' if len(clean) > 80 else '')
            else:
                rec.name = 'Sin prompt'

    @api.depends('response')
    def _compute_response_html(self):
        for rec in self:
            if rec.response:
                # Basic conversion: preserve line breaks and code blocks
                import markupsafe
                escaped = markupsafe.escape(rec.response)
                html = str(escaped).replace('\n', '<br/>')
                rec.response_html = f'<div class="o_devops_ai_response">{html}</div>'
            else:
                rec.response_html = ''

    # ------------------------------------------------------------------
    # Claude CLI / Auth
    # ------------------------------------------------------------------

    @api.model
    def get_claude_auth_status(self):
        """Check if Claude CLI is installed and authenticated.

        Runs LOCALLY on the asistentelisto server, NOT via SSH.
        """
        result = {'installed': False, 'authenticated': False, 'version': '', 'error': ''}
        try:
            # Check if claude CLI exists
            version_proc = subprocess.run(
                ['claude', '--version'],
                capture_output=True, text=True, timeout=10,
            )
            if version_proc.returncode == 0:
                result['installed'] = True
                result['version'] = version_proc.stdout.strip()

                # Check authentication by running a simple command
                auth_proc = subprocess.run(
                    ['claude', '-p', 'respond with OK', '--output-format', 'text'],
                    capture_output=True, text=True, timeout=30,
                    env=self._get_claude_env(),
                )
                if auth_proc.returncode == 0 and auth_proc.stdout.strip():
                    result['authenticated'] = True
            else:
                result['error'] = version_proc.stderr or 'claude CLI no encontrado'
        except FileNotFoundError:
            result['error'] = 'claude CLI no instalado en el sistema'
        except subprocess.TimeoutExpired:
            result['error'] = 'Timeout verificando Claude CLI'
        except Exception as e:
            result['error'] = str(e)

        return result

    def _get_claude_env(self):
        """Build environment dict with ANTHROPIC_API_KEY if configured."""
        env = os.environ.copy()
        api_key = self.env['ir.config_parameter'].sudo().get_param(
            'pmb_devops.claude_api_key', ''
        )
        if api_key:
            env['ANTHROPIC_API_KEY'] = api_key
        return env

    # ------------------------------------------------------------------
    # Send to Claude
    # ------------------------------------------------------------------

    def action_send_to_claude(self):
        """Send prompt to Claude CLI and store response.

        Executes `claude -p prompt --output-format text` via local subprocess.
        """
        self.ensure_one()
        self.write({'state': 'sending', 'error_message': False})

        # If there are pasted images, the CLI can't transmit them — force API path.
        if self._has_prompt_images():
            api_key = self.env['ir.config_parameter'].sudo().get_param(
                'pmb_devops.claude_api_key', ''
            )
            if api_key:
                self._send_via_api(api_key)
                return
            # No API key → downgrade: send text only via CLI, warn the user.
            _logger.warning(
                "Prompt contains images but no claude_api_key configured — "
                "images will be ignored and only text sent via CLI."
            )

        start_time = time.time()
        system_prompt = self._build_system_prompt()
        user_message = self._build_user_message()

        try:
            text_message = user_message if isinstance(user_message, str) else self.prompt or ''
            cmd = ['claude', '-p', text_message, '--output-format', 'text']
            if system_prompt:
                cmd.extend(['--system-prompt', system_prompt])

            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                env=self._get_claude_env(),
            )

            elapsed = time.time() - start_time

            if proc.returncode == 0 and proc.stdout.strip():
                self.write({
                    'state': 'done',
                    'response': proc.stdout.strip(),
                    'duration': round(elapsed, 2),
                    'model_used': 'claude-cli',
                })
            else:
                # Try API fallback
                api_key = self.env['ir.config_parameter'].sudo().get_param(
                    'pmb_devops.claude_api_key', ''
                )
                if api_key:
                    self._send_via_api(api_key)
                else:
                    error_msg = proc.stderr or 'Claude CLI no retornó respuesta'
                    self.write({
                        'state': 'error',
                        'error_message': error_msg,
                        'duration': round(elapsed, 2),
                    })
        except subprocess.TimeoutExpired:
            self.write({
                'state': 'error',
                'error_message': 'Timeout: Claude no respondió en 120 segundos',
                'duration': 120.0,
            })
        except FileNotFoundError:
            # Claude CLI not installed, try API
            api_key = self.env['ir.config_parameter'].sudo().get_param(
                'pmb_devops.claude_api_key', ''
            )
            if api_key:
                self._send_via_api(api_key)
            else:
                self.write({
                    'state': 'error',
                    'error_message': (
                        'Claude CLI no instalado y no hay API key configurada. '
                        'Configure la API key en Ajustes > DevOps.'
                    ),
                })
        except Exception as e:
            _logger.exception("Error sending to Claude")
            self.write({
                'state': 'error',
                'error_message': str(e),
                'duration': round(time.time() - start_time, 2),
            })

    def action_run_claude_code(self):
        """Run Claude Code with project repo as working directory.

        Similar to action_send_to_claude but sets cwd to the project repo.
        """
        self.ensure_one()
        if not self.project_id or not self.project_id.repo_path:
            raise UserError("El proyecto no tiene ruta de repositorio configurada.")

        self.write({'state': 'sending', 'error_message': False})

        start_time = time.time()
        system_prompt = self._build_system_prompt()
        user_message = self._build_user_message()

        try:
            cmd = ['claude', '-p', user_message, '--output-format', 'text']
            if system_prompt:
                cmd.extend(['--system-prompt', system_prompt])

            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=self.project_id.repo_path,
                env=self._get_claude_env(),
            )

            elapsed = time.time() - start_time

            if proc.returncode == 0 and proc.stdout.strip():
                self.write({
                    'state': 'done',
                    'response': proc.stdout.strip(),
                    'duration': round(elapsed, 2),
                    'model_used': 'claude-code',
                })
            else:
                self.write({
                    'state': 'error',
                    'error_message': proc.stderr or 'Claude Code no retornó respuesta',
                    'duration': round(elapsed, 2),
                })
        except subprocess.TimeoutExpired:
            self.write({
                'state': 'error',
                'error_message': 'Timeout: Claude Code no respondió en 120 segundos',
                'duration': 120.0,
            })
        except Exception as e:
            _logger.exception("Error running Claude Code")
            self.write({
                'state': 'error',
                'error_message': str(e),
                'duration': round(time.time() - start_time, 2),
            })

    # ------------------------------------------------------------------
    # Prompt building
    # ------------------------------------------------------------------

    def _build_system_prompt(self):
        """Build system prompt based on context."""
        parts = [
            "Eres un asistente DevOps experto en Odoo.",
            "Responde en español de forma clara y concisa.",
            "Si se te proporciona un log o error, analiza la causa raíz y sugiere soluciones.",
        ]
        if self.project_id:
            parts.append(
                f"Proyecto: {self.project_id.name}, "
                f"Servicio: {self.project_id.service_name or 'N/A'}, "
                f"BD: {self.project_id.database_name or 'N/A'}."
            )
        return ' '.join(parts)

    def _build_user_message(self):
        """Build user message with context from related records.

        Returns a plain string when there are no pasted images, or a
        list of Anthropic content blocks (image + text) when there are.
        """
        parts = []

        # Add context from related records
        if self.context_type == 'log' and self.log_id:
            parts.append(f"=== LOG ({self.log_id.level}) ===")
            parts.append(self.log_id.full_log or self.log_id.message or '')
            parts.append("=== FIN LOG ===\n")
        elif self.context_type == 'build' and self.build_id:
            parts.append(f"=== BUILD ({self.build_id.state}) ===")
            if hasattr(self.build_id, 'log'):
                parts.append(self.build_id.log or '')
            parts.append("=== FIN BUILD ===\n")
        elif self.context_type == 'branch' and self.branch_id:
            parts.append(f"=== BRANCH: {self.branch_id.name} ===\n")

        parts.append(self.prompt or '')
        text = '\n'.join(parts)

        images = self._get_prompt_image_blocks()
        if images:
            return images + [{'type': 'text', 'text': text or '(ver imágenes adjuntas)'}]
        return text

    def _has_prompt_images(self):
        self.ensure_one()
        return bool(self.env['ir.attachment'].sudo().search_count([
            ('res_model', '=', self._name),
            ('res_id', '=', self.id),
            ('mimetype', '=like', 'image/%'),
        ]))

    def _get_prompt_image_blocks(self):
        """Return attachments linked to this record as Anthropic image content blocks."""
        self.ensure_one()
        attachments = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', self._name),
            ('res_id', '=', self.id),
            ('mimetype', '=like', 'image/%'),
        ])
        blocks = []
        for att in attachments:
            if not att.datas:
                continue
            data = att.datas
            if isinstance(data, bytes):
                data = data.decode('ascii')
            blocks.append({
                'type': 'image',
                'source': {
                    'type': 'base64',
                    'media_type': att.mimetype or 'image/png',
                    'data': data,
                },
            })
        return blocks

    # ------------------------------------------------------------------
    # API fallback
    # ------------------------------------------------------------------

    def _send_via_api(self, api_key):
        """Fallback: send prompt via curl to Anthropic API."""
        self.ensure_one()
        start_time = time.time()

        system_prompt = self._build_system_prompt()
        user_message = self._build_user_message()

        try:
            response = self._call_claude_api(api_key, system_prompt, user_message)
            elapsed = time.time() - start_time

            if response.get('content'):
                text_parts = [
                    block.get('text', '')
                    for block in response['content']
                    if block.get('type') == 'text'
                ]
                self.write({
                    'state': 'done',
                    'response': '\n'.join(text_parts),
                    'duration': round(elapsed, 2),
                    'model_used': response.get('model', 'api'),
                    'tokens_used': (
                        response.get('usage', {}).get('input_tokens', 0) +
                        response.get('usage', {}).get('output_tokens', 0)
                    ),
                })
            else:
                error = response.get('error', {}).get('message', 'Respuesta vacía de la API')
                self.write({
                    'state': 'error',
                    'error_message': error,
                    'duration': round(elapsed, 2),
                })
        except Exception as e:
            self.write({
                'state': 'error',
                'error_message': f"Error en API: {e}",
                'duration': round(time.time() - start_time, 2),
            })

    def _call_claude_api(self, api_key, system_prompt, user_message):
        """Call Anthropic Messages API via curl."""
        model = self.env['ir.config_parameter'].sudo().get_param(
            'pmb_devops.claude_model', 'claude-opus-4-7',
        )
        # user_message can be a plain string or a list of Anthropic content
        # blocks (e.g. image + text) — the API accepts both shapes.
        content = user_message if isinstance(user_message, (list, tuple)) else user_message
        payload_data = {
            'model': model,
            'max_tokens': 16000,
            'system': system_prompt,
            'messages': [
                {'role': 'user', 'content': content},
            ],
        }
        # Adaptive thinking on Opus 4.7 / 4.6 / Sonnet 4.6 — Claude decides depth
        if model in ('claude-opus-4-7', 'claude-opus-4-6', 'claude-sonnet-4-6'):
            payload_data['thinking'] = {'type': 'adaptive'}
        payload = json.dumps(payload_data)

        proc = subprocess.run(
            [
                'curl', '-s',
                'https://api.anthropic.com/v1/messages',
                '-H', 'Content-Type: application/json',
                '-H', f'x-api-key: {api_key}',
                '-H', 'anthropic-version: 2023-06-01',
                '-d', payload,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

        if proc.returncode != 0:
            raise UserError(f"curl falló: {proc.stderr}")

        return json.loads(proc.stdout)
