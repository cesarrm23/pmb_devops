#!/usr/bin/env python3
"""WebSocket Terminal Bridge for PMB DevOps.

Persistent sessions: PTY processes survive browser disconnects.
When a user reconnects, they reattach to their existing session.

Session key: uid + cmd_type + cwd (one Claude per instance per user).
"""
import asyncio
import fcntl
import json
import logging
import os
import pty
import signal
import struct
import termios
import time

import websockets

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('ws_terminal')

HOST = '127.0.0.1'
PORT = 8077
TOKEN_DIR = '/tmp/pmb_ws_tokens'
CLAUDE_BIN = '/opt/odooAL/.local/bin/claude'

# Persistent sessions: session_key -> Session
persistent_sessions = {}


def session_key(uid, cmd_type, cwd):
    return f"{uid}:{cmd_type}:{cwd}"


def _write_isolation_rules(cwd, instance_type, allowed_path):
    """Write a CLAUDE.md with environment isolation rules."""
    claude_md = os.path.join(cwd, 'CLAUDE.md')
    marker = '# PMB DevOps Environment Rules'

    if instance_type == 'production':
        rules = f"""{marker}
This is a PRODUCTION environment. Be extra careful with changes.
- Working directory: {cwd}
- You may read and modify files within this directory.
- NEVER run destructive commands (rm -rf, DROP DATABASE, etc).
- Always ask before making significant changes.

# PMB DevOps Platform Context
This server runs PMB DevOps (PatchMyByte), an Odoo 19 module that manages multiple Odoo instances.
The module is at: /opt/odooAL/custom_addons/pmb_devops/

## Architecture
- Production Odoo: runs as systemd service, config in /etc/odoo/
- Staging/Dev instances: created in /opt/instances/, each with own service, DB, nginx vhost, SSL
- SSH remote projects: instances can be created on remote servers via SSH
- WebSocket terminals: ws_terminal.py on port 8077 provides persistent PTY sessions
- Git workflow: production=main, staging branches, dev branches. Pre-push hooks block push to protected branches.

## Instance Creation Pipeline (staging/dev)
When creating a new instance, the system:
1. Clones the production database (pg_dump | psql)
2. Grants DB privileges to the instance user
3. Runs post-clone SQL: updates web.base.url, report.url, deactivates mail servers and crons
4. Replicates the production directory structure (symlinks for odoo/enterprise, clones git repos)
5. Creates its own git branch (named after the instance)
6. Creates Odoo config, systemd service, nginx vhost, SSL certificate
7. Installs pre-push hooks to prevent pushing to main/master

## Post-Clone Parametrization
After cloning a DB for staging/dev, these SQL updates are applied:
```sql
UPDATE ir_config_parameter SET value = 'https://{{domain}}' WHERE key = 'web.base.url';
UPDATE ir_config_parameter SET value = 'https://{{domain}}' WHERE key = 'report.url';
DELETE FROM ir_config_parameter WHERE key = 'database.uuid';
DELETE FROM ir_config_parameter WHERE key = 'database.enterprise_code';
UPDATE ir_mail_server SET active = false;
UPDATE fetchmail_server SET active = false;
UPDATE ir_cron SET active = false WHERE name NOT ILIKE '%session%' AND name NOT ILIKE '%autovacuum%';
```
Projects can add custom SQL in Settings > Script Post-Clonacion.

## Key Paths
- Module code: /opt/odooAL/custom_addons/pmb_devops/
- Controllers: controllers/devops_controller.py, controllers/ai_chat_controller.py
- Models: models/devops_instance.py, models/devops_instance_infra.py, models/devops_project.py
- Frontend: static/src/pmb_app/pmb_app.js, pmb_app.xml
- Utilities: utils/ssh_utils.py, utils/infra_utils.py, utils/git_utils.py, utils/ws_terminal.py
- Nginx configs: /etc/nginx/sites-enabled/
- Instance logs: /var/log/odoo/

## Common Tasks
- Create instance: done from the UI, calls action_create_instance() which runs _launch_creation_script()
- Deploy/upgrade: git pull + detect modules + odoo-bin -u modules --stop-after-init + restart service
- Fix permissions: psql -d {{db}} -c "GRANT ALL ON ALL TABLES IN SCHEMA public TO {{user}}"
- Check service: systemctl status {{service_name}}
- View logs: journalctl -u {{service_name}} -f
- Post-clone fix: psql -d {{db}} -c "UPDATE ir_config_parameter SET value='https://{{domain}}' WHERE key='web.base.url'"
"""
    elif instance_type == 'staging':
        rules = f"""{marker}
This is a STAGING environment for testing before production.
- Working directory: {cwd}
- You may ONLY modify files inside: {allowed_path}
- NEVER modify files in /opt/odooAL/ (production) or other instance paths.
- NEVER run: git push origin main, git push origin master
- NEVER push to production branches. Only push to your staging branch.
- NEVER run destructive commands (rm -rf, DROP DATABASE, etc).

## Post-Clone: if the DB needs fixing after clone, run:
psql -d <db_name> -c "UPDATE ir_config_parameter SET value='https://<domain>' WHERE key='web.base.url';"
## Deploy changes: git pull, then restart the service.
## This instance has a pre-push hook that blocks push to main/master.
"""
    else:  # development
        rules = f"""{marker}
This is a DEVELOPMENT environment for active coding.
- Working directory: {cwd}
- You may ONLY modify files inside: {allowed_path}
- NEVER modify files in /opt/odooAL/ (production).
- NEVER run: git push origin main, git push origin master, git push origin staging
- NEVER push to production or staging branches. Only push to your development branch.
- NEVER run destructive commands (rm -rf, DROP DATABASE, etc).

## Post-Clone: if the DB needs fixing after clone, run:
psql -d <db_name> -c "UPDATE ir_config_parameter SET value='https://<domain>' WHERE key='web.base.url';"
## Workflow: develop here, commit, push to your branch, then merge to staging from the UI.
## This instance has a pre-push hook that blocks push to main/master/staging.
"""

    try:
        if os.path.exists(claude_md):
            with open(claude_md, 'r') as f:
                existing = f.read()
            if marker not in existing:
                return
        with open(claude_md, 'w') as f:
            f.write(rules)
    except Exception as e:
        logger.warning("Could not write CLAUDE.md: %s", e)


