"""Unified command execution: local subprocess or SSH remote."""
import logging
import subprocess

_logger = logging.getLogger(__name__)


def execute_command(project, command, timeout=30, cwd=None):
    """Execute command locally or via SSH based on project connection_type.

    Args:
        project: devops.project recordset (needs connection_type, ssh_* fields)
        command: list of strings (e.g. ['git', 'status'])
        timeout: seconds
        cwd: working directory (local) or remote cd path (ssh)

    Returns:
        subprocess.CompletedProcess
    """
    if project.connection_type == 'local':
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd or project.repo_path,
        )
    else:
        return _execute_ssh(project, command, timeout, cwd)


def _execute_ssh(project, command, timeout=30, cwd=None):
    """Execute command on remote server via SSH."""
    ssh_args = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=10']
    if project.ssh_key_path:
        ssh_args += ['-i', project.ssh_key_path]
    if project.ssh_port and project.ssh_port != 22:
        ssh_args += ['-p', str(project.ssh_port)]
    ssh_args.append(f'{project.ssh_user}@{project.ssh_host}')

    cmd_str = ' '.join(_shell_quote(c) for c in command)
    if cwd:
        cmd_str = f'cd {_shell_quote(cwd)} && {cmd_str}'
    ssh_args.append(cmd_str)

    return subprocess.run(
        ssh_args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def execute_command_shell(project, cmd_str, timeout=30, cwd=None):
    """Execute a shell command string (for pipes, redirects, etc.)."""
    if project.connection_type == 'local':
        full_cmd = cmd_str
        if cwd:
            full_cmd = f'cd {_shell_quote(cwd)} && {cmd_str}'
        return subprocess.run(
            full_cmd, shell=True,
            capture_output=True, text=True, timeout=timeout,
        )
    else:
        return _execute_ssh(project, [cmd_str], timeout, cwd)


def _shell_quote(s):
    """Simple shell quoting."""
    if not s:
        return "''"
    if all(c.isalnum() or c in '-_./=:@' for c in s):
        return s
    return "'" + s.replace("'", "'\"'\"'") + "'"
