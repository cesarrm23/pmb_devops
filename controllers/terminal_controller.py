import json
import logging
import os
import signal
import subprocess
import time

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

TERMINAL_DIR = '/tmp/odoo_devops_terminals'

BRIDGE_SCRIPT = r'''#!/usr/bin/env python3
"""Terminal bridge: ejecuta un comando en PTY y expone I/O via archivos."""
import pty, os, sys, select, signal, time, fcntl, termios, struct

cmd = sys.argv[1:]
session_dir = os.environ['SESSION_DIR']
input_file = os.path.join(session_dir, 'input')
output_file = os.path.join(session_dir, 'output')
pid_file = os.path.join(session_dir, 'pid')
alive_file = os.path.join(session_dir, 'alive')

open(input_file, 'w').close()
open(output_file, 'w').close()
open(alive_file, 'w').write('1')

master, slave = pty.openpty()

try:
    winsize = struct.pack('HHHH', 40, 120, 0, 0)
    fcntl.ioctl(master, termios.TIOCSWINSZ, winsize)
except:
    pass

env = dict(os.environ)
env['TERM'] = 'xterm-256color'

pid = os.fork()
if pid == 0:
    os.close(master)
    os.setsid()
    os.dup2(slave, 0)
    os.dup2(slave, 1)
    os.dup2(slave, 2)
    if slave > 2:
        os.close(slave)
    os.execvpe(cmd[0], cmd, env)

os.close(slave)
open(pid_file, 'w').write(str(pid))

last_input_pos = 0
try:
    while True:
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            try:
                data = os.read(master, 8192)
                if data:
                    with open(output_file, 'ab') as f:
                        f.write(data)
                else:
                    break
            except OSError:
                break

        try:
            with open(input_file, 'rb') as f:
                f.seek(last_input_pos)
                new_input = f.read()
                if new_input:
                    last_input_pos += len(new_input)
                    os.write(master, new_input)
        except (OSError, IOError):
            pass

        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid != 0:
                break
        except ChildProcessError:
            break
except KeyboardInterrupt:
    pass
finally:
    open(alive_file, 'w').write('0')
    try:
        os.kill(pid, signal.SIGTERM)
    except:
        pass
    try:
        os.close(master)
    except:
        pass
'''


def _get_session_dir(uid, session_type, instance_id=None):
    """Return the session directory path for a given user, session type, and instance."""
    suffix = f'_{instance_id}' if instance_id else ''
    session_dir = os.path.join(TERMINAL_DIR, f'user_{uid}', f'{session_type}{suffix}')
    return session_dir


def _write_bridge_script():
    """Write the bridge script to disk and return its path."""
    os.makedirs(TERMINAL_DIR, exist_ok=True)
    bridge_path = os.path.join(TERMINAL_DIR, 'bridge.py')
    with open(bridge_path, 'w') as f:
        f.write(BRIDGE_SCRIPT)
    os.chmod(bridge_path, 0o755)
    return bridge_path