# Git pre-push hook content that blocks pushing to protected branches
_PRE_PUSH_HOOK_STAGING = """#!/bin/bash
# PMB DevOps: Block push to protected branches from staging
while read local_ref local_sha remote_ref remote_sha; do
    branch=$(echo "$remote_ref" | sed 's|refs/heads/||')
    if [ "$branch" = "main" ] || [ "$branch" = "master" ]; then
        echo "ERROR: Push a '$branch' bloqueado desde staging."
        echo "Solo puedes hacer push a tu rama de staging."
        exit 1
    fi
done
exit 0
"""

_PRE_PUSH_HOOK_DEV = """#!/bin/bash
# PMB DevOps: Block push to protected branches from development
while read local_ref local_sha remote_ref remote_sha; do
    branch=$(echo "$remote_ref" | sed 's|refs/heads/||')
    if [ "$branch" = "main" ] || [ "$branch" = "master" ] || [ "$branch" = "staging" ]; then
        echo "ERROR: Push a '$branch' bloqueado desde development."
        echo "Solo puedes hacer push a tu rama de desarrollo."
        exit 1
    fi
done
exit 0
"""


def validate_token(token):
    if not token:
        return None
    token_path = os.path.join(TOKEN_DIR, token)
    if not os.path.exists(token_path):
        return None
    try:
        with open(token_path, 'r') as f:
            data = json.load(f)
        if time.time() - data.get('created', 0) > 30:
            os.remove(token_path)
            return None
        os.remove(token_path)
        return data
    except Exception:
        return None


def is_pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def spawn_pty(cmd, cwd, env):
    master, slave = pty.openpty()
    try:
        winsize = struct.pack('HHHH', 40, 120, 0, 0)
        fcntl.ioctl(master, termios.TIOCSWINSZ, winsize)
    except Exception:
        pass

    pid = os.fork()
    if pid == 0:
        os.close(master)
        os.setsid()
        os.dup2(slave, 0)
        os.dup2(slave, 1)
        os.dup2(slave, 2)
        if slave > 2:
            os.close(slave)
        if cwd:
            os.chdir(cwd)
        os.execvpe(cmd[0], cmd, env)

    os.close(slave)
    return master, pid


def _blocking_read(fd):
    """Blocking read from PTY. Returns bytes or None on EOF/error."""
    import select
    while True:
        ready, _, _ = select.select([fd], [], [], 0.5)
        if ready:
            try:
                data = os.read(fd, 16384)
                return data if data else None
            except OSError:
                return None


