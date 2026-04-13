"""Git utilities using ssh_utils for local/remote execution."""
import logging
import os
import subprocess
from datetime import datetime

from . import ssh_utils

_logger = logging.getLogger(__name__)

# Standard .gitignore content for Odoo addon repositories
GITIGNORE_TEMPLATE = """\
# Python bytecode
__pycache__/
*.py[cod]
*$py.class

# Virtual environments
.venv/
venv/
env/

# IDE / Editor
.vscode/
.idea/
*.swp
*.swo
*~

# OS files
.DS_Store
Thumbs.db

# Odoo filestore & sessions
.local/
filestore/
sessions/

# Logs
*.log

# Environment / secrets
.env
.env.*
"""


def ensure_gitignore(repo_path, project=None):
    """Ensure a proper .gitignore exists in a git repository.

    Supports local and SSH projects. For SSH, all operations run remotely.
    Returns True if changes were made, False otherwise.
    """
    if not repo_path:
        return False

    from . import ssh_utils

    def _run(cmd_list):
        if project and project.connection_type == 'ssh' and project.ssh_host:
            return ssh_utils.execute_command(project, cmd_list, cwd=repo_path, timeout=15)
        return subprocess.run(cmd_list, capture_output=True, text=True, timeout=15, cwd=repo_path)

    def _shell(cmd_str):
        if project and project.connection_type == 'ssh' and project.ssh_host:
            return ssh_utils.execute_command_shell(project, cmd_str, cwd=repo_path, timeout=15)
        return subprocess.run(cmd_str, shell=True, capture_output=True, text=True, timeout=15, cwd=repo_path if not project else None)

    # Check if .git exists
    r = _run(['test', '-d', '.git'])
    if r.returncode != 0:
        return False

    # Read existing .gitignore
    r = _shell('cat .gitignore 2>/dev/null || true')
    existing = r.stdout if r.returncode == 0 else ''

    existing_lines = set(l.strip() for l in existing.splitlines() if l.strip() and not l.startswith('#'))
    template_lines = [l for l in GITIGNORE_TEMPLATE.splitlines() if l.strip() and not l.startswith('#')]
    missing = [l for l in template_lines if l.strip() not in existing_lines]

    changed = False
    if missing:
        if existing:
            append = '\n# Auto-added by pmb_devops\n' + '\n'.join(missing) + '\n'
            _shell(f'printf %s "{append}" >> .gitignore')
        else:
            escaped = GITIGNORE_TEMPLATE.replace('"', '\\"')
            _shell(f'printf "%s" "{escaped}" > .gitignore')
        changed = True

    # Remove tracked files that match .gitignore
    try:
        r = _run(['git', 'ls-files', '-ci', '--exclude-standard'])
        if r.returncode == 0 and r.stdout.strip():
            files = r.stdout.strip().split('\n')
            _run(['git', 'rm', '--cached'] + files)
            changed = True
    except Exception:
        pass

    if changed:
        try:
            _run(['git', 'add', '.gitignore'])
            status = _run(['git', 'diff', '--cached', '--quiet'])
            if status.returncode != 0:
                _run(['git', 'commit', '-m', 'chore: enforce .gitignore — remove tracked artifacts'])
        except Exception:
            pass

    return changed


def git_fetch(project, remote='origin'):
    """Fetch from remote."""
    try:
        ssh_utils.execute_command(
            project, ['git', 'fetch', remote, '--prune'],
            timeout=60, cwd=project.repo_path,
        )
    except Exception as e:
        _logger.warning("Error en git fetch: %s", e)


