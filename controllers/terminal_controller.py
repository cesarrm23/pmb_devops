import json
import logging
import os
import signal
import subprocess

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


def _get_session_dir(uid, session_type):
    """Return the session directory path for a given user and session type."""
    session_dir = os.path.join(TERMINAL_DIR, f'user_{uid}', session_type)
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
        session_dir = _get_session_dir(uid, session_type)

        # Stop any existing session of this type
        self._stop_session(uid, session_type)

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
                    # Production: try repo_path, fallback to home
                    repo = instance.project_id.repo_path
                    if repo and os.path.isdir(repo) and os.access(repo, os.R_OK):
                        cwd = repo
                elif instance.instance_path and os.path.isdir(instance.instance_path):
                    cwd = instance.instance_path
                elif instance.project_id.repo_path and os.path.isdir(instance.project_id.repo_path):
                    cwd = instance.project_id.repo_path

                # For logs, get service name from instance
                if session_type == 'logs' and instance.service_name:
                    service = instance.service_name

                # Update activity
                instance._update_activity()

        # Determine command based on session type
        if session_type == 'claude':
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
        else:
            return {'error': f'Unknown session type: {session_type}'}

        # Write bridge script
        bridge_path = _write_bridge_script()

        # Set up environment for the bridge
        env = dict(os.environ)
        env['SESSION_DIR'] = session_dir

        # For claude sessions, set API key from config
        if session_type == 'claude':
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
        session_dir = _get_session_dir(uid, session_type)
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
        session_dir = _get_session_dir(uid, session_type)
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
    def terminal_resize(self, session_type='shell', rows=40, cols=120):
        """Resize a terminal session."""
        uid = request.env.uid
        session_dir = _get_session_dir(uid, session_type)
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
    def terminal_stop(self, session_type='shell'):
        """Stop a terminal session."""
        uid = request.env.uid
        self._stop_session(uid, session_type)
        return {'status': 'stopped', 'session_type': session_type}

    def _stop_session(self, uid, session_type):
        """Kill bridge process and clean up session directory."""
        session_dir = _get_session_dir(uid, session_type)

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