class Session:
    """A persistent PTY session that survives WebSocket disconnects."""

    SCROLLBACK_SIZE = 64 * 1024  # 64KB scrollback buffer

    def __init__(self, key, master_fd, pid, cwd, cmd_type, ssh_config=None):
        self.key = key
        self.master_fd = master_fd
        self.pid = pid
        self.cwd = cwd
        self.cmd_type = cmd_type
        self.ssh_config = ssh_config
        self.created = time.time()
        self.websocket = None
        self._read_task = None
        self._scrollback = bytearray()

    def start_reader(self):
        """Start the background PTY reader. Runs forever until process dies."""
        if self._read_task and not self._read_task.done():
            return  # already running
        self._read_task = asyncio.ensure_future(self._read_loop())

    async def _read_loop(self):
        """Read PTY output and forward to the current WebSocket (if any)."""
        loop = asyncio.get_event_loop()
        while is_pid_alive(self.pid):
            try:
                data = await loop.run_in_executor(None, _blocking_read, self.master_fd)
                if data is None:
                    break
                # Save to scrollback buffer (ring buffer)
                self._scrollback.extend(data)
                if len(self._scrollback) > self.SCROLLBACK_SIZE:
                    self._scrollback = self._scrollback[-self.SCROLLBACK_SIZE:]
                ws = self.websocket
                if ws:
                    try:
                        await ws.send(data)
                    except Exception:
                        self.websocket = None
            except asyncio.CancelledError:
                return
            except Exception:
                break
        logger.info("Reader ended for session %s (pid %s)", self.key, self.pid)

    async def attach(self, websocket):
        """Attach a new WebSocket to this session. Send scrollback history."""
        self.websocket = websocket
        # Verify PTY is still responsive by checking if master_fd is writable
        try:
            import select
            _, writable, _ = select.select([], [self.master_fd], [], 0.5)
            if not writable:
                logger.warning("Session %s master_fd not writable, marking dead", self.key)
                self._force_dead = True
                return
        except Exception:
            self._force_dead = True
            return
        # Send buffered scrollback so user sees prior conversation
        if self._scrollback:
            try:
                await websocket.send(bytes(self._scrollback))
            except Exception:
                pass
        self.start_reader()

    def detach(self):
        """Detach WebSocket (browser closed). PTY keeps running."""
        self.websocket = None

    def destroy(self):
        """Kill the PTY process and clean up."""
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
        try:
            os.kill(self.pid, signal.SIGTERM)
        except Exception:
            pass
        try:
            os.close(self.master_fd)
        except Exception:
            pass

    @property
    def alive(self):
        if getattr(self, '_force_dead', False):
            return False
        if not is_pid_alive(self.pid):
            return False
        # Check if reader is still running
        if self._read_task and self._read_task.done():
            return False
        # Check if PTY fd is still valid
        try:
            import select
            _, ready, _ = select.select([], [self.master_fd], [], 0)
            return True
        except (OSError, ValueError):
            return False


