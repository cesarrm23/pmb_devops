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
    # Don't overwrite if file already has custom content beyond our marker
    marker = '# PMB DevOps Environment Rules'

    if instance_type == 'production':
        rules = f"""{marker}
This is a PRODUCTION environment. Be extra careful with changes.
- Working directory: {cwd}
- You may read and modify files within this directory.
- NEVER run destructive commands (rm -rf, DROP DATABASE, etc).
- Always ask before making significant changes.
"""
    elif instance_type == 'staging':
        rules = f"""{marker}
This is a STAGING environment.
- Working directory: {cwd}
- You may ONLY modify files inside: {allowed_path}
- NEVER modify files in /opt/odooAL/ (production) or other instance paths.
- NEVER modify files in /opt/instances/ directories that are not this instance.
- This environment is for testing before production.
"""
    else:  # development
        rules = f"""{marker}
This is a DEVELOPMENT environment.
- Working directory: {cwd}
- You may ONLY modify files inside: {allowed_path}
- NEVER modify files in /opt/odooAL/ (production).
- NEVER modify files in other /opt/instances/ directories that are not this instance.
- NEVER switch git branches to main or staging.
- Changes here should be committed to the development branch only.
"""

    try:
        # Only write if file doesn't exist or was written by us
        if os.path.exists(claude_md):
            with open(claude_md, 'r') as f:
                existing = f.read()
            if marker not in existing:
                return  # User has custom CLAUDE.md, don't overwrite
        with open(claude_md, 'w') as f:
            f.write(rules)
    except Exception as e:
        logger.warning("Could not write CLAUDE.md: %s", e)


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

    def __init__(self, key, master_fd, pid, cwd, cmd_type):
        self.key = key
        self.master_fd = master_fd
        self.pid = pid
        self.cwd = cwd
        self.cmd_type = cmd_type
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
        return is_pid_alive(self.pid)


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
        session = persistent_sessions.get(key)
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
            # Check if this is an SSH project
            ssh = token_data.get('ssh')
            if ssh:
                # Build SSH command to remote server
                ssh_cmd = ['ssh', '-t', '-o', 'StrictHostKeyChecking=no']
                if ssh.get('key') and os.path.isfile(ssh['key']):
                    ssh_cmd += ['-i', ssh['key']]
                if ssh.get('port', 22) != 22:
                    ssh_cmd += ['-p', str(ssh['port'])]
                ssh_cmd += [f"{ssh.get('user', 'root')}@{ssh['host']}"]
                remote_cwd = ssh.get('remote_cwd', '/opt')
                instance_user = ssh.get('instance_user', '')
                if cmd_type == 'claude':
                    if instance_user:
                        ssh_cmd += [f'cd {remote_cwd} && sudo -u {instance_user} claude']
                    else:
                        ssh_cmd += [f'cd {remote_cwd} && claude']
                else:
                    if instance_user:
                        ssh_cmd += [f'cd {remote_cwd} && sudo -u {instance_user} -i bash']
                    else:
                        ssh_cmd += [f'cd {remote_cwd} && bash -i']
                cmd = ssh_cmd
            elif cmd_type == 'claude':
                cmd = [CLAUDE_BIN]
            elif cmd_type == 'shell':
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

            session = Session(key, master_fd, child_pid, cwd, cmd_type)
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
                elif msg.get('type') == 'image':
                    # Save pasted image to temp file and send the path to the PTY
                    import base64, tempfile
                    img_data = base64.b64decode(msg['data'])
                    filename = msg.get('filename', f'paste_{int(time.time())}.png')
                    img_path = os.path.join(tempfile.gettempdir(), filename)
                    with open(img_path, 'wb') as f:
                        f.write(img_data)
                    # Send the file path as input to Claude Code
                    os.write(master_fd, img_path.encode('utf-8'))
                    logger.info("Image saved: %s (%d bytes)", img_path, len(img_data))
                    try:
                        await websocket.send(json.dumps({
                            'type': 'info',
                            'data': f'Imagen guardada: {img_path}',
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