def git_list_branches(project):
    """List all branches with last commit info."""
    branches = []
    repo = project.repo_path

    # Local branches
    try:
        result = ssh_utils.execute_command(project, [
            'git', 'for-each-ref',
            '--format=%(refname:short)|||%(objectname:short)|||%(subject)|||%(authordate:iso)|||%(authorname)',
            'refs/heads/',
        ], cwd=repo)
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                parts = line.split('|||')
                if len(parts) >= 5:
                    branches.append({
                        'name': parts[0],
                        'hash': parts[1],
                        'message': parts[2],
                        'date': _parse_git_date(parts[3]),
                        'author': parts[4],
                        'is_remote': False,
                    })
    except Exception as e:
        _logger.warning("Error listando ramas locales: %s", e)

    # Remote branches (not already local)
    local_names = {b['name'] for b in branches}
    try:
        result = ssh_utils.execute_command(project, [
            'git', 'for-each-ref',
            '--format=%(refname:short)|||%(objectname:short)|||%(subject)|||%(authordate:iso)|||%(authorname)',
            'refs/remotes/origin/',
        ], cwd=repo)
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                parts = line.split('|||')
                if len(parts) >= 5:
                    name = parts[0]
                    if name.startswith('origin/'):
                        name = name[7:]
                    if name == 'HEAD' or name in local_names:
                        continue
                    branches.append({
                        'name': name,
                        'hash': parts[1],
                        'message': parts[2],
                        'date': _parse_git_date(parts[3]),
                        'author': parts[4],
                        'is_remote': True,
                    })
    except Exception as e:
        _logger.warning("Error listando ramas remotas: %s", e)

    return branches


def git_log(project, branch='HEAD', count=20, skip=0):
    """Get commit history."""
    commits = []
    try:
        cmd = [
            'git', 'log', f'-{count}',
            '--format=%H|||%h|||%s|||%ai|||%an|||%ae',
            branch,
        ]
        if skip:
            cmd.insert(3, f'--skip={skip}')
        result = ssh_utils.execute_command(project, cmd, cwd=project.repo_path)
        if result.returncode == 0:
            commits = _parse_log_output(result.stdout)
    except Exception as e:
        _logger.warning("Error en git log: %s", e)
    return commits


def git_search(project, branch='HEAD', query='', count=20):
    """Search commits by hash or message."""
    commits = []
    if not query:
        return commits
    try:
        # Try exact hash first
        if len(query) >= 7 and all(c in '0123456789abcdefABCDEF' for c in query):
            result = ssh_utils.execute_command(project, [
                'git', 'log', '-1',
                '--format=%H|||%h|||%s|||%ai|||%an|||%ae',
                query,
            ], cwd=project.repo_path)
            if result.returncode == 0 and result.stdout.strip():
                return _parse_log_output(result.stdout)

        # Search by message (grep)
        result = ssh_utils.execute_command(project, [
            'git', 'log', f'-{count}',
            '--format=%H|||%h|||%s|||%ai|||%an|||%ae',
            f'--grep={query}', '--regexp-ignore-case',
            branch,
        ], cwd=project.repo_path, timeout=15)
        if result.returncode == 0:
            commits = _parse_log_output(result.stdout)
    except Exception as e:
        _logger.warning("Error en git search: %s", e)
    return commits


def _parse_log_output(output):
    """Parse git log formatted output into commit dicts."""
    commits = []
    for line in output.strip().split('\n'):
        if not line.strip():
            continue
        parts = line.split('|||')
        if len(parts) >= 6:
            commits.append({
                'full_hash': parts[0],
                'short_hash': parts[1],
                'message': parts[2],
                'date': parts[3],
                'author': parts[4],
                'email': parts[5],
            })
    return commits


def git_current_branch(project):
    """Get current branch name."""
    try:
        result = ssh_utils.execute_command(
            project, ['git', 'branch', '--show-current'],
            cwd=project.repo_path,
        )
        return result.stdout.strip() if result.returncode == 0 else ''
    except Exception:
        return ''


def git_status(project):
    """Get repo status."""
    try:
        result = ssh_utils.execute_command(
            project, ['git', 'status', '--porcelain'],
            cwd=project.repo_path,
        )
        return result.stdout.strip() if result.returncode == 0 else ''
    except Exception:
        return ''


def _parse_git_date(date_str):
    """Parse git date string to datetime."""
    try:
        date_str = date_str.strip()
        if ' ' in date_str:
            parts = date_str.rsplit(' ', 1)
            date_str = parts[0]
        return datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S')
    except Exception:
        return None