async def terminal_handler(websocket):
    """Handle a WebSocket connection."""
    session = None

    try:
        raw = await asyncio.wait_for(websocket.recv(), timeout=10)
        msg = json.loads(raw)
        token_data = validate_token(msg.get('token', ''))

        if not token_data:
            await websocket.send(json.dumps({'type': 'error', 'data': 'Token invalido o expirado'}))
            return

        uid = token_data.get('uid', 0)
        cmd_type = token_data.get('cmd', 'shell')
        cwd = token_data.get('cwd', '/opt/odooAL')
        key = session_key(uid, cmd_type, cwd)

        # Check for existing session
        force_new = token_data.get('force_new', False)
        session = persistent_sessions.get(key)

        # Force new: destroy existing session
        if force_new and session:
            logger.info("Force new session: destroying old key=%s, pid=%s", key, session.pid)
            session.destroy()
            del persistent_sessions[key]
            session = None

        if session and session.alive:
            # Reattach
            await session.attach(websocket)
            logger.info("Reattach: key=%s, pid=%s", key, session.pid)
            await websocket.send(json.dumps({
                'type': 'ready', 'pid': session.pid, 'reattached': True,
            }))
        else:
            # Clean up dead session if any
            if session:
                session.destroy()
                del persistent_sessions[key]

            # New session
            # Validate cwd exists before spawning
            ssh = token_data.get('ssh')
            if not ssh and not os.path.isdir(cwd):
                await websocket.send(json.dumps({
                    'type': 'error',
                    'data': f'Directorio no existe: {cwd}',
                }))
                logger.warning("Blocked session: cwd %s does not exist", cwd)
                return

            instance_user = token_data.get('instance_user', '')

            if ssh:
                # Build SSH command to remote server
                ssh_cmd = ['ssh', '-t', '-o', 'StrictHostKeyChecking=no']
                if ssh.get('key') and os.path.isfile(ssh['key']):
                    ssh_cmd += ['-i', ssh['key']]
                if ssh.get('port', 22) != 22:
                    ssh_cmd += ['-p', str(ssh['port'])]
                ssh_cmd += [f"{ssh.get('user', 'root')}@{ssh['host']}"]
                remote_cwd = ssh.get('remote_cwd', '/opt')
                ssh_instance_user = ssh.get('instance_user', '')
                ssh_user = ssh.get('user', 'root')
                # Validate remote directory exists before opening shell
                dir_check = f'test -d {remote_cwd} && echo OK || echo FAIL'
                if cmd_type == 'claude':
                    # Auto-install claude if missing on remote (requires node/npm)
                    ensure_claude = (
                        'if ! which claude >/dev/null 2>&1; then '
                        'if which npm >/dev/null 2>&1; then npm install -g @anthropic-ai/claude-code 2>&1 | tail -1; '
                        'else echo "ERROR: npm no instalado. Instala Node.js primero: apt install nodejs npm"; exit 1; fi; fi;'
                    )
                    claude_cmd = f'cd {remote_cwd} && CLAUDE_CODE_DISABLE_AUTOUPDATE=1 claude'
                    if ssh_instance_user:
                        ssh_cmd += [f'{ensure_claude} sudo -u {ssh_instance_user} -i bash -c \'{claude_cmd}\'']
                    elif ssh_user == 'root':
                        ssh_cmd += [f'{ensure_claude} OWNER=$(stat -c %U {remote_cwd} 2>/dev/null || echo nobody); sudo -u $OWNER -i bash -c \'{claude_cmd}\'']
                    else:
                        ssh_cmd += [f'{ensure_claude} {claude_cmd}']
                else:
                    if ssh_instance_user:
                        ssh_cmd += [f'test -d {remote_cwd} || exit 1; sudo -u {ssh_instance_user} -i bash -c "cd {remote_cwd} && exec bash -i"']
                    elif ssh_user == 'root':
                        ssh_cmd += [f'test -d {remote_cwd} || exit 1; OWNER=$(stat -c %U {remote_cwd} 2>/dev/null || echo nobody); sudo -u $OWNER -i bash -c "cd {remote_cwd} && exec bash -i"']
                    else:
                        ssh_cmd += [f'test -d {remote_cwd} || exit 1; cd {remote_cwd} && bash -i']
                cmd = ssh_cmd
            elif cmd_type == 'claude':
                if instance_user:
                    cmd = ['sudo', '-u', instance_user, '-i', 'bash', '-c',
                           f'cd {cwd} && CLAUDE_CODE_DISABLE_AUTOUPDATE=1 PATH=/opt/odooAL/.local/bin:$PATH claude']
                else:
                    cmd = [CLAUDE_BIN]
            elif cmd_type == 'shell':
                if instance_user:
                    cmd = ['sudo', '-u', instance_user, '-i', 'bash', '-c', f'cd {cwd} && exec bash -i']
                else:
                    cmd = ['/bin/bash', '-i']
            else:
                await websocket.send(json.dumps({'type': 'error', 'data': f'Comando desconocido: {cmd_type}'}))
                return

            env = os.environ.copy()
            env['HOME'] = '/opt/odooAL'
            env['PATH'] = '/opt/odooAL/.local/bin:' + env.get('PATH', '/usr/bin:/bin')
            env['TERM'] = 'xterm-256color'
            env['CLAUDE_CODE_DISABLE_AUTOUPDATE'] = '1'
            env.pop('ANTHROPIC_API_KEY', None)

            # Security: create CLAUDE.md with isolation rules per instance type
            instance_type = token_data.get('instance_type', 'production')
            allowed_path = token_data.get('allowed_path', cwd)
            if not ssh:
                _write_isolation_rules(cwd, instance_type, allowed_path)

            master_fd, child_pid = spawn_pty(cmd, cwd, env)

            # Set non-blocking
            flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

            session = Session(key, master_fd, child_pid, cwd, cmd_type, ssh_config=ssh)
            persistent_sessions[key] = session
            await session.attach(websocket)

            logger.info("New session: key=%s, pid=%s, cwd=%s", key, child_pid, cwd)
            await websocket.send(json.dumps({'type': 'ready', 'pid': child_pid}))

        # Send resize
        master_fd = session.master_fd
        child_pid = session.pid

        # Write loop: WS -> PTY (runs until WS disconnects)
        async for raw in websocket:
            try:
                msg = json.loads(raw)
                if msg.get('type') == 'input':
                    os.write(master_fd, msg['data'].encode('utf-8'))
                elif msg.get('type') == 'resize':
                    rows = msg.get('rows', 40)
                    cols = msg.get('cols', 120)
                    winsize = struct.pack('HHHH', rows, cols, 0, 0)
                    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
                    os.kill(child_pid, signal.SIGWINCH)
                elif msg.get('type') in ('image', 'file'):
                    # Save file and make it accessible to the PTY process
                    import base64
                    file_data = base64.b64decode(msg['data'])
                    filename = msg.get('filename', f'paste_{int(time.time())}.bin')
                    filename = filename.replace('/', '_').replace('..', '_')
                    size_kb = len(file_data) / 1024

                    # Determine where to save
                    save_dir = session.cwd if session else '/tmp'
                    file_path = os.path.join(save_dir, filename)

                    # Check if this is an SSH session
                    ssh_cfg = session.ssh_config if session else None
                    if ssh_cfg:
                        # SSH: save locally first, then SCP to remote
                        local_tmp = os.path.join('/tmp', filename)
                        with open(local_tmp, 'wb') as f:
                            f.write(file_data)
                        remote_path = f"/tmp/{filename}"
                        scp_cmd = ['scp', '-o', 'StrictHostKeyChecking=no']
                        if ssh_cfg.get('key') and os.path.isfile(ssh_cfg['key']):
                            scp_cmd += ['-i', ssh_cfg['key']]
                        if ssh_cfg.get('port', 22) != 22:
                            scp_cmd += ['-P', str(ssh_cfg['port'])]
                        scp_cmd += [local_tmp, f"{ssh_cfg.get('user', 'root')}@{ssh_cfg['host']}:{remote_path}"]
                        try:
                            import subprocess
                            subprocess.run(scp_cmd, capture_output=True, timeout=30)
                            os.remove(local_tmp)
                        except Exception as e:
                            logger.warning("SCP failed: %s", e)
                        file_path = remote_path
                    else:
                        # Local: save directly
                        with open(file_path, 'wb') as f:
                            f.write(file_data)

                    # Send path as input to the terminal
                    os.write(master_fd, file_path.encode('utf-8'))
                    logger.info("File saved: %s (%.1f KB, ssh=%s)", file_path, size_kb, bool(ssh_cfg))
                    try:
                        await websocket.send(json.dumps({
                            'type': 'info',
                            'data': f'Archivo: {file_path} ({size_kb:.1f} KB)',
                        }))
                    except Exception:
                        pass
            except (OSError, json.JSONDecodeError):
                pass

    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception:
        logger.exception("Session error")
    finally:
        # WS disconnected — detach but DON'T kill
        if session:
            session.detach()
            logger.info("WS disconnected, session persists: key=%s, pid=%s", session.key, session.pid)


async def cleanup_dead_sessions():
    """Periodically clean up sessions where the process died."""
    while True:
        await asyncio.sleep(30)
        dead = [k for k, s in persistent_sessions.items() if not s.alive]
        for k in dead:
            s = persistent_sessions.pop(k, None)
            if s:
                s.destroy()
                logger.info("Cleaned dead session: %s", k)


async def main():
    logger.info("WebSocket Terminal Bridge (persistent) starting on %s:%s", HOST, PORT)
    os.makedirs(TOKEN_DIR, exist_ok=True)

    asyncio.ensure_future(cleanup_dead_sessions())

    async with websockets.serve(
        terminal_handler, HOST, PORT,
        max_size=2**20,
        ping_interval=20,
        ping_timeout=20,
    ):
        logger.info("Ready. Sessions persist across disconnects.")
        await asyncio.Future()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down...")
