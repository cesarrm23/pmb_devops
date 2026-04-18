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
        ('claude-opus-4.7', 'Claude Opus 4.7'),
        ('claude-sonnet-4.6', 'Claude Sonnet 4.6'),
        ('claude-haiku-4.5', 'Claude Haiku 4.5'),
        ('gpt-5.4', 'GPT-5.4'),
        ('gpt-5-mini', 'GPT-5 mini'),
        ('gpt-4.1', 'GPT-4.1'),
        ('gpt-4o', 'GPT-4o'),
        ('gemini-3.1-pro-preview', 'Gemini 3.1 Pro'),
    ], default='claude-opus-4.7')
    project_id = fields.Many2one('devops.project', required=True, ondelete='cascade')
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)

    # Schedule
    interval_number = fields.Integer(default=1, string='Cada')
    interval_type = fields.Selection([
        ('minutes', 'Minutos'),
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
    output_file = fields.Char(
        string='Archivo de documentación',
        help=(
            'Ruta relativa al primer repo del proyecto donde se escribirá la '
            'documentación generada (ej: CHANGELOG.md). Si el archivo existe, '
            'se inserta un bloque nuevo entre los marcadores '
            '<!-- AGENT:CHANGES:START --> y <!-- AGENT:CHANGES:END -->.'
        ),
    )
    output_type = fields.Selection(
        [('file', 'Archivo Markdown en repo'),
         ('knowledge', 'Artículo de Knowledge (Odoo remoto)')],
        string='Tipo de salida',
        default='file',
        required=True,
        help=(
            '"Archivo": escribe Markdown dentro del repo del proyecto.  '
            '"Knowledge": escribe el bloque dentro de un artículo del '
            'módulo Knowledge en la instancia remota del proyecto (XML-RPC).'
        ),
    )
    output_knowledge_title = fields.Char(
        string='Título del artículo Knowledge',
        help='Título del artículo que se creará/actualizará en Knowledge.',
    )
    output_knowledge_article_id = fields.Integer(
        string='ID remoto del artículo Knowledge',
        readonly=True,
        help='ID de knowledge.article en la instancia remota, cacheado tras la primera ejecución.',
    )
    output_modules_root = fields.Char(
        string='Raíz de módulos personalizados',
        help=(
            'Directorio (relativo al repo) que agrupa módulos Odoo propios del '
            'proyecto, por ejemplo "addons_maha". Si se define, el agente también '
            'generará un archivo data/knowledge_doc.xml dentro de cada módulo '
            'afectado por los commits, con un registro knowledge.article '
            'específico para ese módulo.'
        ),
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

        repos = self._find_git_repos()

        # Fetch latest on EACH discovered repo (the top-level repo_path is
        # not always a git repo — for MAHA the actual repo lives one level
        # deeper at addons_maha/). Without this, the cron never sees new
        # remote commits pushed to the branch the agent tracks.
        for repo in repos:
            try:
                ssh_utils.execute_command(
                    project, ['git', 'fetch', '--all', '--prune'],
                    timeout=60, cwd=repo,
                )
            except Exception as e:
                _logger.warning("Agent %s: git fetch in %s failed: %s",
                                self.name, repo, e)
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

    def _get_commit_files(self, commit_hash, repo_path=None):
        """Return the list of files changed by a commit (relative to repo).

        Uses `git diff-tree --no-commit-id --name-only -r <hash>` which works
        on both merge and non-merge commits, and avoids the `~1` failure on
        the repo's initial commit.
        """
        from ..utils import ssh_utils
        cwd = repo_path or self.project_id.repo_path
        try:
            result = ssh_utils.execute_command(
                self.project_id,
                ['git', 'diff-tree', '--no-commit-id', '--name-only', '-r', commit_hash],
                cwd=cwd, timeout=10,
            )
            if result.returncode == 0:
                return [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
        except Exception as e:
            _logger.warning("Agent %s: diff-tree failed for %s: %s", self.name, commit_hash, e)
        return []

    def _group_commits_by_module(self, commits):
        """Group commits by the custom Odoo module they touched.

        Supports two repo layouts:
          A) Monorepo: the git repo contains an `output_modules_root/` dir
             (e.g. a main repo with `addons_maha/<module>/...`).
          B) Addons-root repo: the git repo ITSELF is the addons root and
             its trailing path segment equals `output_modules_root`
             (e.g. the repo cloned at `/opt/maha/addons_maha`).

        In both cases the module name is inferred and the absolute module
        path is returned so the caller can write to
        `<module_path>/data/knowledge_doc.xml`.
        """
        if not self.output_modules_root:
            return {}
        root = self.output_modules_root.strip('/')
        buckets = {}
        for c in commits:
            repo_path = (c.get('repo_path') or self.project_id.repo_path or '').rstrip('/')
            repo_is_root = repo_path.split('/')[-1] == root
            files = self._get_commit_files(c['full_hash'], c.get('repo_path'))
            seen_here = set()
            for f in files:
                parts = f.split('/')
                mod = None
                module_path = None
                if root in parts:
                    idx = parts.index(root)
                    if idx + 1 < len(parts):
                        mod = parts[idx + 1]
                        module_path = repo_path + '/' + '/'.join(parts[:idx + 2])
                elif repo_is_root and len(parts) >= 2:
                    mod = parts[0]
                    module_path = repo_path + '/' + mod
                if not mod or mod in seen_here:
                    continue
                seen_here.add(mod)
                entry = buckets.setdefault(mod, {
                    'repo_path': repo_path,
                    'module_path': module_path,
                    'commits': [],
                })
                entry['commits'].append(c)
        return buckets

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
        """Exchange GitHub OAuth token for a Copilot API token.

        The GitHub token is stored per-project on `devops.project` so that
        each project can authenticate with its own GitHub account. Falls back
        to the legacy global `ir.config_parameter` value for backwards
        compatibility with older configurations.
        """
        from datetime import datetime
        project = self.project_id
        github_token = project.copilot_github_token
        if not github_token:
            # Legacy fallback — migrate value into the project so future calls
            # use the per-project storage.
            ICP = self.env['ir.config_parameter'].sudo()
            legacy = ICP.get_param('pmb_devops.github_token', '')
            if legacy:
                project.sudo().write({
                    'copilot_github_token': legacy,
                    'copilot_github_user': ICP.get_param('pmb_devops.github_user', ''),
                })
                github_token = legacy
        if not github_token:
            raise Exception(
                f'GitHub Copilot no conectado para el proyecto "{project.name}". '
                'Ve a Ajustes del proyecto > GitHub Copilot para conectar.'
            )

        # Check cached copilot token (per project)
        cached = project.copilot_token
        expires = project.copilot_token_expires
        if cached and expires and datetime.utcnow() < expires.replace(tzinfo=None):
            return cached

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
                f"Verifica que la cuenta GitHub del proyecto tenga Copilot activo. "
                f"Respuesta: {proc.stdout[:200]}"
            )

        # Cache token on the project record
        exp_val = False
        expires_at = resp.get('expires_at', 0)
        if expires_at:
            exp_val = datetime.utcfromtimestamp(expires_at)
        project.sudo().write({
            'copilot_token': token,
            'copilot_token_expires': exp_val,
        })

        return token

    def _call_copilot(self, system_prompt, user_message):
        """Call GitHub Copilot Chat API (OpenAI-compatible)."""
        token = self._get_copilot_token()

        model = self.copilot_model or 'claude-opus-4.7'
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

    _CHANGES_MARKER_START = '<!-- AGENT:CHANGES:START -->'
    _CHANGES_MARKER_END = '<!-- AGENT:CHANGES:END -->'

    def _default_output_template(self):
        """Template used when creating the output file for the first time."""
        return (
            f"# Cambios — {self.project_id.name}\n\n"
            f"Documentación generada automáticamente por el agente IA "
            f"`{self.name}` (pmb_devops). Los registros más recientes "
            f"aparecen al inicio del bloque.\n\n"
            f"{self._CHANGES_MARKER_START}\n"
            f"{self._CHANGES_MARKER_END}\n"
        )

    def _update_output_file(self, documentation, commits):
        """Insert generated docs into the output_file between AGENT:CHANGES markers.

        Returns the absolute path written, or '' if skipped.
        """
        self.ensure_one()
        from ..utils import ssh_utils
        import os

        repos = self._find_git_repos()
        if not repos:
            return ''
        target = os.path.join(repos[0], self.output_file)

        existing = ssh_utils.read_text(self.project_id, target)
        if not existing or self._CHANGES_MARKER_START not in existing:
            existing = self._default_output_template()

        now = fields.Datetime.now().strftime('%Y-%m-%d %H:%M UTC')
        entry = (
            f"\n## {now} — {len(commits)} commit(s)\n\n"
            f"{documentation.strip()}\n\n"
            f"---\n"
        )

        start = existing.index(self._CHANGES_MARKER_START) + len(self._CHANGES_MARKER_START)
        end_idx = existing.index(self._CHANGES_MARKER_END, start)
        new_content = (
            existing[:start]
            + entry
            + existing[start:end_idx]
            + existing[end_idx:]
        )

        ok = ssh_utils.write_text(self.project_id, target, new_content)
        return target if ok else ''

    def _markdown_to_html(self, md_text):
        """Very small Markdown→HTML pass suitable for Knowledge bodies.

        Knowledge accepts arbitrary HTML in `body`. We do not pull a heavy
        Markdown library — the LLM output is simple enough. Headings,
        bold/italic, inline code, fenced code blocks, and paragraph breaks
        are covered; anything else passes through as-is inside <p>.
        """
        import re
        import html as _html

        text = md_text or ''
        lines = text.split('\n')
        out = []
        in_code = False
        code_buf = []
        para_buf = []

        def flush_para():
            if para_buf:
                joined = ' '.join(para_buf).strip()
                if joined:
                    # inline markers
                    joined = re.sub(r'`([^`]+)`', r'<code>\1</code>', joined)
                    joined = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', joined)
                    joined = re.sub(r'\*([^*]+)\*', r'<em>\1</em>', joined)
                    out.append(f'<p>{joined}</p>')
                para_buf.clear()

        for raw in lines:
            if raw.startswith('```'):
                flush_para()
                if in_code:
                    out.append('<pre><code>' + _html.escape('\n'.join(code_buf)) + '</code></pre>')
                    code_buf = []
                    in_code = False
                else:
                    in_code = True
                continue
            if in_code:
                code_buf.append(raw)
                continue
            stripped = raw.strip()
            if not stripped:
                flush_para()
                continue
            if stripped.startswith('### '):
                flush_para()
                out.append(f'<h4>{_html.escape(stripped[4:])}</h4>')
            elif stripped.startswith('## '):
                flush_para()
                out.append(f'<h3>{_html.escape(stripped[3:])}</h3>')
            elif stripped.startswith('# '):
                flush_para()
                out.append(f'<h2>{_html.escape(stripped[2:])}</h2>')
            elif stripped.startswith('- ') or stripped.startswith('* '):
                flush_para()
                out.append(f'<li>{_html.escape(stripped[2:])}</li>')
            else:
                para_buf.append(stripped)
        flush_para()
        if in_code:
            out.append('<pre><code>' + _html.escape('\n'.join(code_buf)) + '</code></pre>')
        return '\n'.join(out)

    def _update_knowledge_article(self, documentation, commits):
        """Insert the generated docs block into a Knowledge article in the
        project's remote Odoo instance, between HTML marker comments.

        On first run the article is created and its ID cached in
        `output_knowledge_article_id`. Returns a human-readable string like
        `https://odoo.maha.com.mx/knowledge/article/123` or '' on failure.
        """
        self.ensure_one()
        project = self.project_id
        conn = project._get_production_xmlrpc()
        if not conn:
            _logger.warning("Agent %s: no remote XML-RPC configured for project", self.name)
            return ''
        uid, models_proxy, db, login, password = conn
        title = (self.output_knowledge_title
                 or f'Cambios — {project.name} ({self.name})').strip()

        # Build the new entry as HTML
        now = fields.Datetime.now().strftime('%Y-%m-%d %H:%M UTC')
        doc_html = self._markdown_to_html(documentation)
        entry_html = (
            f'<h3>{now} — {len(commits)} commit(s)</h3>\n'
            f'{doc_html}\n<hr/>\n'
        )

        start_marker = '<!-- AGENT:CHANGES:START -->'
        end_marker = '<!-- AGENT:CHANGES:END -->'

        # Resolve or create article
        article_id = self.output_knowledge_article_id
        if article_id:
            try:
                existing = models_proxy.execute_kw(
                    db, uid, password, 'knowledge.article', 'read',
                    [[article_id], ['id', 'name', 'body']],
                )
                if not existing:
                    article_id = 0
            except Exception as e:
                _logger.warning("Agent %s: remote article read failed: %s", self.name, e)
                article_id = 0

        if not article_id:
            initial_body = (
                f'<p>Documentación generada automáticamente por el agente IA '
                f'<strong>{title}</strong> (pmb_devops). Los registros más '
                f'recientes aparecen al inicio del bloque.</p>\n'
                f'{start_marker}\n{end_marker}\n'
            )
            try:
                article_id = models_proxy.execute_kw(
                    db, uid, password, 'knowledge.article', 'create',
                    [{
                        'name': title,
                        'body': initial_body,
                        'internal_permission': 'write',
                        'is_article_visible_by_everyone': True,
                    }],
                )
                self.sudo().write({'output_knowledge_article_id': article_id})
                body = initial_body
            except Exception as e:
                _logger.warning("Agent %s: remote article create failed: %s", self.name, e)
                return ''
        else:
            body = existing[0].get('body') or ''
            if start_marker not in body or end_marker not in body:
                body = (body + f'\n{start_marker}\n{end_marker}\n') if body else (
                    f'{start_marker}\n{end_marker}\n'
                )

        start_idx = body.index(start_marker) + len(start_marker)
        end_idx = body.index(end_marker, start_idx)
        new_body = body[:start_idx] + '\n' + entry_html + body[start_idx:end_idx] + body[end_idx:]

        try:
            models_proxy.execute_kw(
                db, uid, password, 'knowledge.article', 'write',
                [[article_id], {
                    'body': new_body,
                    'internal_permission': 'write',
                    'is_article_visible_by_everyone': True,
                }],
            )
        except Exception as e:
            _logger.warning("Agent %s: remote article write failed: %s", self.name, e)
            return ''

        # Build a URL the user can open
        if project.connection_type == 'ssh' and project.ssh_host:
            base = f'https://{project.domain or project.ssh_host}'
        else:
            base = f'https://{project.domain}' if project.domain else ''
        return f'{base}/knowledge/article/{article_id}' if base else str(article_id)

    _MODULE_MARKER_START = '<!-- AGENT:CHANGES:START -->'
    _MODULE_MARKER_END = '<!-- AGENT:CHANGES:END -->'

    def _build_module_user_message(self, module_name, module_commits):
        """Smaller per-module prompt so the LLM focuses on this module's changes."""
        parts = [
            f"Módulo: {module_name}",
            f"Proyecto: {self.project_id.name}",
            f"Commits que tocan este módulo ({len(module_commits)}):",
            "",
        ]
        for c in module_commits:
            stat, diff = self._get_commit_diff(c['full_hash'], c.get('repo_path'))
            parts.append(f"### {c['short_hash']} — {c['message']}")
            parts.append(f"Autor: {c['author']} | Fecha: {c['date']}")
            if stat:
                parts.append(f"```\n{stat}\n```")
            if diff:
                parts.append(f"Diff (parcial):\n```diff\n{diff[:2000]}\n```")
            parts.append("")
        return "\n".join(parts)

    def _ensure_data_in_manifest(self, manifest_path, data_file):
        """Ensure `data_file` is in the 'data' list of the manifest, and bump
        version last segment. Returns True if manifest was modified.

        Parses and rewrites the manifest as text (not ast.literal_eval) to
        avoid losing formatting/comments. Idempotent.
        """
        import re
        from ..utils import ssh_utils

        content = ssh_utils.read_text(self.project_id, manifest_path)
        if not content:
            return False
        original = content

        # Ensure data_file is listed in 'data': [ ... ]
        if data_file not in content:
            # Try to inject into an existing 'data': [ ... ]
            m = re.search(r"(['\"]data['\"]\s*:\s*\[)(.*?)(\])", content, flags=re.DOTALL)
            if m:
                inner = m.group(2).rstrip()
                if inner and not inner.rstrip().endswith(','):
                    inner = inner + ','
                indent = '        '
                new_inner = f"{inner}\n{indent}'{data_file}',\n    "
                content = content[:m.start(2)] + new_inner + content[m.end(2):]
            else:
                # No 'data' key at all — inject one right before the closing brace
                close = content.rfind('}')
                if close == -1:
                    return False
                inject = f"    'data': ['{data_file}'],\n"
                content = content[:close] + inject + content[close:]

        # Bump version — increment last numeric segment
        def _bump(match):
            ver = match.group(2)
            segs = ver.split('.')
            if segs and segs[-1].isdigit():
                segs[-1] = str(int(segs[-1]) + 1)
                return f"{match.group(1)}{'.'.join(segs)}{match.group(3)}"
            return match.group(0)
        content = re.sub(
            r"(['\"]version['\"]\s*:\s*['\"])([0-9\.]+)(['\"])",
            _bump, content, count=1,
        )

        if content == original:
            return False
        ssh_utils.write_text(self.project_id, manifest_path, content)
        return True

    def _build_knowledge_doc_xml(self, module_name, existing_body_html, new_entry_html):
        """Compose the full XML data file with a knowledge.article record for
        this module. Merges `new_entry_html` inside the existing body between
        the AGENT:CHANGES markers (newest first).
        """
        start = self._MODULE_MARKER_START
        end = self._MODULE_MARKER_END
        body = existing_body_html or ''
        if start not in body or end not in body:
            body = (
                f'<p>Documentación del módulo <strong>{module_name}</strong>, '
                f'generada automáticamente por el agente IA del proyecto '
                f'{self.project_id.name}. Los registros más recientes aparecen '
                f'al inicio del bloque.</p>\n'
                f'{start}\n{end}\n'
            )
        s_idx = body.index(start) + len(start)
        e_idx = body.index(end, s_idx)
        new_body = body[:s_idx] + '\n' + new_entry_html + body[s_idx:e_idx] + body[e_idx:]

        # Wrap body inside CDATA so HTML is safe in XML
        xml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<odoo>\n'
            '    <!-- Generado automáticamente por pmb_devops / devops.ai.agent -->\n'
            f'    <record id="knowledge_doc_{module_name}" model="knowledge.article">\n'
            f'        <field name="name">Documentación — {module_name}</field>\n'
            '        <field name="body" type="html">\n'
            '<![CDATA[\n'
            f'{new_body}'
            '\n]]>\n'
            '        </field>\n'
            '    </record>\n'
            '</odoo>\n'
        )
        return xml, new_body

    def _extract_existing_body_from_xml(self, xml_content):
        """Given the contents of a previous knowledge_doc.xml, extract the
        HTML body inside the CDATA so we can merge the new entry in.
        """
        if not xml_content:
            return ''
        import re
        m = re.search(r'<!\[CDATA\[(.*?)\]\]>', xml_content, flags=re.DOTALL)
        return m.group(1).strip() if m else ''

    def _update_module_docs(self, buckets, system_prompt):
        """For each affected module, generate per-module documentation,
        write `data/knowledge_doc.xml`, and bump manifest.

        Returns a list of dicts with info per module, suitable for summarizing
        in the run record.
        """
        from ..utils import ssh_utils
        results = []
        if not buckets:
            return results

        for mod_name, info in buckets.items():
            module_path = info['module_path']
            manifest_path = module_path.rstrip('/') + '/__manifest__.py'

            # Confirm the module really has a manifest (avoid writing to random dirs)
            probe = ssh_utils.execute_command(
                self.project_id, ['test', '-f', manifest_path],
                cwd=info['repo_path'], timeout=5,
            )
            if probe.returncode != 0:
                _logger.warning("Agent %s: skip module %s — no manifest at %s",
                                self.name, mod_name, manifest_path)
                continue

            # Generate per-module docs via LLM
            user_msg = self._build_module_user_message(mod_name, info['commits'])
            try:
                doc_md = self._call_llm(system_prompt, user_msg)
            except Exception as e:
                _logger.warning("Agent %s: LLM failed for module %s: %s",
                                self.name, mod_name, e)
                continue

            now = fields.Datetime.now().strftime('%Y-%m-%d %H:%M UTC')
            entry_html = (
                f'<h3>{now} — {len(info["commits"])} commit(s)</h3>\n'
                f'{self._markdown_to_html(doc_md)}\n<hr/>\n'
            )

            data_dir = module_path.rstrip('/') + '/data'
            # Make sure data/ exists
            ssh_utils.execute_command_shell(
                self.project_id, f'mkdir -p {data_dir}', timeout=5,
            )
            xml_path = data_dir + '/knowledge_doc.xml'
            existing_xml = ssh_utils.read_text(self.project_id, xml_path) or ''
            existing_body = self._extract_existing_body_from_xml(existing_xml)

            new_xml, _ = self._build_knowledge_doc_xml(mod_name, existing_body, entry_html)
            ok = ssh_utils.write_text(self.project_id, xml_path, new_xml)

            manifest_touched = False
            if ok:
                try:
                    manifest_touched = self._ensure_data_in_manifest(
                        manifest_path, 'data/knowledge_doc.xml',
                    )
                except Exception as e:
                    _logger.warning("Agent %s: manifest update failed for %s: %s",
                                    self.name, mod_name, e)

            results.append({
                'module': mod_name,
                'xml_path': xml_path if ok else '',
                'manifest_bumped': manifest_touched,
                'commits': len(info['commits']),
            })

        return results

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

            output_path_written = ''
            if self.output_type == 'knowledge':
                try:
                    output_path_written = self._update_knowledge_article(documentation, commits)
                except Exception as e:
                    _logger.warning("Agent %s: knowledge write failed: %s", self.name, e)
            elif self.output_file:
                try:
                    output_path_written = self._update_output_file(documentation, commits)
                except Exception as e:
                    _logger.warning("Agent %s: output_file write failed: %s", self.name, e)

            # Per-module docs into <module>/data/knowledge_doc.xml
            module_results = []
            if self.output_modules_root:
                try:
                    buckets = self._group_commits_by_module(commits)
                    module_results = self._update_module_docs(buckets, system_prompt)
                    if module_results:
                        _logger.info(
                            "Agent %s: wrote docs for %d modules: %s",
                            self.name, len(module_results),
                            ', '.join(m['module'] for m in module_results),
                        )
                except Exception as e:
                    _logger.warning("Agent %s: module docs write failed: %s", self.name, e)

            summary_body = documentation
            if module_results:
                bullets = "\n".join(
                    f"- {m['module']}: {m['xml_path']}"
                    + (" (manifest bumped)" if m['manifest_bumped'] else "")
                    for m in module_results
                )
                summary_body = (
                    f"{documentation}\n\n"
                    f"---\n\n"
                    f"### Archivos knowledge_doc.xml generados ({len(module_results)})\n\n"
                    f"{bullets}\n"
                )

            run.write({
                'status': 'done',
                'end_time': fields.Datetime.now(),
                'commits_processed': len(commits),
                'summary': summary_body,
                'commits_json': json.dumps([{
                    'hash': c['short_hash'],
                    'message': c['message'],
                    'author': c['author'],
                } for c in commits]),
                'output_path': output_path_written,
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
                    minutes=agent.interval_number if agent.interval_type == 'minutes' else 0,
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
    output_path = fields.Char(help='Ruta del archivo Markdown actualizado')

    @property
    def duration_display(self):
        if self.start_time and self.end_time:
            delta = self.end_time - self.start_time
            return f"{delta.total_seconds():.1f}s"
        return ''