class DevopsTerminalController(http.Controller):

    @http.route('/devops/terminal/start', type='json', auth='user')
    def terminal_start(self, session_type='shell', service=None, instance_id=None):
        """Start a terminal session (claude/shell/logs)."""
        uid = request.env.uid
        session_dir = _get_session_dir(uid, session_type, instance_id)

        # Stop any existing session of this type for this instance
        self._stop_session(uid, session_type, instance_id)

        # Create session directory
        os.makedirs(session_dir, exist_ok=True)

        # Determine working directory
        cwd = os.path.expanduser('~')

        # If instance_id provided, use its config
        if instance_id:
            instance = request.env['devops.instance'].browse(instance_id)
            if instance.exists():
                # Set cwd based on session type and instance type
                if session_type == 'logs':
                    # Logs don't need a specific cwd, just the service name
                    cwd = '/tmp'
                elif instance.instance_type == 'production':
                    # Production: try repo_path, then instance_path
                    repo = instance.project_id.repo_path
                    if repo and os.path.isdir(repo) and os.access(repo, os.R_OK):
                        cwd = repo
                    elif instance.instance_path and os.path.isdir(instance.instance_path):
                        cwd = instance.instance_path
                elif instance.instance_path and os.path.isdir(instance.instance_path):
                    cwd = instance.instance_path
                elif instance.project_id.repo_path and os.path.isdir(instance.project_id.repo_path):
                    cwd = instance.project_id.repo_path

                # Ensure unique cwd per instance (avoid HOME collision) — local only
                if cwd == os.path.expanduser('~') and instance.instance_path:
                    proj = instance.project_id
                    if not (proj.connection_type == 'ssh' and proj.ssh_host):
                        os.makedirs(instance.instance_path, exist_ok=True)
                        cwd = instance.instance_path

                # For logs, get service name from instance
                if session_type == 'logs' and instance.service_name:
                    service = instance.service_name

                # Update activity
                instance._update_activity()

        # Check if this is a remote SSH project
        is_ssh = False
        ssh_cmd_prefix = []
        if instance_id:
            inst = request.env['devops.instance'].browse(instance_id)
            if inst.exists():
                proj = inst.project_id
                if proj.connection_type == 'ssh' and proj.ssh_host:
                    is_ssh = True
                    ssh_cmd_prefix = ['ssh', '-t', '-o', 'StrictHostKeyChecking=no']
                    if proj.ssh_key_path and os.path.isfile(proj.ssh_key_path):
                        ssh_cmd_prefix += ['-i', proj.ssh_key_path]
                    if proj.ssh_port and proj.ssh_port != 22:
                        ssh_cmd_prefix += ['-p', str(proj.ssh_port)]
                    ssh_cmd_prefix += [f'{proj.ssh_user or "root"}@{proj.ssh_host}']
                    # Use project-specific cwd to avoid collisions
                    cwd = os.path.join(os.path.expanduser('~'), '.pmb_ssh', f'project_{proj.id}')
                    os.makedirs(cwd, exist_ok=True)

        # Determine command based on session type
        if is_ssh:
            remote_cwd = inst.project_id.repo_path or '/opt'
            # Docker-runtime staging/development: exec into the odoo
            # container so claude + shell see the bind-mounted addons at
            # /mnt/addons, not the host checkout. Production stays on the
            # host (no exec) to avoid surprise.
            is_docker_nonprod = (
                inst.project_id.runtime == 'docker'
                and inst.instance_type in ('staging', 'development')
                and inst.docker_compose_path
            )
            if is_docker_nonprod:
                # Derive container name the same way devops_instance_docker
                # does: pmb-<project_code>-<instance_name>-odoo.
                import re as _re_docker
                _code = _re_docker.sub(r'[^a-z0-9]', '', (inst.project_id.name or '').lower()) or 'proj'
                _safe = _re_docker.sub(r'[^a-z0-9-]', '-', (inst.name or '').lower()).strip('-') or 'inst'
                odoo_container = f'pmb-{_code}-{_safe}-odoo'
            if session_type == 'claude':
                if is_docker_nonprod:
                    # Claude is baked into the pmb/odoo:<ver> image at
                    # build time (Dockerfile: npm install -g
                    # @anthropic-ai/claude-code). /mnt/addons is the
                    # bind-mounted project checkout.
                    cmd = ssh_cmd_prefix + [
                        f'docker exec -it {odoo_container} bash -lc '
                        f'"cd /mnt/addons && claude"'
                    ]
                else:
                    cmd = ssh_cmd_prefix + [f'cd {remote_cwd} && claude']
            elif session_type == 'shell':
                if is_docker_nonprod:
                    cmd = ssh_cmd_prefix + [
                        f'docker exec -it {odoo_container} bash -lc '
                        f'"cd /mnt/addons && bash -i"'
                    ]
                else:
                    cmd = ssh_cmd_prefix + [f'cd {remote_cwd} && bash -i']
            elif session_type == 'logs':
                svc = service or (inst.service_name if inst else '')
                if is_docker_nonprod:
                    cmd = ssh_cmd_prefix + [f'docker logs -f --tail 200 {odoo_container}']
                else:
                    cmd = ssh_cmd_prefix + [f'journalctl -u {svc}.service -f -n 200 --no-pager --output=short-iso']
            elif session_type == 'odoo_log':
                svc = service or (inst.service_name if inst else '')
                # Detect logfile from remote config
                from ..utils import ssh_utils
                import re as _re
                _exec = ssh_utils.execute_command_shell(
                    proj, f'systemctl show {svc}.service --property=ExecStart', timeout=10,
                ).stdout.strip()
                _m = _re.search(r'-c\s+(\S+)', _exec)
                _conf = _m.group(1) if _m else ''
                _logfile = ''
                if _conf:
                    _cc = ssh_utils.execute_command_shell(proj, f'grep "^logfile" {_conf} 2>/dev/null', timeout=5).stdout.strip()
                    if _cc and '=' in _cc:
                        _logfile = _cc.partition('=')[2].strip()
                if not _logfile:
                    _logfile = f'/var/log/odoo/{svc}.log'
                cmd = ssh_cmd_prefix + [f'tail -f -n 200 {_logfile} 2>/dev/null || journalctl -u {svc}.service -f -n 200']
            else:
                return {'error': f'Unknown session type: {session_type}'}
        elif session_type == 'claude':
            cmd = ['claude']
        elif session_type == 'shell':
            cmd = ['/bin/bash', '-i']
        elif session_type == 'logs':
            if not service:
                return {'error': 'Service name required for logs session'}
            cmd = [
                'journalctl', '-u', f'{service}.service',
                '-f', '-n', '200', '--no-pager', '--output=short-iso',
            ]
        elif session_type == 'odoo_log':
            # Auto-detect logfile from service config
            import re as _re
            logfile = ''
            svc_name = service
            proj = None
            if instance_id:
                inst = request.env['devops.instance'].browse(instance_id)
                if inst.exists():
                    svc_name = inst.service_name or svc_name
                    proj = inst.project_id
            if svc_name:
                # Read config path from systemd, then logfile from config
                from ..utils import ssh_utils
                if proj:
                    exec_out = ssh_utils.execute_command_shell(
                        proj, f'systemctl show {svc_name}.service --property=ExecStart', timeout=10,
                    ).stdout.strip()
                else:
                    exec_out = subprocess.run(
                        f'systemctl show {svc_name}.service --property=ExecStart',
                        shell=True, capture_output=True, text=True, timeout=5,
                    ).stdout.strip()
                m = _re.search(r'-c\s+(\S+)', exec_out)
                conf_path = m.group(1) if m else f'/etc/odoo/{svc_name}.conf'
                if proj:
                    conf_content = ssh_utils.execute_command_shell(
                        proj, f'cat {conf_path} 2>/dev/null', timeout=10,
                    ).stdout
                else:
                    try:
                        with open(conf_path, 'r') as _f:
                            conf_content = _f.read()
                    except Exception:
                        conf_content = ''
                for line in conf_content.split('\n'):
                    line = line.strip()
                    if line.startswith('logfile') and '=' in line:
                        _, _, val = line.partition('=')
                        logfile = val.strip()
                        break
            if not logfile:
                logfile = f'/var/log/odoo/{svc_name}.log' if svc_name else ''
            if not logfile:
                return {'error': 'No se encontró archivo de log'}
            cmd = ['tail', '-f', '-n', '200', logfile]
        else:
            return {'error': f'Unknown session type: {session_type}'}

        # Write bridge script
        bridge_path = _write_bridge_script()

        # Set up environment for the bridge
        env = dict(os.environ)
        env['SESSION_DIR'] = session_dir

        # Set HOME so claude can find its credentials in ~/.claude/
        import pwd
        try:
            pw = pwd.getpwuid(os.getuid())
            env['HOME'] = pw.pw_dir
            # Include ~/.local/bin for native claude installer
            local_bin = os.path.join(pw.pw_dir, '.local', 'bin')
            if os.path.isdir(local_bin):
                env['PATH'] = local_bin + ':' + env.get('PATH', '')
        except KeyError:
            env['HOME'] = '/opt/odooAL'

        # For claude sessions, set API key and disable auto-update
        if session_type == 'claude':
            env['CLAUDE_CODE_DISABLE_AUTOUPDATE'] = '1'
            api_key = request.env['ir.config_parameter'].sudo().get_param(
                'pmb_devops.claude_api_key', ''
            )
            if api_key:
                env['ANTHROPIC_API_KEY'] = api_key

        try:
            # Launch bridge process
            process = subprocess.Popen(
                ['python3', bridge_path] + cmd,
                env=env,
                cwd=cwd,
                start_new_session=True,
            )

            # Store bridge PID
            bridge_pid_file = os.path.join(session_dir, 'bridge_pid')
            with open(bridge_pid_file, 'w') as f:
                f.write(str(process.pid))

            # Note: output is read via polling from /devops/terminal/read
            # (bus.bus watcher threads don't work with gevent workers)

            _logger.info(
                "Terminal session started: type=%s, uid=%s, bridge_pid=%s",
                session_type, uid, process.pid,
            )

            return {
                'status': 'started',
                'session_type': session_type,
                'message': f'Welcome to PatchMyByte DevOps — {session_type} terminal',
            }

        except Exception as e:
            _logger.exception("Failed to start terminal session")
            return {'error': str(e)}

    @http.route('/devops/terminal/read', type='json', auth='user')
    def terminal_read(self, session_type='shell', pos=0, instance_id=None):
        """Read output from a terminal session."""
        uid = request.env.uid
        session_dir = _get_session_dir(uid, session_type, instance_id)
        output_file = os.path.join(session_dir, 'output')
        alive_file = os.path.join(session_dir, 'alive')

        if not os.path.exists(output_file):
            return {'output': '', 'pos': 0, 'alive': False}

        try:
            with open(output_file, 'rb') as f:
                f.seek(pos)
                data = f.read()
                new_pos = pos + len(data)

            alive = True
            if os.path.exists(alive_file):
                with open(alive_file, 'r') as f:
                    alive = f.read().strip() == '1'

            # Update instance activity on read
            if instance_id:
                try:
                    instance = request.env['devops.instance'].browse(instance_id)
                    if instance.exists():
                        instance._update_activity()
                except Exception:
                    pass

            return {
                'output': data.decode('utf-8', errors='replace'),
                'pos': new_pos,
                'alive': alive,
            }
        except Exception as e:
            _logger.exception("Failed to read terminal output")
            return {'output': '', 'pos': pos, 'alive': False, 'error': str(e)}

    @http.route('/devops/terminal/write', type='json', auth='user')
    def terminal_write(self, session_type='shell', data='', instance_id=None):
        """Send input to a terminal session."""
        uid = request.env.uid
        session_dir = _get_session_dir(uid, session_type, instance_id)
        input_file = os.path.join(session_dir, 'input')

        if not os.path.exists(input_file):
            return {'error': 'Session not found'}

        try:
            with open(input_file, 'ab') as f:
                f.write(data.encode('utf-8'))

            # Update instance activity on write
            if instance_id:
                try:
                    instance = request.env['devops.instance'].browse(instance_id)
                    if instance.exists():
                        instance._update_activity()
                except Exception:
                    pass

            return {'status': 'ok'}
        except Exception as e:
            _logger.exception("Failed to write terminal input")
            return {'error': str(e)}

    @http.route('/devops/terminal/resize', type='json', auth='user')
    def terminal_resize(self, session_type='shell', rows=40, cols=120, instance_id=None):
        """Resize a terminal session."""
        uid = request.env.uid
        session_dir = _get_session_dir(uid, session_type, instance_id)
        pid_file = os.path.join(session_dir, 'pid')

        if not os.path.exists(pid_file):
            return {'error': 'Session not found'}

        try:
            with open(pid_file, 'r') as f:
                child_pid = int(f.read().strip())

            # Send SIGWINCH to the child process after updating the window size
            # We need to find the PTY master fd — but since we don't have it,
            # we store resize info for the bridge to pick up
            resize_file = os.path.join(session_dir, 'resize')
            with open(resize_file, 'w') as f:
                json.dump({'rows': rows, 'cols': cols}, f)

            return {'status': 'ok'}
        except Exception as e:
            _logger.exception("Failed to resize terminal")
            return {'error': str(e)}

    @http.route('/devops/terminal/stop', type='json', auth='user')
    def terminal_stop(self, session_type='shell', instance_id=None):
        """Stop a terminal session."""
        uid = request.env.uid
        self._stop_session(uid, session_type, instance_id)
        return {'status': 'stopped', 'session_type': session_type}

    def _start_output_watcher(self, uid, session_type, session_dir):
        """Start a background thread that watches the output file and publishes to bus.bus."""
        import threading

        output_file = os.path.join(session_dir, 'output')
        alive_file = os.path.join(session_dir, 'alive')
        channel = f'terminal_{uid}_{session_type}'
        dbname = request.env.cr.dbname

        def watcher():
            import odoo
            pos = 0
            while True:
                time.sleep(0.15)  # 150ms check interval
                try:
                    if not os.path.exists(alive_file):
                        break
                    with open(alive_file, 'r') as f:
                        if f.read().strip() != '1':
                            # Send final notification
                            with odoo.registry(dbname).cursor() as cr:
                                env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
                                env['bus.bus']._sendone(
                                    channel, 'terminal_output',
                                    {'output': '', 'alive': False},
                                )
                            break

                    if not os.path.exists(output_file):
                        continue

                    with open(output_file, 'rb') as f:
                        f.seek(pos)
                        data = f.read()
                        if data:
                            pos += len(data)
                            text = data.decode('utf-8', errors='replace')
                            with odoo.registry(dbname).cursor() as cr:
                                env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
                                env['bus.bus']._sendone(
                                    channel, 'terminal_output',
                                    {'output': text, 'alive': True},
                                )
                except Exception:
                    break

        t = threading.Thread(target=watcher, daemon=True)
        t.start()

    def _stop_session(self, uid, session_type, instance_id=None):
        """Kill bridge process and clean up session directory."""
        session_dir = _get_session_dir(uid, session_type, instance_id)

        if not os.path.exists(session_dir):
            return

        # Kill the bridge process
        bridge_pid_file = os.path.join(session_dir, 'bridge_pid')
        if os.path.exists(bridge_pid_file):
            try:
                with open(bridge_pid_file, 'r') as f:
                    bridge_pid = int(f.read().strip())
                os.kill(bridge_pid, signal.SIGTERM)
                _logger.info(
                    "Killed bridge process %s for session %s (uid=%s)",
                    bridge_pid, session_type, uid,
                )
            except (ProcessLookupError, ValueError):
                pass
            except Exception:
                _logger.exception("Error killing bridge process")

        # Kill the child process
        pid_file = os.path.join(session_dir, 'pid')
        if os.path.exists(pid_file):
            try:
                with open(pid_file, 'r') as f:
                    child_pid = int(f.read().strip())
                os.kill(child_pid, signal.SIGTERM)
                _logger.info(
                    "Killed child process %s for session %s (uid=%s)",
                    child_pid, session_type, uid,
                )
            except (ProcessLookupError, ValueError):
                pass
            except Exception:
                _logger.exception("Error killing child process")

        # Clean up session directory
        try:
            import shutil
            shutil.rmtree(session_dir, ignore_errors=True)
        except Exception:
            _logger.exception("Error cleaning up session directory")
