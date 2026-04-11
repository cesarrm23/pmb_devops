"""API endpoints for the PMB DevOps SPA."""
import logging
import os

from odoo import fields, http
from odoo.http import request

_logger = logging.getLogger(__name__)


class DevopsController(http.Controller):

    @http.route('/devops/project/data', type='json', auth='user')
    def project_data(self, project_id):
        """Get all instances + branches for a project (sidebar data)."""
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}

        instances = request.env['devops.instance'].search_read(
            [('project_id', '=', project_id)],
            ['id', 'name', 'instance_type', 'state', 'creation_step',
             'full_domain', 'port', 'database_name', 'service_name', 'url',
             'branch_id', 'subdomain', 'last_activity'],
            order='instance_type, name',
        )
        branches = request.env['devops.branch'].search_read(
            [('project_id', '=', project_id)],
            ['id', 'name', 'branch_type', 'is_current', 'last_commit_hash',
             'last_commit_message', 'last_commit_author', 'instance_id',
             'commit_history'],
            order='branch_type, name',
        )
        return {
            'project': {
                'id': project.id,
                'name': project.name,
                'domain': project.domain,
                'repo_path': project.repo_path,
                'repo_url': project.repo_url,
                'max_staging': project.max_staging,
                'max_development': project.max_development,
                'production_branch': project.production_branch or 'main',
            },
            'instances': instances,
            'branches': branches,
        }

    @http.route('/devops/instance/create', type='json', auth='user')
    def instance_create(self, project_id, name, instance_type, branch_from='main', clone_from_id=False):
        """Create a new staging/development instance."""
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}

        # Check if instance with same name already exists
        existing = request.env['devops.instance'].search([
            ('project_id', '=', project_id),
            ('name', '=', name),
        ], limit=1)
        if existing:
            return {'error': f'Ya existe una instancia "{name}" en este proyecto.'}

        # Find or create branch record
        branch = request.env['devops.branch'].search([
            ('project_id', '=', project_id),
            ('name', '=', name),
        ], limit=1)
        if not branch:
            branch = request.env['devops.branch'].create({
                'project_id': project_id,
                'name': name,
                'branch_type': instance_type,
            })

        # Determine clone source
        clone_from = False
        if clone_from_id:
            clone_from = request.env['devops.instance'].browse(clone_from_id)
        elif instance_type == 'staging' and project.production_instance_id:
            clone_from = project.production_instance_id
        elif instance_type == 'development':
            # Clone from first staging, or production
            staging = request.env['devops.instance'].search([
                ('project_id', '=', project_id),
                ('instance_type', '=', 'staging'),
                ('state', '=', 'running'),
            ], limit=1)
            clone_from = staging or project.production_instance_id

        # Create instance
        instance = request.env['devops.instance'].create({
            'project_id': project_id,
            'branch_id': branch.id,
            'name': name,
            'instance_type': instance_type,
            'cloned_from_id': clone_from.id if clone_from else False,
        })

        # Run creation pipeline (non-blocking: spawns background thread)
        try:
            instance.action_create_instance()
            return {
                'status': 'creating',
                'instance_id': instance.id,
            }
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    @http.route('/devops/instance/poll_status', type='json', auth='user')
    def instance_poll_status(self, instance_id, log_pos=0):
        """Poll instance creation status + live creation log."""
        instance = request.env['devops.instance'].browse(instance_id)
        if not instance.exists():
            return {'error': 'Not found'}

        # Read creation log tail
        log_lines = ''
        new_log_pos = log_pos
        if instance.state in ('creating', 'error'):
            log_file = '/var/log/odoo/pmb_creation.log'
            try:
                with open(log_file, 'rb') as f:
                    f.seek(0, 2)  # end
                    file_size = f.tell()
                    if log_pos == 0:
                        # First call: find the start of this instance's section
                        marker = f'id={instance.id})'.encode()
                        # Read last 50KB to find marker
                        read_from = max(0, file_size - 50000)
                        f.seek(read_from)
                        chunk = f.read()
                        idx = chunk.rfind(marker)
                        if idx >= 0:
                            # Find start of line
                            line_start = chunk.rfind(b'\n', 0, idx)
                            new_log_pos = read_from + (line_start + 1 if line_start >= 0 else idx)
                            f.seek(new_log_pos)
                            log_lines = f.read().decode('utf-8', errors='replace')
                            new_log_pos = file_size
                        else:
                            new_log_pos = file_size
                    elif log_pos < file_size:
                        f.seek(log_pos)
                        log_lines = f.read().decode('utf-8', errors='replace')
                        new_log_pos = file_size
                    else:
                        new_log_pos = log_pos
            except Exception:
                pass

        return {
            'state': instance.state,
            'creation_step': instance.creation_step or '',
            'creation_pid': instance.creation_pid or 0,
            'log': log_lines,
            'log_pos': new_log_pos,
        }

    @http.route('/devops/instance/detect_service', type='json', auth='user')
    def instance_detect_service(self, service_name):
        """Auto-detect Odoo config from a systemd service name."""
        import subprocess
        import re

        result = {'service_name': service_name}

        try:
            # Read ExecStart from systemd unit to find config path
            proc = subprocess.run(
                ['systemctl', 'show', f'{service_name}.service', '--property=ExecStart'],
                capture_output=True, text=True, timeout=5,
            )
            exec_line = proc.stdout.strip()
            # Extract -c /path/to/config
            m = re.search(r'-c\s+(\S+)', exec_line)
            config_path = m.group(1) if m else f'/etc/odoo/{service_name}.conf'

            # Also extract instance path from ExecStart (WorkingDirectory or odoo-bin path)
            m2 = re.search(r'(\S+)/odoo/odoo-bin', exec_line)
            if m2:
                result['instance_path'] = m2.group(1)

            # Read Odoo config
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    config_content = f.read()

                for line in config_content.split('\n'):
                    line = line.strip()
                    if '=' not in line or line.startswith('#') or line.startswith('['):
                        continue
                    key, _, val = line.partition('=')
                    key = key.strip()
                    val = val.strip()
                    if key == 'http_port':
                        result['port'] = int(val)
                    elif key == 'gevent_port':
                        result['gevent_port'] = int(val)
                    elif key == 'db_name':
                        result['database_name'] = val
                    elif key == 'data_dir':
                        result['data_dir'] = val
                    elif key == 'addons_path':
                        result['addons_path'] = val
                        # Auto-detect repo_path and enterprise_path from addons_path
                        for addon_dir in val.split(','):
                            addon_dir = addon_dir.strip()
                            if not addon_dir:
                                continue
                            # Enterprise: contains 'enterprise' in path
                            if 'enterprise' in addon_dir.lower() and os.path.isdir(addon_dir):
                                result['enterprise_path'] = addon_dir
                            # Custom addons repo: has .git directory (not odoo core)
                            elif os.path.isdir(os.path.join(addon_dir, '.git')):
                                result['repo_path'] = addon_dir

                result['config_path'] = config_path

            # Check if service is active
            proc2 = subprocess.run(
                ['systemctl', 'is-active', f'{service_name}.service'],
                capture_output=True, text=True, timeout=5,
            )
            result['active'] = proc2.stdout.strip() == 'active'

        except Exception as e:
            result['error'] = str(e)

        return result

    @http.route('/devops/instance/register_production', type='json', auth='user')
    def instance_register_production(self, project_id, service_name, database_name, port=8069, instance_path=''):
        """Register an existing Odoo service as the production instance.

        Auto-detects repo_path, enterprise_path, git branch from the service config.
        """
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}

        existing = request.env['devops.instance'].search([
            ('project_id', '=', project_id),
            ('instance_type', '=', 'production'),
        ], limit=1)
        if existing:
            return {'error': 'Ya existe una instancia de produccion para este proyecto.'}

        # Auto-detect everything from the service config
        detected = self.instance_detect_service(service_name)
        if not instance_path:
            instance_path = detected.get('instance_path', f'/opt/{service_name}')
        if not database_name:
            database_name = detected.get('database_name', service_name)
        if not port:
            port = detected.get('port', 8069)
        gevent_port = detected.get('gevent_port', port + 1000)

        # Auto-detect git branch from repo
        git_branch = project.production_branch or 'main'
        repo_path = detected.get('repo_path', '')
        enterprise_path = detected.get('enterprise_path', '')

        if repo_path:
            import subprocess
            try:
                result = subprocess.run(
                    ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                    capture_output=True, text=True, timeout=5, cwd=repo_path,
                )
                if result.returncode == 0 and result.stdout.strip():
                    git_branch = result.stdout.strip()
            except Exception:
                pass

        instance = request.env['devops.instance'].create({
            'project_id': project_id,
            'name': 'production',
            'instance_type': 'production',
            'service_name': service_name,
            'database_name': database_name,
            'port': port,
            'gevent_port': gevent_port,
            'instance_path': instance_path,
            'subdomain': '',
            'git_branch': git_branch,
            'state': 'running' if detected.get('active') else 'stopped',
        })

        # Auto-update project with detected paths
        project_vals = {
            'production_instance_id': instance.id,
            'database_name': database_name,
            'odoo_service_name': service_name,
            'production_branch': git_branch,
        }
        if repo_path and not project.repo_path:
            project_vals['repo_path'] = repo_path
        if enterprise_path and not project.enterprise_path:
            project_vals['enterprise_path'] = enterprise_path
        project.write(project_vals)

        return {
            'status': 'ok',
            'instance_id': instance.id,
            'auto_detected': {
                'repo_path': repo_path,
                'enterprise_path': enterprise_path,
                'git_branch': git_branch,
            },
        }

    @http.route('/devops/instance/deploy', type='json', auth='user')
    def instance_deploy(self, instance_id, repo_path=''):
        """Launch async deploy: git pull + detect modules + update + restart.

        Writes progress to a log file, returns immediately.
        Frontend polls /devops/instance/deploy_status for progress.
        """
        import subprocess
        import shlex

        instance = request.env['devops.instance'].browse(instance_id)
        if not instance.exists():
            return {'error': 'Instancia no encontrada'}

        project = instance.project_id

        # Get repos
        repos_result = self.instance_repos(project.id, instance_id)
        repos = repos_result.get('repos', [])
        if repo_path:
            repos = [r for r in repos if r['path'] == repo_path]

        if not repos:
            return {'error': 'No hay repos para desplegar'}

        # Build deploy script
        deploy_id = f"deploy_{instance.id}_{int(fields.Datetime.now().timestamp())}"
        log_file = f"/tmp/pmb_{deploy_id}.log"
        status_file = f"/tmp/pmb_{deploy_id}.status"
        service = instance.service_name or ''
        db = instance.database_name or ''
        inst_path = instance.instance_path or ''
        config = instance.odoo_config_path or ''
        python_bin = f"{inst_path}/.venv/bin/python" if inst_path else 'python3'
        odoo_bin = f"{inst_path}/odoo/odoo-bin" if inst_path else 'odoo-bin'

        repo_cmds = []
        for r in repos:
            rp = shlex.quote(r['path'])
            rn = r['name']
            repo_cmds.append(f'''
echo "=== {rn} ({rp}) ===" >> {log_file}
cd {rp}
sudo git pull --ff-only >> {log_file} 2>&1
if [ $? -ne 0 ]; then
    echo "ERROR: git pull failed" >> {log_file}
else
    CHANGED=$(sudo git diff --name-only HEAD@{{1}} HEAD 2>/dev/null | wc -l)
    if [ "$CHANGED" -gt 0 ]; then
        echo "$CHANGED archivo(s) cambiado(s)" >> {log_file}
        sudo git diff --name-only HEAD@{{1}} HEAD 2>/dev/null >> {log_file}
        PULLED=1
        # Detect modules
        for f in $(sudo git diff --name-only HEAD@{{1}} HEAD 2>/dev/null); do
            MOD=$(echo "$f" | cut -d/ -f1)
            if [ -f "{rp}/$MOD/__manifest__.py" ]; then
                MODULES="$MODULES,$MOD"
            fi
        done
    else
        echo "Sin cambios" >> {log_file}
    fi
fi
''')

        script = f'''#!/bin/bash
echo "running" > {status_file}
PULLED=0
MODULES=""
{''.join(repo_cmds)}
# Remove leading comma
MODULES=$(echo "$MODULES" | sed 's/^,//' | tr ',' '\\n' | sort -u | tr '\\n' ',' | sed 's/,$//')

if [ -n "$MODULES" ]; then
    echo "" >> {log_file}
    echo "=== Actualizando modulos: $MODULES ===" >> {log_file}
    sudo systemctl stop {service} >> {log_file} 2>&1
    echo "Servicio detenido" >> {log_file}
    {python_bin} {odoo_bin} -d {db} -u "$MODULES" --stop-after-init {'-c ' + config if config else ''} >> {log_file} 2>&1
    if [ $? -eq 0 ]; then
        echo "Modulos actualizados: $MODULES" >> {log_file}
    else
        echo "ERROR actualizando modulos" >> {log_file}
    fi
    sudo systemctl start {service} >> {log_file} 2>&1
    echo "Servicio iniciado" >> {log_file}
elif [ "$PULLED" -gt 0 ]; then
    echo "" >> {log_file}
    echo "Sin cambios en modulos, reiniciando servicio..." >> {log_file}
    sudo systemctl restart {service} >> {log_file} 2>&1
    echo "Servicio reiniciado" >> {log_file}
else
    echo "" >> {log_file}
    echo "Todo al dia, sin cambios que desplegar." >> {log_file}
fi
echo "done" > {status_file}
'''

        script_path = f"/tmp/pmb_{deploy_id}.sh"
        with open(script_path, 'w') as f:
            f.write(script)
        os.chmod(script_path, 0o755)
        open(log_file, 'w').close()

        subprocess.Popen(
            ['/bin/bash', script_path],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        return {'status': 'started', 'deploy_id': deploy_id}

    @http.route('/devops/instance/deploy_status', type='json', auth='user')
    def instance_deploy_status(self, deploy_id, log_pos=0):
        """Poll deploy progress."""
        log_file = f"/tmp/pmb_{deploy_id}.log"
        status_file = f"/tmp/pmb_{deploy_id}.status"

        status = 'running'
        if os.path.exists(status_file):
            with open(status_file, 'r') as f:
                status = f.read().strip() or 'running'

        log = ''
        new_pos = log_pos
        if os.path.exists(log_file):
            with open(log_file, 'rb') as f:
                f.seek(log_pos)
                data = f.read()
                log = data.decode('utf-8', errors='replace')
                new_pos = log_pos + len(data)

        return {'status': status, 'log': log, 'log_pos': new_pos}

    @http.route('/devops/instance/destroy', type='json', auth='user')
    def instance_destroy(self, instance_id):
        """Destroy an instance."""
        instance = request.env['devops.instance'].browse(instance_id)
        if not instance.exists():
            return {'error': 'Instancia no encontrada'}
        try:
            instance.action_destroy()
            return {'status': 'ok'}
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    @http.route('/devops/instance/start', type='json', auth='user')
    def instance_start(self, instance_id):
        """Start an instance."""
        instance = request.env['devops.instance'].browse(instance_id)
        if not instance.exists():
            return {'error': 'Instancia no encontrada'}
        try:
            instance.action_start()
            return {'status': 'ok', 'state': instance.state}
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    @http.route('/devops/instance/stop', type='json', auth='user')
    def instance_stop(self, instance_id):
        """Stop an instance."""
        instance = request.env['devops.instance'].browse(instance_id)
        if not instance.exists():
            return {'error': 'Instancia no encontrada'}
        try:
            instance.action_stop()
            return {'status': 'ok', 'state': instance.state}
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    @http.route('/devops/instance/restart', type='json', auth='user')
    def instance_restart(self, instance_id):
        """Restart an instance."""
        instance = request.env['devops.instance'].browse(instance_id)
        if not instance.exists():
            return {'error': 'Instancia no encontrada'}
        try:
            instance.action_restart()
            return {'status': 'ok', 'state': instance.state}
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    @http.route('/devops/instance/repos', type='json', auth='user')
    def instance_repos(self, project_id, instance_id=None):
        """Detect available git repos for an instance from its Odoo config."""
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'repos': []}

        repos = []
        config_path = ''

        # Find config path from instance or project
        if instance_id:
            inst = request.env['devops.instance'].browse(instance_id)
            if inst.exists() and inst.service_name:
                import subprocess, re
                try:
                    proc = subprocess.run(
                        ['systemctl', 'show', f'{inst.service_name}.service', '--property=ExecStart'],
                        capture_output=True, text=True, timeout=5,
                    )
                    m = re.search(r'-c\s+(\S+)', proc.stdout)
                    if m:
                        config_path = m.group(1)
                except Exception:
                    pass

        if not config_path and project.odoo_service_name:
            import subprocess, re
            try:
                proc = subprocess.run(
                    ['systemctl', 'show', f'{project.odoo_service_name}.service', '--property=ExecStart'],
                    capture_output=True, text=True, timeout=5,
                )
                m = re.search(r'-c\s+(\S+)', proc.stdout)
                if m:
                    config_path = m.group(1)
            except Exception:
                pass

        # Parse addons_path from config
        addons_paths = []
        if config_path and os.path.exists(config_path):
            try:
                with open(config_path, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith('addons_path'):
                            _, _, val = line.partition('=')
                            addons_paths = [p.strip() for p in val.strip().split(',') if p.strip()]
                            break
            except Exception:
                pass

        # Check which paths have .git: direct, parent dirs, or child dirs
        import subprocess
        seen = set()

        def _add_repo(git_path):
            if git_path in seen:
                return
            seen.add(git_path)
            branch = 'HEAD'
            ahead = 0
            behind = 0
            remote = ''
            dirty = False
            try:
                r = subprocess.run(
                    ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                    capture_output=True, text=True, timeout=5, cwd=git_path,
                )
                if r.returncode == 0 and r.stdout.strip():
                    branch = r.stdout.strip()
            except Exception:
                pass
            # Ahead/behind remote
            try:
                r2 = subprocess.run(
                    ['git', 'rev-list', '--left-right', '--count', f'{branch}...@{{upstream}}'],
                    capture_output=True, text=True, timeout=5, cwd=git_path,
                )
                if r2.returncode == 0:
                    parts = r2.stdout.strip().split()
                    if len(parts) == 2:
                        ahead = int(parts[0])
                        behind = int(parts[1])
                r3 = subprocess.run(
                    ['git', 'rev-parse', '--abbrev-ref', f'{branch}@{{upstream}}'],
                    capture_output=True, text=True, timeout=5, cwd=git_path,
                )
                if r3.returncode == 0:
                    remote = r3.stdout.strip()
            except Exception:
                pass
            # Uncommitted changes
            try:
                r4 = subprocess.run(
                    ['git', 'status', '--porcelain'],
                    capture_output=True, text=True, timeout=5, cwd=git_path,
                )
                if r4.returncode == 0 and r4.stdout.strip():
                    dirty = True
            except Exception:
                pass
            shallow = os.path.exists(os.path.join(git_path, '.git', 'shallow'))
            # Pending merge: commits ahead of target branch
            merge_target = ''
            merge_pending = 0
            merge_pending_commits = []
            if branch == 'development':
                merge_target = 'staging'
            elif branch == 'staging':
                merge_target = 'main'
            if merge_target:
                try:
                    r5 = subprocess.run(
                        ['git', 'log', '--oneline', f'origin/{merge_target}..{branch}'],
                        capture_output=True, text=True, timeout=5, cwd=git_path,
                    )
                    if r5.returncode == 0 and r5.stdout.strip():
                        lines = [l for l in r5.stdout.strip().split('\n') if l.strip()]
                        merge_pending = len(lines)
                        for l in lines[:10]:
                            parts = l.split(' ', 1)
                            merge_pending_commits.append({
                                'hash': parts[0],
                                'message': parts[1] if len(parts) > 1 else '',
                            })
                except Exception:
                    pass
            repos.append({
                'path': git_path, 'name': os.path.basename(git_path),
                'branch': branch, 'ahead': ahead, 'behind': behind,
                'remote': remote, 'dirty': dirty, 'shallow': shallow,
                'merge_target': merge_target, 'merge_pending': merge_pending,
                'merge_pending_commits': merge_pending_commits,
            })

        # For non-production instances, only show repos inside the instance's own path
        inst_path = None
        inst_type = 'production'
        if instance_id:
            inst = request.env['devops.instance'].browse(instance_id)
            if inst.exists():
                inst_path = inst.instance_path
                inst_type = inst.instance_type or 'production'

        for path in addons_paths:
            # 1. Direct: path itself has .git
            if os.path.isdir(os.path.join(path, '.git')):
                _add_repo(path)
                continue
            # 2. Parent: walk up to find .git
            parent = os.path.dirname(path)
            found_parent = False
            while parent and parent != '/':
                if os.path.isdir(os.path.join(parent, '.git')):
                    _add_repo(parent)
                    found_parent = True
                    break
                parent = os.path.dirname(parent)
            # 3. Children: scan immediate subdirs for .git (e.g. custom_addons/pmb_devops/.git)
            if os.path.isdir(path):
                try:
                    for child in os.listdir(path):
                        child_path = os.path.join(path, child)
                        if os.path.isdir(os.path.join(child_path, '.git')):
                            _add_repo(child_path)
                except PermissionError:
                    pass

        # For dev/staging: filter repos from other instances, keep shared repos
        if inst_path and inst_type != 'production':
            instances_base = os.path.dirname(inst_path)
            repos = [r for r in repos
                     if r['path'].startswith(inst_path)
                     or not r['path'].startswith(instances_base + '/')]

        # Fallback: project.repo_path (only if not already found)
        if not repos and project.repo_path and project.repo_path not in seen:
            if os.path.isdir(os.path.join(project.repo_path, '.git')):
                repos.append({'path': project.repo_path, 'name': os.path.basename(project.repo_path), 'branch': 'HEAD'})

        is_admin = request.env.user.has_group('pmb_devops.group_devops_admin')
        return {'repos': repos, 'is_admin': is_admin}

    @http.route('/devops/repo/fetch_deeper', type='json', auth='user')
    def repo_fetch_deeper(self, repo_path, count=50):
        """Deepen a shallow clone by N commits."""
        if not repo_path or not os.path.isdir(repo_path):
            return {'error': 'Repo not found'}
        import subprocess
        try:
            result = subprocess.run(
                ['git', 'fetch', f'--deepen={count}'],
                capture_output=True, text=True, timeout=120, cwd=repo_path,
            )
            # Count commits now available
            r2 = subprocess.run(
                ['git', 'rev-list', '--count', 'HEAD'],
                capture_output=True, text=True, timeout=5, cwd=repo_path,
            )
            total = int(r2.stdout.strip()) if r2.returncode == 0 else 0
            shallow = os.path.exists(os.path.join(repo_path, '.git', 'shallow'))
            return {'status': 'ok', 'total_commits': total, 'still_shallow': shallow}
        except subprocess.TimeoutExpired:
            return {'error': 'Timeout'}
        except Exception as e:
            return {'error': str(e)}

    @http.route('/devops/branch/history', type='json', auth='user')
    def branch_history(self, project_id, branch_name, limit=20, search='', offset=0, repo_path='', **kw):
        """Get commit history for a branch with optional search.

        repo_path: absolute path to the git repo to query.
        Falls back to project.repo_path if not provided.
        """
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}

        if not repo_path:
            repo_path = project.repo_path or ''

        if not repo_path or not os.path.isdir(repo_path):
            return {'commits': []}

        from ..utils import git_utils

        class RepoOverride:
            def __init__(self, proj, path):
                self.connection_type = proj.connection_type
                self.ssh_host = proj.ssh_host
                self.ssh_user = proj.ssh_user
                self.ssh_port = proj.ssh_port
                self.ssh_key_path = proj.ssh_key_path
                self.repo_path = path

        proj_override = RepoOverride(project, repo_path)
        if search:
            commits = git_utils.git_search(proj_override, branch=branch_name, query=search, count=limit)
        else:
            commits = git_utils.git_log(proj_override, branch=branch_name, count=limit, skip=offset)
        return {'commits': commits}

    @http.route('/devops/commit/detail', type='json', auth='user')
    def commit_detail(self, project_id, commit_hash, repo_path=''):
        """Get commit detail: full message + changed files."""
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}

        cwd = repo_path or project.repo_path
        if not cwd or not os.path.isdir(cwd):
            return {'error': 'Repo path not found'}

        from ..utils import ssh_utils

        # Get full commit message (body)
        result = ssh_utils.execute_command(project, [
            'git', 'log', '-1', '--format=%B', commit_hash,
        ], cwd=cwd)
        body = result.stdout.strip() if result.returncode == 0 else ''

        # Get changed files with stats
        result = ssh_utils.execute_command(project, [
            'git', 'diff-tree', '--no-commit-id', '-r', '--name-status', commit_hash,
        ], cwd=cwd)
        files = []
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                parts = line.split('\t', 1)
                if len(parts) >= 2:
                    files.append({'status': parts[0], 'path': parts[1]})

        # Get diff stat
        result = ssh_utils.execute_command(project, [
            'git', 'diff-tree', '--no-commit-id', '--stat', commit_hash,
        ], cwd=cwd)
        stat = result.stdout.strip() if result.returncode == 0 else ''

        return {'body': body, 'files': files, 'stat': stat}

    @http.route('/devops/commit/file_diff', type='json', auth='user')
    def commit_file_diff(self, project_id, commit_hash, file_path, repo_path=''):
        """Get the diff of a specific file in a commit."""
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}

        cwd = repo_path or project.repo_path

        from ..utils import ssh_utils
        result = ssh_utils.execute_command(project, [
            'git', 'diff', f'{commit_hash}~1', commit_hash, '--', file_path,
        ], cwd=cwd)

        if result.returncode != 0:
            result = ssh_utils.execute_command(project, [
                'git', 'show', f'{commit_hash}', '--', file_path,
            ], cwd=cwd)

        return {'diff': result.stdout if result.returncode == 0 else 'No diff available'}

    @http.route('/devops/files/list', type='json', auth='user')
    def files_list(self, project_id, instance_id=None, path='', repo='addons'):
        """List files and directories. repo: 'addons', 'odoo', 'enterprise'."""
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}

        # Determine base directory based on repo type and instance
        base_dir = project.repo_path  # default: addons
        if instance_id:
            inst = request.env['devops.instance'].browse(instance_id)
            if inst.exists() and inst.instance_path and inst.instance_type != 'production':
                if repo == 'addons':
                    base_dir = f"{inst.instance_path}/cremara_addons"
                elif repo == 'odoo':
                    base_dir = f"{inst.instance_path}/odoo"
                elif repo == 'enterprise':
                    base_dir = f"{inst.instance_path}/enterprise"
            else:
                # Production
                if repo == 'odoo':
                    base_dir = '/opt/odooAL/odoo'
                elif repo == 'enterprise':
                    base_dir = project.enterprise_path or '/opt/odoo19/enterprise'
                    # If path doesn't exist, try common locations
                    if not os.path.isdir(base_dir):
                        for path_candidate in ['/opt/odoo19/enterprise', '/opt/odooAL/enterprise']:
                            if os.path.isdir(path_candidate):
                                base_dir = path_candidate
                                break

        from ..utils import ssh_utils

        full_path = os.path.normpath(os.path.join(base_dir, path))
        # Security: prevent directory traversal
        if not full_path.startswith(base_dir):
            return {'error': 'Acceso denegado'}

        result = ssh_utils.execute_command(project, [
            'find', full_path, '-maxdepth', '1', '-mindepth', '1', '-printf', '%y|||%f|||%s|||%T@\n',
        ], cwd=base_dir, timeout=10)

        if result.returncode != 0:
            return {'error': result.stderr or 'Error listando archivos'}

        items = []
        for line in result.stdout.strip().split('\n'):
            if not line.strip():
                continue
            parts = line.split('|||')
            if len(parts) >= 4:
                name = parts[1]
                if not name or name == '.':
                    continue
                items.append({
                    'type': 'dir' if parts[0] == 'd' else 'file',
                    'name': name,
                    'size': int(parts[2]) if parts[2].isdigit() else 0,
                    'path': os.path.join(path, name) if path else name,
                })

        # Sort: dirs first, then files, alphabetical
        items.sort(key=lambda x: (0 if x['type'] == 'dir' else 1, x['name'].lower()))
        return {'items': items, 'current_path': path, 'base_dir': base_dir}

    @http.route('/devops/files/read', type='json', auth='user')
    def files_read(self, project_id, instance_id=None, path='', repo='addons'):
        """Read file content."""
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}

        base_dir = project.repo_path
        if instance_id:
            inst = request.env['devops.instance'].browse(instance_id)
            if inst.exists() and inst.instance_path and inst.instance_type != 'production':
                if repo == 'addons':
                    base_dir = f"{inst.instance_path}/cremara_addons"
                elif repo == 'odoo':
                    base_dir = f"{inst.instance_path}/odoo"
                elif repo == 'enterprise':
                    base_dir = f"{inst.instance_path}/enterprise"
            else:
                if repo == 'odoo':
                    base_dir = '/opt/odooAL/odoo'
                elif repo == 'enterprise':
                    base_dir = project.enterprise_path or '/opt/odoo19/enterprise'
                    # If path doesn't exist, try common locations
                    if not os.path.isdir(base_dir):
                        for path_candidate in ['/opt/odoo19/enterprise', '/opt/odooAL/enterprise']:
                            if os.path.isdir(path_candidate):
                                base_dir = path_candidate
                                break

        from ..utils import ssh_utils

        full_path = os.path.normpath(os.path.join(base_dir, path))
        if not full_path.startswith(base_dir):
            return {'error': 'Acceso denegado'}

        result = ssh_utils.execute_command(project, [
            'head', '-c', '500000', full_path,
        ], cwd=base_dir, timeout=10)

        if result.returncode != 0:
            return {'error': result.stderr or 'Error leyendo archivo'}

        return {'content': result.stdout, 'path': path, 'name': os.path.basename(path)}

    @http.route('/devops/git/status', type='json', auth='user')
    def git_status(self, project_id, instance_id=None, repo_path=''):
        """Get git status: staged, unstaged, and untracked files."""
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}

        from ..utils import ssh_utils

        # Use explicit repo_path if provided (from frontend repo discovery)
        if not repo_path:
            repo_path = project.repo_path or ''
            if instance_id:
                inst = request.env['devops.instance'].browse(instance_id)
                if inst.exists() and inst.instance_path and inst.instance_type != 'production':
                    repo_path = f"{inst.instance_path}/cremara_addons"

        if not repo_path or not os.path.isdir(repo_path):
            return {'staged': [], 'unstaged': [], 'untracked': [], 'outgoing': []}

        # git status --porcelain=v1
        result = ssh_utils.execute_command(project, [
            'git', 'status', '--porcelain=v1',
        ], cwd=repo_path, timeout=10)

        staged = []
        unstaged = []
        untracked = []
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if not line:
                    continue
                x = line[0]   # index status
                y = line[1]   # worktree status
                filepath = line[3:]
                if x == '?':
                    untracked.append(filepath)
                elif x != ' ':
                    staged.append({'status': x, 'path': filepath})
                if y != ' ' and y != '?':
                    unstaged.append({'status': y, 'path': filepath})

        # git log outgoing (ahead of remote)
        outgoing = []
        result = ssh_utils.execute_command(project, [
            'git', 'log', '--oneline', '@{upstream}..HEAD',
        ], cwd=repo_path, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    parts = line.split(' ', 1)
                    outgoing.append({
                        'hash': parts[0],
                        'message': parts[1] if len(parts) > 1 else '',
                    })

        return {
            'staged': staged,
            'unstaged': unstaged,
            'untracked': untracked,
            'outgoing': outgoing,
        }

    @http.route('/devops/git/stage', type='json', auth='user')
    def git_stage(self, project_id, repo_path=''):
        """Stage all changes (git add -A)."""
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}
        if not repo_path or not os.path.isdir(repo_path):
            return {'error': 'Repo path not found'}
        from ..utils import ssh_utils
        result = ssh_utils.execute_command(project, ['git', 'add', '-A'], cwd=repo_path, timeout=15)
        if result.returncode != 0:
            return {'error': result.stderr.strip() or 'git add failed'}
        return {'status': 'ok'}

    @http.route('/devops/git/commit', type='json', auth='user')
    def git_commit(self, project_id, repo_path='', message=''):
        """Stage all and commit with the given message."""
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}
        if not repo_path or not os.path.isdir(repo_path):
            return {'error': 'Repo path not found'}
        if not message or not message.strip():
            return {'error': 'Commit message is required'}
        from ..utils import ssh_utils
        # Ensure git user is configured (use the logged-in Odoo user)
        user = request.env.user
        r = ssh_utils.execute_command(project, ['git', 'config', 'user.email'], cwd=repo_path, timeout=5)
        if r.returncode != 0 or not r.stdout.strip():
            ssh_utils.execute_command(project, ['git', 'config', 'user.email', user.email or user.login], cwd=repo_path, timeout=5)
            ssh_utils.execute_command(project, ['git', 'config', 'user.name', user.name or user.login], cwd=repo_path, timeout=5)
        # Stage all changes first
        ssh_utils.execute_command(project, ['git', 'add', '-A'], cwd=repo_path, timeout=15)
        # Commit
        result = ssh_utils.execute_command(project, [
            'git', 'commit', '-m', message.strip(),
        ], cwd=repo_path, timeout=30)
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            return {'error': err or 'git commit failed'}
        return {'status': 'ok', 'output': result.stdout.strip()}

    @http.route('/devops/git/push', type='json', auth='user')
    def git_push(self, project_id, repo_path=''):
        """Push current branch to origin."""
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}
        if not repo_path or not os.path.isdir(repo_path):
            return {'error': 'Repo path not found'}
        from ..utils import ssh_utils
        # Get current branch
        r = ssh_utils.execute_command(project, [
            'git', 'rev-parse', '--abbrev-ref', 'HEAD',
        ], cwd=repo_path, timeout=5)
        branch = r.stdout.strip() if r.returncode == 0 else 'HEAD'
        # Push
        result = ssh_utils.execute_command(project, [
            'git', 'push', 'origin', branch,
        ], cwd=repo_path, timeout=60)
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            return {'error': err or 'git push failed'}
        return {'status': 'ok', 'output': result.stdout.strip() + '\n' + result.stderr.strip()}

    @http.route('/devops/git/pull', type='json', auth='user')
    def git_pull(self, project_id, repo_path='', branch=''):
        """Pull latest changes from origin for a branch."""
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}
        if not repo_path or not os.path.isdir(repo_path):
            return {'error': 'Repo path not found'}
        from ..utils import ssh_utils
        # Ensure git user configured
        user = request.env.user
        r = ssh_utils.execute_command(project, ['git', 'config', 'user.email'], cwd=repo_path, timeout=5)
        if r.returncode != 0 or not r.stdout.strip():
            ssh_utils.execute_command(project, ['git', 'config', 'user.email', user.email or user.login], cwd=repo_path, timeout=5)
            ssh_utils.execute_command(project, ['git', 'config', 'user.name', user.name or user.login], cwd=repo_path, timeout=5)
        # Fetch first
        ssh_utils.execute_command(project, ['git', 'fetch', 'origin'], cwd=repo_path, timeout=60)
        # Pull current branch
        if not branch:
            r = ssh_utils.execute_command(project, ['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=repo_path, timeout=5)
            branch = r.stdout.strip() if r.returncode == 0 else 'HEAD'
        result = ssh_utils.execute_command(project, ['git', 'pull', 'origin', branch], cwd=repo_path, timeout=60)
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            return {'error': err or 'git pull failed'}
        return {'status': 'ok', 'output': result.stdout.strip()}

    @http.route('/devops/branch/merge', type='json', auth='user')
    def branch_merge(self, project_id, repo_path='', source_branch='', target_branch=''):
        """Merge source branch into target. Flow: development → staging → main."""
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}
        if not repo_path or not os.path.isdir(repo_path):
            # Fallback to project repo_path
            repo_path = project.repo_path
        if not repo_path or not os.path.isdir(repo_path):
            return {'error': 'Repo path not found'}

        # Validate merge direction: development → staging → main only
        # Admin required for staging → main
        is_admin = request.env.user.has_group('pmb_devops.group_devops_admin')
        allowed_merges = {
            'development': ['staging'],
        }
        if is_admin:
            allowed_merges['staging'] = ['main']
        allowed_targets = allowed_merges.get(source_branch, [])
        if target_branch not in allowed_targets:
            return {'error': f'No se permite merge de {source_branch} → {target_branch}. '
                    f'Flujo correcto: development → staging → main'}

        from ..utils import ssh_utils
        # Ensure git user configured
        user = request.env.user
        r = ssh_utils.execute_command(project, ['git', 'config', 'user.email'], cwd=repo_path, timeout=5)
        if r.returncode != 0 or not r.stdout.strip():
            ssh_utils.execute_command(project, ['git', 'config', 'user.email', user.email or user.login], cwd=repo_path, timeout=5)
            ssh_utils.execute_command(project, ['git', 'config', 'user.name', user.name or user.login], cwd=repo_path, timeout=5)

        try:
            # Fetch latest
            ssh_utils.execute_command(project, ['git', 'fetch', 'origin'], cwd=repo_path, timeout=60)
            # Save current branch to return to it later
            r = ssh_utils.execute_command(project, ['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=repo_path, timeout=5)
            original_branch = r.stdout.strip() if r.returncode == 0 else ''
            # Checkout target
            r = ssh_utils.execute_command(project, ['git', 'checkout', target_branch], cwd=repo_path, timeout=15)
            if r.returncode != 0:
                return {'error': f'Error al cambiar a {target_branch}: {r.stderr.strip()}'}
            # Pull target to ensure it's up to date
            ssh_utils.execute_command(project, ['git', 'pull', 'origin', target_branch], cwd=repo_path, timeout=60)
            # Merge source into target
            result = ssh_utils.execute_command(project, [
                'git', 'merge', f'origin/{source_branch}',
                '-m', f'Merge {source_branch} into {target_branch}',
            ], cwd=repo_path, timeout=60)
            if result.returncode != 0:
                # Abort merge on conflict
                ssh_utils.execute_command(project, ['git', 'merge', '--abort'], cwd=repo_path, timeout=5)
                if original_branch:
                    ssh_utils.execute_command(project, ['git', 'checkout', original_branch], cwd=repo_path, timeout=15)
                return {'error': f'Conflicto de merge: {result.stderr.strip() or result.stdout.strip()}'}
            # Push
            push_r = ssh_utils.execute_command(project, ['git', 'push', 'origin', target_branch], cwd=repo_path, timeout=60)
            # Return to original branch
            if original_branch and original_branch != target_branch:
                ssh_utils.execute_command(project, ['git', 'checkout', original_branch], cwd=repo_path, timeout=15)
            if push_r.returncode != 0:
                return {'error': f'Merge exitoso pero push falló: {push_r.stderr.strip()}'}
            return {'status': 'ok', 'output': f'Merge {source_branch} → {target_branch} exitoso y pusheado'}
        except Exception as e:
            return {'error': str(e)}

    @http.route('/devops/project/metrics', type='json', auth='user')
    def project_metrics(self, project_id, refresh=False):
        """Get server metrics for a project. If refresh=True, collect now."""
        import json as json_mod
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}
        if refresh or not project.server_metrics:
            try:
                metrics = project._collect_metrics(project)
                project.sudo().write({
                    'server_metrics': json_mod.dumps(metrics),
                    'server_metrics_updated': fields.Datetime.now(),
                })
                return {'metrics': metrics, 'updated': str(fields.Datetime.now())}
            except Exception as e:
                return {'error': str(e)}
        try:
            return {
                'metrics': json_mod.loads(project.server_metrics),
                'updated': str(project.server_metrics_updated or ''),
            }
        except Exception:
            return {'metrics': {}, 'updated': ''}

    @http.route('/devops/project/status', type='json', auth='user')
    def project_status(self, project_id):
        """Get project status summary."""
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}
        return {
            'name': project.name,
            'domain': project.domain,
            'instance_count': len(project.instance_ids),
            'running': len(project.instance_ids.filtered(lambda i: i.state == 'running')),
        }

    # ------------------------------------------------------------------
    # Project CRUD + SSH key management
    # ------------------------------------------------------------------

    @http.route('/devops/project/get', type='json', auth='user')
    def project_get(self, project_id):
        """Get full project config for settings tab."""
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}
        return {
            'id': project.id,
            'name': project.name,
            'domain': project.domain,
            'repo_path': project.repo_path,
            'repo_url': project.repo_url,
            'enterprise_path': project.enterprise_path or '',
            'database_name': project.database_name or '',
            'connection_type': project.connection_type or 'local',
            'ssh_host': project.ssh_host or '',
            'ssh_user': project.ssh_user or '',
            'ssh_port': project.ssh_port or 22,
            'ssh_key_path': project.ssh_key_path or '',
            'max_staging': project.max_staging,
            'max_development': project.max_development,
            'auto_destroy_hours': project.auto_destroy_hours,
            'production_branch': project.production_branch or 'main',
            'ssh_key_configured': bool(project.ssh_key_path and os.path.exists(project.ssh_key_path)),
        }

    @http.route('/devops/project/save', type='json', auth='user')
    def project_save(self, project_id=None, **vals):
        """Create or update a project."""
        allowed_fields = [
            'name', 'domain', 'repo_path', 'enterprise_path', 'database_name',
            'connection_type', 'ssh_host', 'ssh_user', 'ssh_port',
            'max_staging', 'max_development', 'auto_destroy_hours',
            'production_branch',
        ]
        write_vals = {k: v for k, v in vals.items() if k in allowed_fields}

        if project_id:
            project = request.env['devops.project'].browse(project_id)
            if not project.exists():
                return {'error': 'Proyecto no encontrado'}
            project.write(write_vals)
        else:
            if not write_vals.get('name'):
                return {'error': 'El nombre es requerido'}
            project = request.env['devops.project'].create(write_vals)

        return {'status': 'ok', 'project_id': project.id}

    @http.route('/devops/project/generate_ssh_key', type='json', auth='user')
    def project_generate_ssh_key(self, project_id):
        """Generate an ED25519 SSH keypair for a project.

        Stores the key in /opt/odooAL/.ssh/pmb_<project_id>_ed25519
        Returns the public key so user can add it to the server.
        """
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}

        import subprocess
        ssh_dir = '/opt/odooAL/.ssh'
        os.makedirs(ssh_dir, exist_ok=True)
        os.chmod(ssh_dir, 0o700)

        key_name = f'pmb_{project_id}_ed25519'
        key_path = os.path.join(ssh_dir, key_name)

        # Remove old key if exists
        for ext in ['', '.pub']:
            try:
                os.remove(key_path + ext)
            except FileNotFoundError:
                pass

        # Generate keypair
        result = subprocess.run(
            ['ssh-keygen', '-t', 'ed25519', '-f', key_path, '-N', '',
             '-C', f'pmb-devops-{project.name}@asistentelisto.com'],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return {'error': f'ssh-keygen failed: {result.stderr}'}

        # Read public key
        with open(key_path + '.pub', 'r') as f:
            pub_key = f.read().strip()

        # Save key path to project
        project.write({'ssh_key_path': key_path})

        return {
            'status': 'ok',
            'public_key': pub_key,
            'key_path': key_path,
            'instructions': (
                f'Agrega esta llave publica al servidor {project.ssh_host or "remoto"}:\n\n'
                f'1. Copia la llave publica\n'
                f'2. En la terminal AI (Claude Code), escribe:\n'
                f'   "Configura SSH al servidor {project.ssh_host or "X.X.X.X"} '
                f'con usuario {project.ssh_user or "root"}, '
                f'usa ssh-copy-id con la llave {key_path}"\n\n'
                f'Claude te pedira la contrasena una sola vez.'
            ),
        }

    @http.route('/devops/project/test_ssh', type='json', auth='user')
    def project_test_ssh(self, project_id):
        """Test SSH connection to the project's server."""
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}

        if project.connection_type != 'ssh':
            return {'error': 'Proyecto no usa conexion SSH'}

        from ..utils import ssh_utils
        try:
            result = ssh_utils.execute_command(project, ['echo', 'PMB_SSH_OK'], timeout=10)
            if result.returncode == 0 and 'PMB_SSH_OK' in result.stdout:
                return {'status': 'ok', 'message': 'Conexion SSH exitosa'}
            else:
                return {'status': 'error', 'message': result.stderr or 'Sin respuesta'}
        except Exception as e:
            return {'status': 'error', 'message': str(e)}
