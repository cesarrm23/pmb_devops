"""AI Agent models for automated DevOps tasks."""
import json
import logging
import subprocess
import time

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class DevopsAiAgent(models.Model):
    _name = 'devops.ai.agent'
    _description = 'Agente IA automatizado'
    _order = 'sequence, id'

    name = fields.Char(required=True)
    description = fields.Text()
    agent_type = fields.Selection([
        ('git_docs', 'Documentación de cambios Git'),
    ], required=True, default='git_docs')
    provider = fields.Selection([
        ('claude', 'Claude (Anthropic)'),
        ('copilot', 'GitHub Copilot'),
    ], default='copilot', required=True)
    copilot_model = fields.Selection([
        ('gpt-4o', 'GPT-4o'),
        ('gpt-4.1', 'GPT-4.1'),
        ('gpt-5-mini', 'GPT-5 mini'),
        ('claude-haiku-4.5', 'Claude Haiku 4.5'),
        ('claude-sonnet-4.6', 'Claude Sonnet 4.6 (plan superior)'),
        ('claude-opus-4.7', 'Claude Opus 4.7 (plan superior)'),
        ('gpt-5.4', 'GPT-5.4 (plan superior)'),
        ('gemini-3.1-pro-preview', 'Gemini 3.1 Pro (plan superior)'),
    ], default='gpt-4o')
    project_id = fields.Many2one('devops.project', required=True, ondelete='cascade')
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)

    # Schedule
    interval_number = fields.Integer(default=1, string='Cada')
    interval_type = fields.Selection([
        ('hours', 'Horas'),
        ('days', 'Días'),
    ], default='days')

    # State
    last_run = fields.Datetime(readonly=True)
    last_commit_hash = fields.Char(readonly=True, help='Último commit procesado')
    run_count = fields.Integer(compute='_compute_run_count')
    run_ids = fields.One2many('devops.ai.agent.run', 'agent_id')

    # Config
    max_commits = fields.Integer(default=20, help='Máximo de commits a procesar por ejecución')
    branch = fields.Char(default='HEAD', help='Rama a monitorear')
    custom_system_prompt = fields.Text(
        string='Prompt del sistema',
        help='Instrucciones de sistema para el LLM. El user message con la lista de commits, stats y diff se genera automaticamente.',
        default=lambda self: self._default_system_prompt(),
    )

    @api.model
    def _default_system_prompt(self):
        return (
            "Eres un documentador técnico de software. "
            "Genera documentación clara y concisa en español sobre los cambios "
            "realizados en un repositorio Git. "
            "Organiza por categorías: nuevas funcionalidades, correcciones, "
            "mejoras, refactorizaciones. "
            "Incluye el hash del commit como referencia. "
            "Formato: Markdown."
        )

    @api.depends('run_ids')
    def _compute_run_count(self):
        for rec in self:
            rec.run_count = len(rec.run_ids)

    def _find_git_repos(self):
        """Discover actual git repositories under project repo_path."""
        from ..utils import ssh_utils
        project = self.project_id
        repo_path = project.repo_path or ''
        if not repo_path:
            return []

        # Find .git dirs up to 2 levels deep
        cmd_str = f'find {repo_path} -maxdepth 2 -name .git -type d 2>/dev/null'
        try:
            result = ssh_utils.execute_command_shell(project, cmd_str, timeout=10)
            if result.returncode == 0:
                repos = []
                for line in result.stdout.strip().split('\n'):
                    line = line.strip()
                    if line.endswith('/.git'):
                        repos.append(line[:-5])  # Remove /.git
                # Also check if repo_path itself is a git repo
                if not repos:
                    check = ssh_utils.execute_command(
                        project, ['git', 'rev-parse', '--git-dir'],
                        cwd=repo_path, timeout=5,
                    )
                    if check.returncode == 0:
                        repos = [repo_path]
                return repos
        except Exception as e:
            _logger.warning("Agent %s: error finding repos: %s", self.name, e)
        return [repo_path]  # Fallback

    def _get_new_commits(self):
        """Detect commits newer than last_commit_hash across all repos."""
        self.ensure_one()
        project = self.project_id
        if not project.repo_path:
            return []

        from ..utils import git_utils, ssh_utils

        # Fetch latest
        git_utils.git_fetch(project)

        repos = self._find_git_repos()
        all_commits = []
        branch = self.branch or 'HEAD'

        for repo in repos:
            if self.last_commit_hash:
                cmd = [
                    'git', 'log', f'-{self.max_commits}',
                    '--format=%H|||%h|||%s|||%ai|||%an|||%ae',
                    f'{self.last_commit_hash}..{branch}',
                ]
            else:
                cmd = [
                    'git', 'log', f'-{self.max_commits}',
                    '--format=%H|||%h|||%s|||%ai|||%an|||%ae',
                    branch,
                ]
            try:
                result = ssh_utils.execute_command(project, cmd, cwd=repo)
                if result.returncode == 0:
                    commits = git_utils._parse_log_output(result.stdout)
                    for c in commits:
                        c['repo'] = repo.split('/')[-1]  # Short name
                        c['repo_path'] = repo  # Full path for diffs
                    all_commits.extend(commits)
            except Exception as e:
                _logger.warning("Agent %s: error in repo %s: %s", self.name, repo, e)

        # Sort by date desc, limit total
        all_commits.sort(key=lambda c: c.get('date', ''), reverse=True)
        return all_commits[:self.max_commits]

    def _get_commit_diff(self, commit_hash, repo_path=None):
        """Get the diff for a specific commit."""
        from ..utils import ssh_utils
        cwd = repo_path or self.project_id.repo_path
        try:
            result = ssh_utils.execute_command(
                self.project_id,
                ['git', 'diff', f'{commit_hash}~1..{commit_hash}', '--stat'],
                cwd=cwd, timeout=15,
            )
            stat = result.stdout.strip() if result.returncode == 0 else ''

            result2 = ssh_utils.execute_command(
                self.project_id,
                ['git', 'diff', f'{commit_hash}~1..{commit_hash}', '--no-color'],
                cwd=cwd, timeout=30,
            )
            diff = result2.stdout[:8000] if result2.returncode == 0 else ''  # Limit size

            return stat, diff
        except Exception as e:
            _logger.warning("Agent %s: error getting diff for %s: %s", self.name, commit_hash, e)
            return '', ''

    def _call_llm(self, system_prompt, user_message):
        """Call LLM API (Claude or Copilot) to generate documentation."""
        if self.provider == 'copilot':
            return self._call_copilot(system_prompt, user_message)
        return self._call_claude(system_prompt, user_message)

    def _call_claude(self, system_prompt, user_message):
        """Call Claude API (Anthropic) directly."""
        ICP = self.env['ir.config_parameter'].sudo()
        api_key = ICP.get_param('pmb_devops.claude_api_key', '')
        if not api_key:
            raise Exception('Claude API key no configurada')

        model = ICP.get_param('pmb_devops.claude_model', 'claude-opus-4-7')
        payload_data = {
            'model': model,
            'max_tokens': 16000,
            'system': system_prompt,
            'messages': [{'role': 'user', 'content': user_message}],
        }
        # Adaptive thinking on Opus 4.7 / 4.6 / Sonnet 4.6 — Claude self-tunes
        # depth for documentation generation (genuinely complex task).
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
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            raise Exception(f'curl error: {proc.stderr}')

        resp = json.loads(proc.stdout)
        if 'error' in resp:
            raise Exception(f"API error: {resp['error'].get('message', resp['error'])}")

        content = resp.get('content', [])
        return content[0].get('text', '') if content else ''

    def _get_copilot_token(self):
        """Exchange GitHub OAuth token for a Copilot API token."""
        ICP = self.env['ir.config_parameter'].sudo()
        github_token = ICP.get_param('pmb_devops.github_token', '')
        if not github_token:
            raise Exception(
                'GitHub token no configurado. '
                'Agrégalo en Ajustes > GitHub Token para usar Copilot.'
            )

        # Check cached copilot token
        cached = ICP.get_param('pmb_devops.copilot_token', '')
        expires = ICP.get_param('pmb_devops.copilot_token_expires', '')
        if cached and expires:
            from datetime import datetime
            try:
                exp_dt = datetime.fromisoformat(expires)
                if datetime.utcnow() < exp_dt:
                    return cached
            except Exception:
                pass

        # Exchange GitHub token for Copilot token
        proc = subprocess.run(
            [
                'curl', '-s',
                'https://api.github.com/copilot_internal/v2/token',
                '-H', f'Authorization: token {github_token}',
                '-H', 'Accept: application/json',
                '-H', 'Editor-Version: vscode/1.96.0',
                '-H', 'Editor-Plugin-Version: copilot-chat/0.24.2',
            ],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            raise Exception(f'Error obteniendo token Copilot: {proc.stderr}')

        resp = json.loads(proc.stdout)
        token = resp.get('token', '')
        if not token:
            raise Exception(
                f"No se pudo obtener token Copilot. "
                f"Verifica que tu cuenta GitHub tenga Copilot activo. "
                f"Respuesta: {proc.stdout[:200]}"
            )

        # Cache token
        expires_at = resp.get('expires_at', 0)
        if expires_at:
            from datetime import datetime
            exp_dt = datetime.utcfromtimestamp(expires_at)
            ICP.set_param('pmb_devops.copilot_token_expires', exp_dt.isoformat())
        ICP.set_param('pmb_devops.copilot_token', token)

        return token

    def _call_copilot(self, system_prompt, user_message):
        """Call GitHub Copilot Chat API (OpenAI-compatible)."""
        token = self._get_copilot_token()

        model = self.copilot_model or 'gpt-4o'
        payload = json.dumps({
            'model': model,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_message},
            ],
            'temperature': 0.2,
            'stream': False,
        })

        proc = subprocess.run(
            [
                'curl', '-s',
                'https://api.githubcopilot.com/chat/completions',
                '-H', 'Content-Type: application/json',
                '-H', f'Authorization: Bearer {token}',
                '-H', 'Editor-Version: vscode/1.96.0',
                '-H', 'Editor-Plugin-Version: copilot-chat/0.24.2',
                '-H', 'Copilot-Integration-Id: vscode-chat',
                '-H', 'Openai-Intent: conversation-panel',
                '-d', payload,
            ],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            raise Exception(f'curl error: {proc.stderr}')

        resp = json.loads(proc.stdout)
        if 'error' in resp:
            raise Exception(f"Copilot API error: {resp['error'].get('message', resp['error'])}")

        choices = resp.get('choices', [])
        if choices:
            return choices[0].get('message', {}).get('content', '')
        return ''

    def action_run(self):
        """Manual trigger — run the agent now."""
        self.ensure_one()
        self._execute()

    def _execute(self):
        """Execute the agent: detect changes, generate docs."""
        self.ensure_one()
        Run = self.env['devops.ai.agent.run']
        run = Run.create({
            'agent_id': self.id,
            'status': 'running',
            'start_time': fields.Datetime.now(),
        })
        self.env.cr.commit()  # Commit so UI can see it immediately

        try:
            commits = self._get_new_commits()
            if not commits:
                run.write({
                    'status': 'done',
                    'end_time': fields.Datetime.now(),
                    'summary': 'Sin commits nuevos.',
                    'commits_processed': 0,
                })
                self.write({'last_run': fields.Datetime.now()})
                return run

            # Build context for Claude
            commit_details = []
            for c in commits:
                stat, diff = self._get_commit_diff(c['full_hash'], c.get('repo_path'))
                commit_details.append({
                    'hash': c['short_hash'],
                    'repo': c.get('repo', ''),
                    'message': c['message'],
                    'author': c['author'],
                    'date': c['date'],
                    'stat': stat,
                    'diff': diff[:3000],  # Limit per commit
                })

            system_prompt = (self.custom_system_prompt or '').strip() or self._default_system_prompt()

            user_msg = (
                f"Proyecto: {self.project_id.name}\n"
                f"Rama: {self.branch or 'main'}\n"
                f"Commits nuevos ({len(commit_details)}):\n\n"
            )
            for cd in commit_details:
                repo_label = f" ({cd['repo']})" if cd.get('repo') else ''
                user_msg += f"### {cd['hash']}{repo_label} — {cd['message']}\n"
                user_msg += f"Autor: {cd['author']} | Fecha: {cd['date']}\n"
                if cd['stat']:
                    user_msg += f"```\n{cd['stat']}\n```\n"
                if cd['diff']:
                    user_msg += f"Diff (parcial):\n```diff\n{cd['diff'][:2000]}\n```\n\n"

            documentation = self._call_llm(system_prompt, user_msg)

            run.write({
                'status': 'done',
                'end_time': fields.Datetime.now(),
                'commits_processed': len(commits),
                'summary': documentation,
                'commits_json': json.dumps([{
                    'hash': c['short_hash'],
                    'message': c['message'],
                    'author': c['author'],
                } for c in commits]),
            })
            self.write({
                'last_run': fields.Datetime.now(),
                'last_commit_hash': commits[0]['full_hash'],
            })
            _logger.info("Agent %s: documented %d commits", self.name, len(commits))

        except Exception as e:
            _logger.exception("Agent %s execution error", self.name)
            run.write({
                'status': 'error',
                'end_time': fields.Datetime.now(),
                'error_message': str(e),
            })

        return run

    @api.model
    def _cron_run_agents(self):
        """Cron job: run all due agents."""
        agents = self.search([('active', '=', True)])
        now = fields.Datetime.now()
        for agent in agents:
            try:
                if not agent.last_run:
                    agent._execute()
                    self.env.cr.commit()
                    continue

                from datetime import timedelta
                delta = timedelta(
                    hours=agent.interval_number if agent.interval_type == 'hours' else 0,
                    days=agent.interval_number if agent.interval_type == 'days' else 0,
                )
                if now >= agent.last_run + delta:
                    agent._execute()
                    self.env.cr.commit()
            except Exception as e:
                _logger.exception("Cron agent error for %s: %s", agent.name, e)
                self.env.cr.rollback()


class DevopsAiAgentRun(models.Model):
    _name = 'devops.ai.agent.run'
    _description = 'Ejecución de agente IA'
    _order = 'start_time desc'

    agent_id = fields.Many2one('devops.ai.agent', required=True, ondelete='cascade')
    status = fields.Selection([
        ('running', 'Ejecutando'),
        ('done', 'Completado'),
        ('error', 'Error'),
    ], default='running')
    start_time = fields.Datetime()
    end_time = fields.Datetime()
    commits_processed = fields.Integer(default=0)
    summary = fields.Text(help='Documentación generada')
    commits_json = fields.Text(help='JSON de commits procesados')
    error_message = fields.Text()

    @property
    def duration_display(self):
        if self.start_time and self.end_time:
            delta = self.end_time - self.start_time
            return f"{delta.total_seconds():.1f}s"
        return ''
