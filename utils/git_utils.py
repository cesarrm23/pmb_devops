"""Git utilities using ssh_utils for local/remote execution."""
import logging
from datetime import datetime

from . import ssh_utils

_logger = logging.getLogger(__name__)


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


def git_log(project, branch='HEAD', count=20):
    """Get commit history."""
    commits = []
    try:
        result = ssh_utils.execute_command(project, [
            'git', 'log', f'-{count}',
            '--format=%H|||%h|||%s|||%ai|||%an|||%ae',
            branch,
        ], cwd=project.repo_path)
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
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
    except Exception as e:
        _logger.warning("Error en git log: %s", e)
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
