"""API endpoints for the PMB DevOps SPA."""
import logging
import os

from odoo import fields, http
from odoo.http import request

_logger = logging.getLogger(__name__)


class DevopsController(http.Controller):

    # Throttled activity touch — avoids DB write on every request
    _activity_cache = {}  # {instance_id: last_touch_timestamp}

    def _touch_activity(self, instance_id):
        """Update last_activity on an instance, throttled to once per 5 min."""
        if not instance_id:
            return
        import time
        now = time.time()
        last = self._activity_cache.get(instance_id, 0)
        if now - last < 300:  # 5 min throttle
            return
        self._activity_cache[instance_id] = now
        try:
            inst = request.env['devops.instance'].sudo().browse(instance_id)
            if inst.exists() and inst.state == 'running':
                inst.write({'last_activity': fields.Datetime.now()})
        except Exception:
            pass

    @http.route('/devops/assets/clear', type='json', auth='user')
    def assets_clear(self):
        """Clear all compiled asset bundles to force regeneration."""
        request.env['ir.attachment'].sudo().search([
            '|',
            ('name', 'like', 'assets'),
            ('url', 'like', '/web/assets'),
        ]).unlink()
        # Also clear the asset caches via ir.qweb
        request.env.registry.clear_all_caches()
        return {'status': 'ok'}

    @http.route('/devops/project/data', type='json', auth='user')
    def project_data(self, project_id):
        """Get all instances + branches for a project (sidebar data)."""
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}
        # Refresh service status for SSH projects
        if project.connection_type == 'ssh' and project.ssh_host:
            try:
                project.instance_ids._check_service_status()
            except Exception:
                pass

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
        """Create a new staging/development instance (admin or developer)."""
        if not request.env.user.has_group('pmb_devops.group_devops_developer'):
            return {'error': 'Se requiere rol Developer o Admin para crear instancias'}
        # Use sudo for internal operations (developer may not see production via record rules)
        Project = request.env['devops.project'].sudo()
        Instance = request.env['devops.instance'].sudo()
        Branch = request.env['devops.branch'].sudo()

        project = Project.browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}

        # Check if instance with same name already exists
        existing = Instance.search([
            ('project_id', '=', project_id),
            ('name', '=', name),
        ], limit=1)
        if existing:
            return {'error': f'Ya existe una instancia "{name}" en este proyecto.'}

        # Find or create branch record
        branch = Branch.search([
            ('project_id', '=', project_id),
            ('name', '=', name),
        ], limit=1)
        if not branch:
            branch = Branch.create({
                'project_id': project_id,
                'name': name,
                'branch_type': instance_type,
            })

        # Determine clone source
        clone_from = False
        if clone_from_id:
            clone_from = Instance.browse(clone_from_id)
        elif instance_type == 'staging' and project.production_instance_id:
            clone_from = project.production_instance_id
        elif instance_type == 'development':
            staging = Instance.search([
                ('project_id', '=', project_id),
                ('instance_type', '=', 'staging'),
                ('state', '=', 'running'),
            ], limit=1)
            clone_from = staging or project.production_instance_id

        # Create instance
        instance = Instance.create({
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

        project = instance.project_id
        is_ssh = project.connection_type == 'ssh' and project.ssh_host

        log_lines = ''
        new_log_pos = log_pos
        creation_step = instance.creation_step or ''

        if instance.state in ('creating', 'error'):
            if is_ssh:
                # SSH: read status and log from remote files
                status_file = f"/tmp/pmb_create_{instance.id}.status"
                log_file = f"/tmp/pmb_create_{instance.id}.log"
                try:
                    # Read current step from status file
                    status_out = self._cmd_on_project(
                        project, f'cat {status_file} 2>/dev/null || echo running',
                    )
                    remote_status = status_out.strip()

                    # Read log from remote
                    log_out = self._cmd_on_project(
                        project,
                        f'tail -c +{log_pos + 1} {log_file} 2>/dev/null',
                    )
                    log_lines = log_out
                    new_log_pos = log_pos + len(log_lines.encode('utf-8'))

                    # Update local record from remote status
                    if remote_status == 'done':
                        instance.sudo().write({
                            'state': 'running',
                            'creation_step': '',
                            'creation_pid': 0,
                        })
                        creation_step = ''
                    elif remote_status.startswith('error:'):
                        err_msg = remote_status[6:].strip()
                        instance.sudo().write({
                            'state': 'error',
                            'creation_step': f'Error: {err_msg}',
                            'creation_pid': 0,
                        })
                        creation_step = f'Error: {err_msg}'
                    else:
                        # Intermediate step
                        creation_step = remote_status
                        instance.sudo().write({'creation_step': remote_status})
                except Exception as e:
                    _logger.warning("SSH poll_status error: %s", e)
            else:
                # Local: read from shared log file
                log_file = '/var/log/odoo/pmb_creation.log'
                try:
                    with open(log_file, 'rb') as f:
                        f.seek(0, 2)  # end
                        file_size = f.tell()
                        if log_pos == 0:
                            marker = f'id={instance.id})'.encode()
                            read_from = max(0, file_size - 50000)
                            f.seek(read_from)
                            chunk = f.read()
                            idx = chunk.rfind(marker)
                            if idx >= 0:
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
            'creation_step': creation_step,
            'creation_pid': instance.creation_pid or 0,
            'log': log_lines,
            'log_pos': new_log_pos,
        }

    @http.route('/devops/instance/detect_service', type='json', auth='user')
    def instance_detect_service(self, service_name, project_id=None):
        """Auto-detect Odoo config from a systemd service name.
        Supports SSH projects — runs commands on remote server if needed.
        """
        return self.project_autodetect(service_name, project_id)

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
        detected = self.instance_detect_service(service_name, project_id=project_id)
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

        # Enforce .gitignore on the production repo
        if repo_path:
            from ..utils.git_utils import ensure_gitignore
            ensure_gitignore(repo_path, project=project)

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
        self._touch_activity(instance_id)
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
        is_ssh = project.connection_type == 'ssh' and project.ssh_host

        if is_ssh:
            # SSH: transfer script to remote and execute there
            from ..utils import ssh_utils
            # Write script locally first
            with open(script_path, 'w') as f:
                f.write(script)
            # Transfer to remote
            scp_cmd = ['scp', '-o', 'StrictHostKeyChecking=no']
            if project.ssh_key_path and os.path.isfile(project.ssh_key_path):
                scp_cmd += ['-i', project.ssh_key_path]
            if project.ssh_port and project.ssh_port != 22:
                scp_cmd += ['-P', str(project.ssh_port)]
            scp_cmd += [script_path, f'{project.ssh_user or "root"}@{project.ssh_host}:{script_path}']
            subprocess.run(scp_cmd, capture_output=True, timeout=30)
            # Execute remotely, redirect output back via SSH tail
            ssh_base = ['ssh', '-o', 'StrictHostKeyChecking=no']
            if project.ssh_key_path and os.path.isfile(project.ssh_key_path):
                ssh_base += ['-i', project.ssh_key_path]
            if project.ssh_port and project.ssh_port != 22:
                ssh_base += ['-p', str(project.ssh_port)]
            ssh_base += [f'{project.ssh_user or "root"}@{project.ssh_host}']
            # Run script on remote, pipe log back to local
            remote_cmd = f'bash {script_path} && cat {log_file}'
            subprocess.Popen(
                ssh_base + [f'nohup bash {script_path} > /dev/null 2>&1 &'],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Poll will read log from remote
            open(log_file, 'w').write('Deploy iniciado en servidor remoto...\n')
            open(status_file, 'w').write('running')
        else:
            # Local execution
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

        return {'status': 'started', 'deploy_id': deploy_id, 'is_ssh': is_ssh}

    @http.route('/devops/instance/deploy_status', type='json', auth='user')
    def instance_deploy_status(self, deploy_id, log_pos=0, instance_id=None):
        """Poll deploy progress (local or SSH)."""
        log_file = f"/tmp/pmb_{deploy_id}.log"
        status_file = f"/tmp/pmb_{deploy_id}.status"

        # Check if this is an SSH deploy
        is_ssh = False
        project = None
        if instance_id:
            inst = request.env['devops.instance'].browse(instance_id)
            if inst.exists():
                project = inst.project_id
                is_ssh = project.connection_type == 'ssh' and project.ssh_host

        if is_ssh and project:
            # Read log and status from remote
            status_out = self._cmd_on_project(project, f'cat {status_file} 2>/dev/null || echo running')
            status = status_out.strip() or 'running'
            log_out = self._cmd_on_project(project, f'tail -c +{log_pos + 1} {log_file} 2>/dev/null')
            log = log_out
            new_pos = log_pos + len(log.encode('utf-8'))
        else:
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
        """Destroy an instance (admin or developer)."""
        if not request.env.user.has_group('pmb_devops.group_devops_developer'):
            return {'error': 'Se requiere rol Developer o Admin para eliminar instancias'}
        instance = request.env['devops.instance'].browse(instance_id)
        if not instance.exists():
            return {'error': 'Instancia no encontrada'}
        try:
            instance.sudo().action_destroy()
            return {'status': 'ok'}
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    def _cmd_on_project(self, project, cmd_str, timeout=30):
        """Run a shell command locally or via SSH depending on project type."""
        import subprocess
        if project.connection_type == 'ssh' and project.ssh_host:
            ssh_cmd = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=10']
            if project.ssh_key_path and os.path.isfile(project.ssh_key_path):
                ssh_cmd += ['-i', project.ssh_key_path]
            if project.ssh_port and project.ssh_port != 22:
                ssh_cmd += ['-p', str(project.ssh_port)]
            ssh_cmd += [f'{project.ssh_user or "root"}@{project.ssh_host}', cmd_str]
            r = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout)
        else:
            r = subprocess.run(cmd_str, shell=True, capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ''

    @http.route('/devops/instance/diagnose', type='json', auth='user')
    def instance_diagnose(self, instance_id):
        """Diagnose an instance's health: check paths, service, config."""
        instance = request.env['devops.instance'].browse(instance_id)
        if not instance.exists():
            return {'error': 'Instancia no encontrada'}

        project = instance.project_id
        run = lambda cmd: self._cmd_on_project(project, cmd)
        issues = []
        info = []

        # Check instance_path
        if instance.instance_path:
            if run(f'test -d {instance.instance_path} && echo ok') == 'ok':
                info.append({'type': 'ok', 'msg': f'Directorio {instance.instance_path} existe'})
            else:
                issues.append({'type': 'error', 'msg': f'Directorio {instance.instance_path} NO existe', 'fix': 'create_dir'})

        # Check repo_path
        repo = project.repo_path
        if repo:
            if run(f'test -d {repo} && echo ok') == 'ok':
                info.append({'type': 'ok', 'msg': f'Repo {repo} existe'})
            else:
                issues.append({'type': 'warning', 'msg': f'Repo {repo} NO existe'})

        # Check service
        if instance.service_name:
            status = run(f'systemctl is-active {instance.service_name}.service')
            if status == 'active':
                info.append({'type': 'ok', 'msg': f'Servicio {instance.service_name} activo'})
            else:
                issues.append({'type': 'error', 'msg': f'Servicio {instance.service_name}: {status or "desconocido"}', 'fix': 'start_service'})

            # Check if enabled
            enabled = run(f'systemctl is-enabled {instance.service_name}.service')
            if enabled and enabled != 'enabled':
                issues.append({'type': 'warning', 'msg': f'Servicio {instance.service_name} no habilitado ({enabled})', 'fix': 'enable_service'})

            # Check config
            import re
            exec_line = run(f'systemctl show {instance.service_name}.service --property=ExecStart')
            if exec_line:
                m = re.search(r'-c\s+(\S+)', exec_line)
                if m:
                    conf = m.group(1)
                    if run(f'test -f {conf} && echo ok') == 'ok':
                        info.append({'type': 'ok', 'msg': f'Config {conf} existe'})
                    else:
                        issues.append({'type': 'error', 'msg': f'Config {conf} NO existe'})
        else:
            issues.append({'type': 'error', 'msg': 'No hay servicio systemd configurado'})

        return {'issues': issues, 'info': info, 'total_issues': len(issues)}

    @http.route('/devops/instance/fix', type='json', auth='user')
    def instance_fix(self, instance_id, fix_type=''):
        """Attempt to fix a diagnosed issue (local or SSH)."""
        instance = request.env['devops.instance'].browse(instance_id)
        if not instance.exists():
            return {'error': 'Instancia no encontrada'}
        if not request.env.user.has_group('pmb_devops.group_devops_admin'):
            return {'error': 'Solo administradores pueden reparar'}

        project = instance.project_id
        run = lambda cmd: self._cmd_on_project(project, cmd)

        if fix_type == 'create_dir' and instance.instance_path:
            result = run(f'mkdir -p {instance.instance_path} && echo ok')
            if result == 'ok':
                return {'status': 'ok', 'msg': f'Directorio {instance.instance_path} creado'}
            return {'error': 'No se pudo crear el directorio'}
        elif fix_type == 'start_service' and instance.service_name:
            run(f'sudo systemctl start {instance.service_name}.service')
            status = run(f'systemctl is-active {instance.service_name}.service')
            return {'status': 'ok', 'msg': f'Servicio {instance.service_name}: {status}', 'state': status}
        elif fix_type == 'enable_service' and instance.service_name:
            run(f'sudo systemctl enable {instance.service_name}.service')
            return {'status': 'ok', 'msg': f'Servicio {instance.service_name} habilitado'}
        return {'error': f'Fix type desconocido: {fix_type}'}

    @http.route('/devops/instance/cleanup', type='json', auth='user')
    def instance_cleanup(self, instance_id):
        """Delete a failed/error instance and its associated records."""
        if not request.env.user.has_group('pmb_devops.group_devops_admin'):
            return {'error': 'Solo administradores'}
        instance = request.env['devops.instance'].sudo().browse(instance_id)
        if not instance.exists():
            return {'error': 'Instancia no encontrada'}
        if instance.state not in ('error', 'creating'):
            return {'error': 'Solo se pueden limpiar instancias en error o creando'}
        name = instance.name
        try:
            instance.action_destroy()
        except Exception:
            # Force delete if destroy fails
            instance.unlink()
        return {'status': 'ok', 'msg': f'Instancia {name} eliminada'}

    @http.route('/devops/instance/start', type='json', auth='user')
    def instance_start(self, instance_id):
        """Start an instance (local or SSH)."""
        instance = request.env['devops.instance'].browse(instance_id)
        if not instance.exists():
            return {'error': 'Instancia no encontrada'}
        project = instance.project_id
        if project.connection_type == 'ssh' and project.ssh_host:
            self._cmd_on_project(project, f'sudo systemctl start {instance.service_name}.service')
            status = self._cmd_on_project(project, f'systemctl is-active {instance.service_name}.service')
            instance.sudo().write({'state': 'running' if status == 'active' else 'stopped'})
            return {'status': 'ok', 'state': instance.state}
        try:
            instance.action_start()
            return {'status': 'ok', 'state': instance.state}
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    @http.route('/devops/instance/stop', type='json', auth='user')
    def instance_stop(self, instance_id):
        """Stop an instance (local or SSH)."""
        instance = request.env['devops.instance'].browse(instance_id)
        if not instance.exists():
            return {'error': 'Instancia no encontrada'}
        project = instance.project_id
        if project.connection_type == 'ssh' and project.ssh_host:
            self._cmd_on_project(project, f'sudo systemctl stop {instance.service_name}.service')
            instance.sudo().write({'state': 'stopped'})
            return {'status': 'ok', 'state': 'stopped'}
        try:
            instance.action_stop()
            return {'status': 'ok', 'state': instance.state}
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    @http.route('/devops/instance/restart', type='json', auth='user')
    def instance_restart(self, instance_id):
        """Restart an instance (local or SSH)."""
        instance = request.env['devops.instance'].browse(instance_id)
        if not instance.exists():
            return {'error': 'Instancia no encontrada'}
        project = instance.project_id
        if project.connection_type == 'ssh' and project.ssh_host:
            self._cmd_on_project(project, f'sudo systemctl restart {instance.service_name}.service')
            status = self._cmd_on_project(project, f'systemctl is-active {instance.service_name}.service')
            instance.sudo().write({'state': 'running' if status == 'active' else 'error'})
            return {'status': 'ok', 'state': instance.state}
        try:
            instance.action_restart()
            return {'status': 'ok', 'state': instance.state}
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    @http.route('/devops/instance/repos', type='json', auth='user')
    def instance_repos(self, project_id, instance_id=None):
        """Detect available git repos for an instance from its Odoo config."""
        self._touch_activity(instance_id)
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'repos': []}

        repos = []
        config_path = ''

        # For SSH projects, detect repos via SSH
        if project.connection_type == 'ssh' and project.ssh_host:
            import subprocess, re
            svc = project.odoo_service_name or ''
            if instance_id:
                inst = request.env['devops.instance'].browse(instance_id)
                if inst.exists() and inst.service_name:
                    svc = inst.service_name
            if svc:
                detected = self.project_autodetect(svc, project_id)
                addons_path = detected.get('addons_path', '')
                if addons_path:
                    # Build SSH command helper
                    def ssh_run(cmd_str):
                        ssh_cmd = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=10']
                        if project.ssh_key_path and os.path.isfile(project.ssh_key_path):
                            ssh_cmd += ['-i', project.ssh_key_path]
                        if project.ssh_port and project.ssh_port != 22:
                            ssh_cmd += ['-p', str(project.ssh_port)]
                        ssh_cmd += [f'{project.ssh_user or "root"}@{project.ssh_host}', cmd_str]
                        r = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=15)
                        return r.stdout.strip() if r.returncode == 0 else ''

                    # Collect unique paths (dedup parent/child)
                    paths = [p.strip() for p in addons_path.split(',') if p.strip()]

                    # Find git repos + custom addons dirs
                    # Script: for each path, check .git (self, parent, children)
                    # Also output ADDON: for paths that are custom addons without .git
                    script = 'for p in ' + ' '.join(paths) + '; do '
                    script += 'if [ -d "$p/.git" ]; then echo "REPO:$p"; '
                    script += 'else parent=$(dirname "$p"); '
                    script += 'if [ -d "$parent/.git" ]; then echo "REPO:$parent"; '
                    script += 'elif [ -d "$p" ]; then '
                    # Check children for .git
                    script += 'found=0; for c in "$p"/*/; do '
                    script += 'if [ -d "$c/.git" ]; then echo "REPO:${c%/}"; found=1; fi; done; '
                    # If no .git found and it's not an odoo core path, list as addon dir
                    script += 'if [ "$found" = "0" ] && ! echo "$p" | grep -q "/odoo/"; then echo "ADDON:$p"; fi; '
                    script += 'fi; fi; '
                    script += 'done | sort -u'
                    raw = ssh_run(script)

                    # Determine expected branch for staging/dev instances
                    expected_branch = ''
                    inst_obj = None
                    if instance_id:
                        inst_obj = request.env['devops.instance'].browse(instance_id)
                        if inst_obj.exists() and inst_obj.instance_type != 'production':
                            expected_branch = inst_obj.git_branch or ''

                    seen = set()
                    for line in raw.split('\n'):
                        line = line.strip()
                        if line.startswith('REPO:'):
                            git_path = line[5:]
                            if git_path in seen:
                                continue
                            seen.add(git_path)
                            name = os.path.basename(git_path)

                            # Fix safe.directory for this repo
                            ssh_run(f'git config --global --add safe.directory {git_path} 2>/dev/null')

                            branch = ssh_run(f'git -C {git_path} branch --show-current 2>/dev/null') or 'HEAD'
                            repo_type = 'custom'
                            if ssh_run(f'test -f {git_path}/odoo-bin && echo yes') == 'yes':
                                repo_type = 'odoo'
                            elif 'enterprise' in git_path.lower():
                                repo_type = 'enterprise'

                            # Auto-fix: ensure custom repos are on the correct instance branch
                            if expected_branch and repo_type == 'custom' and branch != expected_branch:
                                ssh_run(
                                    f'cd {git_path} && '
                                    f'git checkout {expected_branch} 2>/dev/null || '
                                    f'(git checkout -b {expected_branch} && '
                                    f'git push -u origin {expected_branch} 2>/dev/null); '
                                    f'true'
                                )
                                branch = ssh_run(f'git -C {git_path} branch --show-current 2>/dev/null') or branch

                            # Auto-install pre-push hook if missing (branch protection)
                            if expected_branch and repo_type == 'custom':
                                inst_type = inst_obj.instance_type if inst_obj else 'staging'
                                protected = 'main|master' if inst_type == 'staging' else 'main|master|staging'
                                ssh_run(
                                    f'if [ ! -f {git_path}/.git/hooks/pre-push ]; then '
                                    f'  echo \'#!/bin/bash\n'
                                    f'while read l ls r rs; do b=$(echo "$r"|sed "s|refs/heads/||"); '
                                    f'if echo "$b"|grep -qE "^({protected})$"; then '
                                    f'echo "ERROR: Push a $b bloqueado."; exit 1; fi; done; exit 0\' '
                                    f'> {git_path}/.git/hooks/pre-push && '
                                    f'chmod +x {git_path}/.git/hooks/pre-push; fi'
                                )

                            dirty = bool(ssh_run(f'git -C {git_path} status --porcelain 2>/dev/null'))

                            # Merge/sync targets based on instance type
                            merge_target = ''
                            merge_pending = 0
                            merge_pending_commits = []
                            sync_pending = 0
                            sync_pending_commits = []
                            inst_type_ssh = inst_obj.instance_type if inst_obj else 'production'

                            if repo_type == 'custom' and inst_type_ssh != 'production':
                                prod_branch = project.production_branch or 'main'
                                if inst_type_ssh == 'development':
                                    staging_inst = request.env['devops.instance'].sudo().search([
                                        ('project_id', '=', project.id),
                                        ('instance_type', '=', 'staging'),
                                    ], limit=1)
                                    merge_target = staging_inst.git_branch if staging_inst else 'staging'
                                elif inst_type_ssh == 'staging':
                                    merge_target = prod_branch

                                # Count pending merges
                                if merge_target:
                                    ssh_run(f'git -C {git_path} fetch origin 2>/dev/null')
                                    log_out = ssh_run(f'git -C {git_path} log --oneline origin/{merge_target}..{branch} 2>/dev/null')
                                    if log_out:
                                        for l in log_out.strip().split('\n')[:10]:
                                            if l.strip():
                                                parts = l.split(' ', 1)
                                                merge_pending += 1
                                                merge_pending_commits.append({'hash': parts[0], 'message': parts[1] if len(parts) > 1 else ''})

                                # Count sync pending (production → this branch)
                                sync_out = ssh_run(f'git -C {git_path} log --oneline {branch}..origin/{prod_branch} 2>/dev/null')
                                if sync_out:
                                    for l in sync_out.strip().split('\n')[:10]:
                                        if l.strip():
                                            parts = l.split(' ', 1)
                                            sync_pending += 1
                                            sync_pending_commits.append({'hash': parts[0], 'message': parts[1] if len(parts) > 1 else ''})

                            repos.append({
                                'path': git_path, 'name': name, 'branch': branch,
                                'ahead': 0, 'behind': 0, 'remote': '', 'dirty': dirty,
                                'shallow': False, 'owned': True, 'repo_type': repo_type,
                                'merge_target': merge_target, 'merge_pending': merge_pending,
                                'merge_pending_commits': merge_pending_commits,
                                'sync_pending': sync_pending, 'sync_pending_commits': sync_pending_commits,
                            })
                        elif line.startswith('ADDON:'):
                            addon_path = line[6:]
                            if addon_path in seen:
                                continue
                            seen.add(addon_path)
                            name = os.path.basename(addon_path)
                            repos.append({
                                'path': addon_path, 'name': name, 'branch': '',
                                'ahead': 0, 'behind': 0, 'remote': '', 'dirty': False,
                                'shallow': False, 'owned': True, 'repo_type': 'custom',
                                'merge_target': '', 'merge_pending': 0, 'merge_pending_commits': [],
                                'sync_pending': 0, 'sync_pending_commits': [],
                            })

            is_admin = request.env.user.has_group('pmb_devops.group_devops_admin')
            return {'repos': repos, 'is_admin': is_admin}

        # Find config path from instance or project (local)
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
            # Determine repo type early for auto-fix
            is_odoo = os.path.isfile(os.path.join(git_path, 'odoo-bin'))
            is_enterprise = 'enterprise' in git_path.lower()
            is_custom = not is_odoo and not is_enterprise

            # Auto-fix: ensure custom repos in staging/dev are on the correct branch
            if expected_branch and is_custom and branch != expected_branch:
                try:
                    # Try checkout existing branch, or create from current HEAD
                    co = subprocess.run(
                        ['git', 'checkout', expected_branch],
                        capture_output=True, text=True, timeout=10, cwd=git_path,
                    )
                    if co.returncode != 0:
                        subprocess.run(
                            ['git', 'checkout', '-b', expected_branch],
                            capture_output=True, text=True, timeout=10, cwd=git_path,
                        )
                        subprocess.run(
                            ['git', 'push', '-u', 'origin', expected_branch],
                            capture_output=True, text=True, timeout=30, cwd=git_path,
                        )
                    branch = expected_branch
                    _logger.info("Auto-fixed branch for %s -> %s", git_path, expected_branch)
                except Exception:
                    pass

            # Auto-install pre-push hook if missing (branch protection)
            if expected_branch and is_custom:
                hook_path = os.path.join(git_path, '.git', 'hooks', 'pre-push')
                if not os.path.exists(hook_path):
                    try:
                        inst_type = inst_type  # already set above
                        protected = 'main|master' if inst_type == 'staging' else 'main|master|staging'
                        os.makedirs(os.path.dirname(hook_path), exist_ok=True)
                        with open(hook_path, 'w') as hf:
                            hf.write(f'#!/bin/bash\n'
                                     f'while read l ls r rs; do b=$(echo "$r"|sed "s|refs/heads/||"); '
                                     f'if echo "$b"|grep -qE "^({protected})$"; then '
                                     f'echo "ERROR: Push a $b bloqueado desde {inst_type}."; exit 1; fi; done; exit 0\n')
                        os.chmod(hook_path, 0o755)
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
            # Promote: commits ahead of target branch (dev→staging, staging→main)
            # Use instance_type to determine merge direction, not branch name
            merge_target = ''
            merge_pending = 0
            merge_pending_commits = []
            if inst_type == 'development':
                # Dev merges into staging branch — find it from project's staging instances
                staging_inst = request.env['devops.instance'].sudo().search([
                    ('project_id', '=', project.id),
                    ('instance_type', '=', 'staging'),
                ], limit=1)
                merge_target = staging_inst.git_branch if staging_inst else 'staging'
            elif inst_type == 'staging':
                merge_target = project.production_branch or 'main'
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
            # Sync: commits in production branch not yet in this branch
            sync_pending = 0
            sync_pending_commits = []
            prod_branch = project.production_branch or 'main'
            if inst_type in ('staging', 'development'):
                try:
                    r6 = subprocess.run(
                        ['git', 'log', '--oneline', f'{branch}..origin/{prod_branch}'],
                        capture_output=True, text=True, timeout=5, cwd=git_path,
                    )
                    if r6.returncode == 0 and r6.stdout.strip():
                        lines = [l for l in r6.stdout.strip().split('\n') if l.strip()]
                        sync_pending = len(lines)
                        for l in lines[:10]:
                            parts = l.split(' ', 1)
                            sync_pending_commits.append({
                                'hash': parts[0],
                                'message': parts[1] if len(parts) > 1 else '',
                            })
                except Exception:
                    pass
            repo_type = 'custom'
            if is_odoo:
                repo_type = 'odoo'
            elif is_enterprise:
                repo_type = 'enterprise'
            repos.append({
                'path': git_path, 'name': os.path.basename(git_path),
                'branch': branch, 'ahead': ahead, 'behind': behind,
                'remote': remote, 'dirty': dirty, 'shallow': shallow,
                'owned': True, 'repo_type': repo_type,
                'merge_target': merge_target, 'merge_pending': merge_pending,
                'merge_pending_commits': merge_pending_commits,
                'sync_pending': sync_pending, 'sync_pending_commits': sync_pending_commits,
            })

        # For non-production instances, only show repos inside the instance's own path
        inst_path = None
        inst_type = 'production'
        expected_branch = ''
        if instance_id:
            inst = request.env['devops.instance'].browse(instance_id)
            if inst.exists():
                inst_path = inst.instance_path
                inst_type = inst.instance_type or 'production'
                if inst_type != 'production':
                    expected_branch = inst.git_branch or ''

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

        # For dev/staging: filter repos from OTHER instances, keep own + shared
        if inst_path and inst_type != 'production':
            instances_base = os.path.dirname(inst_path)
            repos = [r for r in repos
                     if r['path'].startswith(inst_path)
                     or not r['path'].startswith(instances_base + '/')]
        # Classify each repo dynamically: 'odoo', 'enterprise', or 'custom'
        enterprise_path = project.enterprise_path or ''
        for r in repos:
            r['owned'] = r['path'].startswith(inst_path) if inst_path else True
            # Detect Odoo source: contains odoo-bin
            if os.path.isfile(os.path.join(r['path'], 'odoo-bin')):
                r['repo_type'] = 'odoo'
            # Detect Enterprise: matches project enterprise_path or parent of it
            elif enterprise_path and (
                r['path'] == enterprise_path
                or enterprise_path.startswith(r['path'] + '/')
                or r['path'].endswith('/enterprise')
            ):
                r['repo_type'] = 'enterprise'
            else:
                r['repo_type'] = 'custom'

        # Fallback: project.repo_path (only if not already found)
        if not repos and project.repo_path and project.repo_path not in seen:
            if os.path.isdir(os.path.join(project.repo_path, '.git')):
                repos.append({'path': project.repo_path, 'name': os.path.basename(project.repo_path), 'branch': 'HEAD', 'owned': True, 'repo_type': 'custom'})

        # Enforce .gitignore on all discovered repos (idempotent, SSH-aware)
        from ..utils.git_utils import ensure_gitignore
        for repo in repos:
            try:
                ensure_gitignore(repo['path'], project=project)
            except Exception:
                pass

        is_admin = request.env.user.has_group('pmb_devops.group_devops_admin')
        return {'repos': repos, 'is_admin': is_admin}

    @http.route('/devops/repo/fetch_deeper', type='json', auth='user')
    def repo_fetch_deeper(self, repo_path, count=50):
        """Deepen a shallow clone by N commits."""
        is_ssh = project.connection_type == 'ssh' and project.ssh_host
        if not repo_path or (not is_ssh and not os.path.isdir(repo_path)):
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

        is_ssh = project.connection_type == 'ssh' and project.ssh_host
        if not repo_path:
            return {'commits': []}
        if not is_ssh and not os.path.isdir(repo_path):
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
        is_ssh = project.connection_type == 'ssh' and project.ssh_host
        if not cwd:
            return {'error': 'Repo path not found'}
        if not is_ssh and not os.path.isdir(cwd):
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

    def _get_editor_allowed_paths(self, project, instance_id=None):
        """Return the instance root + any external addons paths (SSH-aware)."""

        # Determine instance root and service name
        inst_root = ''
        service_name = ''
        if instance_id:
            inst = project.env['devops.instance'].browse(instance_id)
            if inst.exists():
                inst_root = inst.instance_path or ''
                service_name = inst.service_name or ''
        if not inst_root:
            if project.production_instance_id and project.production_instance_id.instance_path:
                inst_root = project.production_instance_id.instance_path
            elif project.repo_path:
                inst_root = os.path.dirname(project.repo_path)
        if not service_name:
            service_name = project.odoo_service_name or ''

        # Use autodetect to get addons_path (works for both local and SSH)
        addons_paths = []
        if service_name:
            try:
                detected = self.project_autodetect(service_name, project.id)
                ap = detected.get('addons_path', '')
                if ap:
                    addons_paths = [p.strip() for p in ap.split(',') if p.strip()]
            except Exception:
                pass

        # For local projects, verify paths exist
        is_ssh = project.connection_type == 'ssh' and project.ssh_host
        if not is_ssh:
            addons_paths = [p for p in addons_paths if os.path.isdir(p)]

        # Build allowed paths: instance root + external addons paths
        allowed = []
        if inst_root:
            if is_ssh or os.path.isdir(inst_root):
                allowed.append(inst_root)
        for p in addons_paths:
            if inst_root and p.startswith(inst_root + '/'):
                continue
            if p not in allowed:
                if is_ssh or os.path.isdir(p):
                    allowed.append(p)
        return allowed

    @http.route('/devops/files/list', type='json', auth='user')
    def files_list(self, project_id, instance_id=None, path='', repo='addons'):
        """List files and directories from addons_path entries."""
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}

        allowed_paths = self._get_editor_allowed_paths(project, instance_id)
        external_paths = []

        from ..utils import ssh_utils

        # Root listing: list instance dir contents + external paths at same level
        if not path:
            inst_root = allowed_paths[0] if allowed_paths else ''
            external_paths = allowed_paths[1:] if len(allowed_paths) > 1 else []
            if inst_root:
                # List instance dir contents
                path = inst_root
                # Will be listed below; add external paths as extra items after
            else:
                return {'items': [], 'current_path': ''}

        # Sub-directory: path is absolute (from root click) or relative within a base
        full_path = path
        if not os.path.isabs(path):
            return {'error': 'Ruta inválida'}

        # Security: must be inside one of the allowed paths
        full_path = os.path.normpath(full_path)
        if not any(full_path == ap or full_path.startswith(ap + '/') for ap in allowed_paths):
            return {'error': 'Acceso denegado'}

        result = ssh_utils.execute_command(project, [
            'find', full_path, '-maxdepth', '1', '-mindepth', '1', '-printf', '%y|||%f|||%s|||%T@\n',
        ], cwd=full_path, timeout=10)

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
                    'path': os.path.join(full_path, name),
                    'absolute': True,
                })

        # Add external shared paths at root level (only for root listing)
        if external_paths:
            existing_names = {i['name'] for i in items}
            for ep in external_paths:
                name = os.path.basename(ep)
                if name not in existing_names:
                    items.append({
                        'type': 'dir', 'name': name, 'size': 0,
                        'path': ep, 'absolute': True,
                    })

        # Sort: dirs first, then files, alphabetical
        items.sort(key=lambda x: (0 if x['type'] == 'dir' else 1, x['name'].lower()))
        return {'items': items, 'current_path': path}

    @http.route('/devops/files/read', type='json', auth='user')
    def files_read(self, project_id, instance_id=None, path='', repo='addons'):
        """Read file content."""
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}

        allowed_paths = self._get_editor_allowed_paths(project, instance_id)

        from ..utils import ssh_utils

        full_path = os.path.normpath(path)
        if not any(full_path == ap or full_path.startswith(ap + '/') for ap in allowed_paths):
            return {'error': 'Acceso denegado'}

        result = ssh_utils.execute_command(project, [
            'head', '-c', '500000', full_path,
        ], timeout=10)

        if result.returncode != 0:
            return {'error': result.stderr or 'Error leyendo archivo'}

        return {'content': result.stdout, 'path': path, 'name': os.path.basename(path)}

    @http.route('/devops/git/status', type='json', auth='user')
    def git_status(self, project_id, instance_id=None, repo_path=''):
        """Get git status: staged, unstaged, and untracked files."""
        self._touch_activity(instance_id)
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

        is_ssh = project.connection_type == 'ssh' and project.ssh_host
        if not repo_path or (not is_ssh and not os.path.isdir(repo_path)):
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

    @http.route('/devops/git/diff', type='json', auth='user')
    def git_diff(self, project_id, repo_path='', file_path='', staged=False):
        """Get diff for a specific file (staged or unstaged)."""
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}
        is_ssh = project.connection_type == 'ssh' and project.ssh_host
        if not repo_path or (not is_ssh and not os.path.isdir(repo_path)):
            return {'error': 'Repo path not found'}
        if not file_path:
            return {'error': 'File path required'}
        from ..utils import ssh_utils
        cmd = ['git', 'diff']
        if staged:
            cmd.append('--cached')
        cmd.extend(['--', file_path])
        result = ssh_utils.execute_command(project, cmd, cwd=repo_path, timeout=10)
        if result.returncode != 0:
            return {'error': result.stderr or 'diff failed'}
        return {'diff': result.stdout, 'file': file_path, 'staged': staged}

    # ---- Git Auth helpers ----

    def _is_git_authed(self):
        """Check if current user is admin or has git-authed this session."""
        if request.env.user.has_group('pmb_devops.group_devops_admin'):
            return True
        return request.session.get('pmb_git_authed') == request.env.uid

    @http.route('/devops/git/auth/check', type='json', auth='user')
    def git_auth_check(self):
        """Check if current user needs git auth."""
        is_admin = request.env.user.has_group('pmb_devops.group_devops_admin')
        is_developer = request.env.user.has_group('pmb_devops.group_devops_developer')
        return {
            'is_admin': is_admin,
            'is_developer': is_developer,
            'authenticated': is_admin or request.session.get('pmb_git_authed') == request.env.uid,
            'user_name': request.env.user.name,
            'user_email': request.env.user.email or request.env.user.login,
        }

    @http.route('/devops/git/auth', type='json', auth='user')
    def git_auth(self, login='', password=''):
        """Authenticate user with Odoo credentials for git operations."""
        if not login or not password:
            return {'error': 'Login y contraseña requeridos'}
        user = request.env['res.users'].sudo().search([('login', '=', login)], limit=1)
        if not user:
            return {'error': 'Usuario no encontrado'}
        try:
            user._check_credentials({'type': 'password', 'password': password}, {'interactive': True})
            request.session['pmb_git_authed'] = request.env.uid
            return {
                'status': 'ok',
                'user_name': user.name,
                'user_email': user.email or user.login,
            }
        except Exception:
            return {'error': 'Contraseña incorrecta'}

    # ---- GitHub credentials per instance ----

    @http.route('/devops/git/github/check', type='json', auth='user')
    def github_check(self, instance_id=None):
        """Check if GitHub credentials are configured for this instance."""
        if not instance_id:
            return {'configured': False}
        inst = request.env['devops.instance'].sudo().browse(instance_id)
        if not inst.exists():
            return {'configured': False}
        return {
            'configured': bool(inst.github_user and inst.github_token),
            'github_user': inst.github_user or '',
        }

    @http.route('/devops/git/github/save', type='json', auth='user')
    def github_save(self, instance_id=None, github_user='', github_token=''):
        """Save GitHub credentials for an instance and configure git remotes."""
        if not instance_id or not github_user or not github_token:
            return {'error': 'Usuario y token de GitHub requeridos'}
        inst = request.env['devops.instance'].sudo().browse(instance_id)
        if not inst.exists():
            return {'error': 'Instancia no encontrada'}
        if inst.instance_type == 'production':
            return {'error': 'No se pueden configurar credenciales en produccion'}

        inst.write({
            'github_user': github_user,
            'github_token': github_token,
        })

        # Configure git remote URLs with credentials in all custom repos
        project = inst.project_id
        from ..utils import ssh_utils
        repos_data = self.instance_repos(project.id, instance_id)
        for repo in repos_data.get('repos', []):
            if repo.get('repo_type') != 'custom':
                continue
            rpath = repo['path']
            # Get current remote URL
            r = ssh_utils.execute_command(project, ['git', 'remote', 'get-url', 'origin'], cwd=rpath, timeout=10)
            if r.returncode != 0:
                continue
            url = r.stdout.strip()
            # Replace/add credentials in URL: https://user:token@github.com/...
            import re
            if url.startswith('https://'):
                # Remove existing credentials
                clean = re.sub(r'https://[^@]+@', 'https://', url)
                new_url = clean.replace('https://', f'https://{github_user}:{github_token}@')
                ssh_utils.execute_command(project, ['git', 'remote', 'set-url', 'origin', new_url], cwd=rpath, timeout=10)

        return {'status': 'ok'}

    @http.route('/devops/git/github/oauth/start', type='json', auth='user')
    def github_oauth_start(self, instance_id=None):
        """Start GitHub OAuth flow. Returns the authorization URL."""
        if not instance_id:
            return {'error': 'Instance ID required'}
        inst = request.env['devops.instance'].sudo().browse(instance_id)
        if not inst.exists():
            return {'error': 'Instancia no encontrada'}
        project = inst.project_id
        client_id = project.github_client_id
        if not client_id:
            return {'error': 'GitHub OAuth no configurado. Configura Client ID en Settings del proyecto.'}
        # State = instance_id:uid for CSRF protection
        import hashlib
        state = hashlib.sha256(f'{instance_id}:{request.env.uid}:{client_id}'.encode()).hexdigest()[:16]
        state = f'{instance_id}_{state}'
        # Store state in session
        request.session[f'github_oauth_state_{instance_id}'] = state
        auth_url = (
            f'https://github.com/login/oauth/authorize'
            f'?client_id={client_id}'
            f'&scope=repo'
            f'&state={state}'
            f'&redirect_uri=https://{request.httprequest.host}/devops/git/github/oauth/callback'
        )
        return {'auth_url': auth_url}

    @http.route('/devops/git/github/oauth/callback', type='http', auth='user', csrf=False)
    def github_oauth_callback(self, code=None, state=None, **kw):
        """GitHub OAuth callback — exchange code for token."""
        import requests as http_requests
        if not code or not state:
            return request.redirect('/web#action=780&error=oauth_missing_params')

        # Parse instance_id from state
        instance_id = int(state.split('_')[0]) if '_' in state else 0
        if not instance_id:
            return request.redirect('/web#action=780&error=oauth_invalid_state')

        inst = request.env['devops.instance'].sudo().browse(instance_id)
        if not inst.exists():
            return request.redirect('/web#action=780&error=oauth_instance_not_found')

        project = inst.project_id
        client_id = project.github_client_id
        client_secret = project.github_client_secret
        if not client_id or not client_secret:
            return request.redirect('/web#action=780&error=oauth_not_configured')

        # Exchange code for token
        try:
            resp = http_requests.post(
                'https://github.com/login/oauth/access_token',
                data={
                    'client_id': client_id,
                    'client_secret': client_secret,
                    'code': code,
                },
                headers={'Accept': 'application/json'},
                timeout=15,
            )
            data = resp.json()
            token = data.get('access_token', '')
            if not token:
                _logger.warning("GitHub OAuth: no token in response: %s", data)
                return request.redirect('/web#action=780&error=oauth_no_token')
        except Exception as e:
            _logger.warning("GitHub OAuth token exchange failed: %s", e)
            return request.redirect('/web#action=780&error=oauth_exchange_failed')

        # Get GitHub username
        github_user = ''
        try:
            user_resp = http_requests.get(
                'https://api.github.com/user',
                headers={'Authorization': f'token {token}', 'Accept': 'application/json'},
                timeout=10,
            )
            github_user = user_resp.json().get('login', '')
        except Exception:
            pass

        # Save credentials
        inst.write({
            'github_user': github_user or 'oauth-user',
            'github_token': token,
        })

        # Configure git remotes with the token
        from ..utils import ssh_utils
        repos_data = self.instance_repos(project.id, instance_id)
        for repo in repos_data.get('repos', []):
            if repo.get('repo_type') != 'custom':
                continue
            rpath = repo['path']
            r = ssh_utils.execute_command(project, ['git', 'remote', 'get-url', 'origin'], cwd=rpath, timeout=10)
            if r.returncode != 0:
                continue
            url = r.stdout.strip()
            import re
            if url.startswith('https://'):
                clean = re.sub(r'https://[^@]+@', 'https://', url)
                new_url = clean.replace('https://', f'https://{github_user or "oauth"}:{token}@')
                ssh_utils.execute_command(project, ['git', 'remote', 'set-url', 'origin', new_url], cwd=rpath, timeout=10)

        _logger.info("GitHub OAuth: token saved for instance %s, user %s", inst.name, github_user)
        return request.redirect('/web#action=780')

    @http.route('/devops/git/github/logout', type='json', auth='user')
    def github_logout(self, instance_id=None):
        """Remove GitHub credentials and clean remote URLs."""
        if not instance_id:
            return {'error': 'Instance ID required'}
        inst = request.env['devops.instance'].sudo().browse(instance_id)
        if not inst.exists():
            return {'error': 'Instancia no encontrada'}

        # Clean credentials from git remote URLs
        project = inst.project_id
        from ..utils import ssh_utils
        import re
        repos_data = self.instance_repos(project.id, instance_id)
        for repo in repos_data.get('repos', []):
            if repo.get('repo_type') != 'custom':
                continue
            rpath = repo['path']
            r = ssh_utils.execute_command(project, ['git', 'remote', 'get-url', 'origin'], cwd=rpath, timeout=10)
            if r.returncode != 0:
                continue
            url = r.stdout.strip()
            if '@' in url and url.startswith('https://'):
                clean_url = re.sub(r'https://[^@]+@', 'https://', url)
                ssh_utils.execute_command(project, ['git', 'remote', 'set-url', 'origin', clean_url], cwd=rpath, timeout=10)

        inst.write({'github_user': False, 'github_token': False})
        return {'status': 'ok'}

    # ---- Claude sessions ----

    def _get_claude_project_dir(self, instance_id=None):
        """Get the Claude project directory for an instance.

        For local: reads from ~/.claude/projects/<slug>/
        For SSH: reads from remote server via SSH.
        Returns (project_dir, is_ssh, project) tuple.
        """
        home = os.path.expanduser('~')
        if not instance_id:
            return '', False, None
        inst = request.env['devops.instance'].browse(instance_id)
        if not inst.exists():
            return '', False, None

        project = inst.project_id
        is_ssh = project.connection_type == 'ssh' and project.ssh_host

        if is_ssh:
            # For SSH, sessions are on the remote server
            # Build candidate dirs: instance path + production/repo path
            ssh_user = project.ssh_user or 'root'
            remote_home = '/root' if ssh_user == 'root' else f'/home/{ssh_user}'
            candidates = []
            if inst.instance_path:
                slug = inst.instance_path.replace('/', '-').replace('_', '-')
                candidates.append(f'{remote_home}/.claude/projects/{slug}')
            if project.repo_path:
                slug = project.repo_path.replace('/', '-').replace('_', '-')
                candidates.append(f'{remote_home}/.claude/projects/{slug}')
            remote_dir = ','.join(candidates) if candidates else ''
            return remote_dir, True, project

        # Local
        cwd = home
        if inst.instance_type == 'production':
            repo = inst.project_id.repo_path
            if repo and os.path.isdir(repo):
                cwd = repo
            elif inst.instance_path and os.path.isdir(inst.instance_path):
                cwd = inst.instance_path
        elif inst.instance_path and os.path.isdir(inst.instance_path):
            cwd = inst.instance_path
        elif inst.project_id.repo_path and os.path.isdir(inst.project_id.repo_path):
            cwd = inst.project_id.repo_path

        if cwd == home and inst.instance_path:
            cwd = inst.instance_path

        slug = cwd.replace('/', '-').replace('_', '-')
        project_dir = os.path.join(home, '.claude', 'projects', slug)
        if not os.path.isdir(project_dir):
            slug_alt = cwd.replace('/', '-')
            project_dir_alt = os.path.join(home, '.claude', 'projects', slug_alt)
            if os.path.isdir(project_dir_alt):
                return project_dir_alt, False, project
        return project_dir, False, project

    @http.route('/devops/claude/sessions', type='json', auth='user')
    def claude_sessions(self, instance_id=None, search=''):
        """List Claude Code sessions for an instance (local or SSH)."""
        import json as json_mod
        project_dir, is_ssh, project = self._get_claude_project_dir(instance_id)
        if not project_dir:
            return {'sessions': []}

        if is_ssh and project:
            return self._claude_sessions_ssh(project, project_dir, search)

        # Local
        import glob
        if not os.path.isdir(project_dir):
            return {'sessions': []}

        sessions = []
        for f in sorted(glob.glob(os.path.join(project_dir, '*.jsonl')), key=os.path.getmtime, reverse=True):
            sid = os.path.basename(f).replace('.jsonl', '')
            try:
                summary = ''
                ts = ''
                with open(f, 'r') as fh:
                    for line in fh:
                        d = json_mod.loads(line)
                        if not ts and 'timestamp' in d:
                            ts = d['timestamp']
                        if d.get('type') == 'summary':
                            summary = (d.get('summary') or '')[:120]
                        if not summary and d.get('type') == 'user':
                            content = d.get('message', {}).get('content', '')
                            if isinstance(content, list):
                                for c in content:
                                    if isinstance(c, dict) and c.get('type') == 'text':
                                        summary = c['text'][:120]
                                        break
                            elif isinstance(content, str):
                                summary = content[:120]
                size = os.path.getsize(f)
                if search and search.lower() not in summary.lower() and search.lower() not in sid.lower():
                    continue
                sessions.append({
                    'id': sid,
                    'timestamp': ts,
                    'summary': summary,
                    'size': size,
                })
            except Exception:
                continue

        return {'sessions': sessions[:50]}

    def _claude_sessions_ssh(self, project, remote_dirs_csv, search=''):
        """List Claude sessions from a remote SSH server."""
        dirs = [d.strip() for d in remote_dirs_csv.split(',') if d.strip()]
        dir_list = ' '.join(f'"{d}"' for d in dirs)
        # Simple script: list files with timestamp from filename/mtime, no python3 needed
        script = (
            f'for DIR in {dir_list}; do '
            f'  [ -d "$DIR" ] || continue; '
            f'  for f in $(ls -t "$DIR"/*.jsonl 2>/dev/null | head -50); do '
            f'    SID=$(basename "$f" .jsonl); '
            f'    SIZE=$(stat -c%s "$f" 2>/dev/null || echo 0); '
            f'    MTIME=$(stat -c%Y "$f" 2>/dev/null || echo 0); '
            f'    SUMMARY=$(grep -m1 "content" "$f" 2>/dev/null | head -c 150 || echo ""); '
            f'    echo "$SID||$MTIME||$SUMMARY||$SIZE"; '
            f'  done; '
            f'done'
        )
        raw = self._cmd_on_project(project, script)
        if not raw or raw == 'NODIR':
            return {'sessions': []}

        sessions = []
        seen_ids = set()
        for line in raw.split('\n'):
            line = line.strip()
            if not line or '||' not in line:
                continue
            parts = line.split('||', 3)
            if len(parts) < 4:
                continue
            sid, ts, summary, size_str = parts
            if sid in seen_ids:
                continue
            seen_ids.add(sid)
            if search and search.lower() not in summary.lower() and search.lower() not in sid.lower():
                continue
            sessions.append({
                'id': sid,
                'timestamp': ts,
                'summary': summary,
                'size': int(size_str) if size_str.isdigit() else 0,
            })
        return {'sessions': sessions}

    @http.route('/devops/claude/sessions/delete', type='json', auth='user')
    def claude_session_delete(self, instance_id=None, session_id=''):
        """Delete a Claude Code session file (local or SSH)."""
        if not session_id:
            return {'error': 'Session ID required'}
        project_dir, is_ssh, project = self._get_claude_project_dir(instance_id)
        if not project_dir:
            return {'error': 'Project dir not found'}

        if is_ssh and project:
            filepath = f'{project_dir}/{session_id}.jsonl'
            self._cmd_on_project(project, f'rm -f {filepath}')
            return {'status': 'ok'}

        # Local
        filepath = os.path.join(project_dir, f'{session_id}.jsonl')
        if not os.path.isfile(filepath):
            return {'error': 'Session not found'}
        if '.claude/projects/' not in filepath:
            return {'error': 'Invalid path'}
        os.remove(filepath)
        return {'status': 'ok'}

    # ---- Meetings ----

    @http.route('/devops/meetings/list', type='json', auth='user')
    def meetings_list(self, project_id):
        """List meetings for a project."""
        meetings = request.env['devops.meeting'].sudo().search([
            ('project_id', '=', project_id),
        ], order='date desc', limit=50)
        return {'meetings': [{
            'id': m.id,
            'name': m.name,
            'date': m.date.isoformat() if m.date else '',
            'meet_url': m.meet_url or '',
            'meet_type': m.meet_type or 'jitsi',
            'jitsi_room': m.jitsi_room or '',
            'state': m.state,
            'user': m.user_id.name,
            'notes': m.notes or '',
            'has_transcription': bool(m.transcription),
            'has_audio': bool(m.audio_file),
            'duration': m.duration_minutes or 0,
            'recordings': [{
                'id': r.id,
                'name': r.name,
                'duration': r.duration_minutes or 0,
                'has_transcription': bool(r.transcription),
                'state': r.state,
            } for r in m.recording_ids],
            'recording_count': len(m.recording_ids),
            'task_count': len(m.task_ids),
        } for m in meetings]}

    @http.route('/devops/meetings/create', type='json', auth='user')
    def meetings_create(self, project_id, name='', meet_url='', meet_type='jitsi', instance_id=None):
        """Create a new meeting."""
        if not name:
            return {'error': 'Nombre requerido'}
        import uuid
        jitsi_room = ''
        if meet_type == 'jitsi':
            # Generate unique room name
            project = request.env['devops.project'].browse(project_id)
            slug = (project.name or 'pmb').replace(' ', '').lower()
            jitsi_room = f"pmb-{slug}-{uuid.uuid4().hex[:8]}"
            meet_url = f"https://meet.jit.si/{jitsi_room}"
        vals = {
            'name': name,
            'project_id': project_id,
            'meet_type': meet_type,
            'meet_url': meet_url,
            'jitsi_room': jitsi_room,
            'state': 'scheduled',
        }
        if instance_id:
            vals['instance_id'] = instance_id
        meeting = request.env['devops.meeting'].create(vals)
        return {'status': 'ok', 'id': meeting.id, 'meet_url': meeting.meet_url, 'jitsi_room': meeting.jitsi_room}

    @http.route('/devops/meetings/update', type='json', auth='user')
    def meetings_update(self, meeting_id, notes=None, state=None, meet_url=None, duration=None):
        """Update meeting notes, state, url."""
        meeting = request.env['devops.meeting'].browse(meeting_id)
        if not meeting.exists():
            return {'error': 'Reunion no encontrada'}
        vals = {}
        if notes is not None:
            vals['notes'] = notes
        if state:
            vals['state'] = state
        if meet_url:
            vals['meet_url'] = meet_url
        if duration is not None:
            vals['duration_minutes'] = duration
        if vals:
            meeting.write(vals)
        return {'status': 'ok'}

    @http.route('/devops/meetings/transcription', type='json', auth='user')
    def meetings_get_transcription(self, meeting_id):
        """Get meeting transcription."""
        meeting = request.env['devops.meeting'].browse(meeting_id)
        if not meeting.exists():
            return {'error': 'Reunion no encontrada'}
        return {'transcription': meeting.transcription or '', 'notes': meeting.notes or ''}

    @http.route('/devops/meetings/upload_audio', type='json', auth='user')
    def meetings_upload_audio(self, meeting_id, audio_data='', filename='', duration=0):
        """Upload audio file as a new recording for a meeting."""
        meeting = request.env['devops.meeting'].browse(meeting_id)
        if not meeting.exists():
            return {'error': 'Reunion no encontrada'}
        if not audio_data:
            return {'error': 'Audio requerido'}
        meeting.sudo().write({
            'audio_file': audio_data,
            'audio_filename': filename or 'recording.webm',
        })
        rec = request.env['devops.meeting.recording'].sudo().create({
            'meeting_id': meeting_id,
            'name': filename or f'Grabacion {fields.Datetime.now()}',
            'audio_file': audio_data,
            'audio_filename': filename or 'recording.webm',
            'duration_minutes': duration or 0,
        })
        return {'status': 'ok', 'recording_id': rec.id}

    @http.route('/devops/meetings/upload_chunk', type='json', auth='user')
    def meetings_upload_chunk(self, meeting_id, recording_id=None, chunk_data='', chunk_index=0, is_last=False, filename='', duration=0):
        """Upload audio in chunks. Creates recording on first chunk, appends on subsequent."""
        import base64
        meeting = request.env['devops.meeting'].sudo().browse(meeting_id)
        if not meeting.exists():
            return {'error': 'Reunion no encontrada'}

        if not recording_id:
            # First chunk — create the recording
            rec = request.env['devops.meeting.recording'].sudo().create({
                'meeting_id': meeting_id,
                'name': filename or f'Grabacion {fields.Datetime.now()}',
                'audio_file': chunk_data,
                'audio_filename': filename or 'recording.webm',
            })
            recording_id = rec.id
        else:
            # Append chunk to existing recording
            rec = request.env['devops.meeting.recording'].sudo().browse(recording_id)
            if rec.exists() and rec.audio_file:
                existing = base64.b64decode(rec.audio_file)
                new_chunk = base64.b64decode(chunk_data)
                combined = base64.b64encode(existing + new_chunk).decode()
                rec.write({'audio_file': combined})
            elif rec.exists():
                rec.write({'audio_file': chunk_data})

        if is_last:
            rec = request.env['devops.meeting.recording'].sudo().browse(recording_id)
            rec.write({'duration_minutes': duration or 0})
            # Also update meeting level
            meeting.write({
                'audio_file': rec.audio_file,
                'audio_filename': rec.audio_filename,
            })

        return {'status': 'ok', 'recording_id': recording_id}

    @http.route('/devops/meetings/transcribe', type='json', auth='user')
    def meetings_transcribe(self, meeting_id):
        """Transcribe meeting audio using Groq Whisper API."""
        meeting = request.env['devops.meeting'].sudo().browse(meeting_id)
        if not meeting.exists():
            return {'error': 'Reunion no encontrada'}
        if not meeting.audio_file:
            return {'error': 'No hay audio para transcribir'}

        groq_key = request.env['ir.config_parameter'].sudo().get_param('pmb_devops.groq_api_key', '')
        if not groq_key:
            return {'error': 'API key de Groq no configurada. Ve a Settings del proyecto.'}

        import base64
        import tempfile
        import requests as http_requests

        # Save audio to temp file
        audio_bytes = base64.b64decode(meeting.audio_file)
        ext = (meeting.audio_filename or 'audio.webm').split('.')[-1]
        with tempfile.NamedTemporaryFile(suffix=f'.{ext}', delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        try:
            # Call Groq Whisper API
            resp = http_requests.post(
                'https://api.groq.com/openai/v1/audio/transcriptions',
                headers={'Authorization': f'Bearer {groq_key}'},
                files={'file': (meeting.audio_filename or 'audio.webm', open(tmp_path, 'rb'))},
                data={'model': 'whisper-large-v3', 'language': 'es'},
                timeout=120,
            )
            if resp.status_code == 200:
                result = resp.json()
                transcription = result.get('text', '')
                meeting.write({
                    'transcription': transcription,
                    'state': 'transcribed',
                })
                return {'status': 'ok', 'transcription': transcription}
            else:
                return {'error': f'Groq API error {resp.status_code}: {resp.text[:200]}'}
        except Exception as e:
            return {'error': str(e)}
        finally:
            os.unlink(tmp_path)

    @http.route('/devops/meetings/transcribe_all', type='json', auth='user')
    def meetings_transcribe_all(self, meeting_id, force=False):
        """Transcribe ALL recordings of a meeting using Groq Whisper API."""
        meeting = request.env['devops.meeting'].sudo().browse(meeting_id)
        if not meeting.exists():
            return {'error': 'Reunion no encontrada'}

        recordings = meeting.recording_ids.filtered(lambda r: r.audio_file)
        # Also include legacy audio_file on the meeting itself
        if not recordings and not meeting.audio_file:
            return {'error': 'No hay grabaciones para transcribir'}

        groq_key = request.env['ir.config_parameter'].sudo().get_param('pmb_devops.groq_api_key', '')
        if not groq_key:
            return {'error': 'API key de Groq no configurada. Ve a Settings del proyecto.'}

        import base64, tempfile
        import requests as http_requests

        all_transcriptions = []
        errors = []

        # Transcribe each recording
        for rec in recordings:
            if rec.transcription and not force:
                all_transcriptions.append(f"[Grabacion: {rec.name}]\n{rec.transcription}")
                continue
            try:
                audio_bytes = base64.b64decode(rec.audio_file)
                ext = (rec.audio_filename or 'audio.webm').split('.')[-1]
                with tempfile.NamedTemporaryFile(suffix=f'.{ext}', delete=False) as tmp:
                    tmp.write(audio_bytes)
                    tmp_path = tmp.name
                resp = http_requests.post(
                    'https://api.groq.com/openai/v1/audio/transcriptions',
                    headers={'Authorization': f'Bearer {groq_key}'},
                    files={'file': (rec.audio_filename or 'audio.webm', open(tmp_path, 'rb'))},
                    data={'model': 'whisper-large-v3', 'language': 'es'},
                    timeout=120,
                )
                os.unlink(tmp_path)
                if resp.status_code == 200:
                    text = resp.json().get('text', '')
                    rec.write({'transcription': text, 'state': 'transcribed'})
                    all_transcriptions.append(f"[Grabacion: {rec.name}]\n{text}")
                else:
                    errors.append(f"{rec.name}: Groq error {resp.status_code}")
            except Exception as e:
                errors.append(f"{rec.name}: {e}")

        # Also handle legacy audio_file on meeting itself
        if meeting.audio_file and not meeting.transcription:
            try:
                audio_bytes = base64.b64decode(meeting.audio_file)
                ext = (meeting.audio_filename or 'audio.webm').split('.')[-1]
                with tempfile.NamedTemporaryFile(suffix=f'.{ext}', delete=False) as tmp:
                    tmp.write(audio_bytes)
                    tmp_path = tmp.name
                resp = http_requests.post(
                    'https://api.groq.com/openai/v1/audio/transcriptions',
                    headers={'Authorization': f'Bearer {groq_key}'},
                    files={'file': (meeting.audio_filename or 'audio.webm', open(tmp_path, 'rb'))},
                    data={'model': 'whisper-large-v3', 'language': 'es'},
                    timeout=120,
                )
                os.unlink(tmp_path)
                if resp.status_code == 200:
                    text = resp.json().get('text', '')
                    meeting.write({'transcription': text, 'state': 'transcribed'})
                    all_transcriptions.append(f"[Audio principal]\n{text}")
            except Exception as e:
                errors.append(f"Audio principal: {e}")
        elif meeting.transcription:
            all_transcriptions.append(f"[Audio principal]\n{meeting.transcription}")

        # Combine all transcriptions
        full_transcription = '\n\n'.join(all_transcriptions)
        meeting.write({
            'transcription': full_transcription,
            'state': 'transcribed',
        })

        result = {'status': 'ok', 'transcription': full_transcription, 'count': len(all_transcriptions)}
        if errors:
            result['warnings'] = errors
        return result

    @http.route('/devops/meetings/analyze', type='json', auth='user')
    def meetings_analyze(self, meeting_id):
        """Analyze transcription with Claude CLI to extract tasks."""
        import subprocess, json as json_mod
        meeting = request.env['devops.meeting'].sudo().browse(meeting_id)
        if not meeting.exists():
            return {'error': 'Reunion no encontrada'}
        text = meeting.transcription or meeting.notes or ''
        if not text.strip():
            return {'error': 'No hay transcripcion ni notas para analizar'}

        prompt = f"""Analiza esta transcripcion/notas de una reunion de desarrollo de software y extrae las tareas accionables.

Responde SOLO con un JSON array, sin texto adicional ni markdown. Cada tarea:
- "name": titulo corto (max 80 chars)
- "description": descripcion detallada
- "priority": "0" (normal), "1" (urgente)
- "tag": categoria (bug, feature, refactor, docs, deploy, test)

Texto:
{text[:4000]}"""

        try:
            import requests as http_req
            content = ''
            # Try API key first (direct REST call)
            api_key = request.env['ir.config_parameter'].sudo().get_param('pmb_devops.claude_api_key', '')
            if api_key and not api_key.startswith('sk-ant-oat'):
                resp = http_req.post('https://api.anthropic.com/v1/messages',
                    headers={'anthropic-version': '2023-06-01', 'content-type': 'application/json', 'x-api-key': api_key},
                    json={'model': 'claude-haiku-4-5-20251001', 'max_tokens': 2000,
                          'messages': [{'role': 'user', 'content': prompt}]}, timeout=60)
                if resp.status_code == 200:
                    content = resp.json().get('content', [{}])[0].get('text', '[]')

            # Fallback: Claude CLI wrapper
            if not content:
                import tempfile
                with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as tmp:
                    tmp.write(prompt)
                    prompt_file = tmp.name
                try:
                    script = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'utils', 'claude_analyze.sh')
                    result = subprocess.run(['/bin/bash', script, prompt_file],
                        capture_output=True, text=True, timeout=60)
                    if result.returncode == 0 and result.stdout.strip():
                        content = result.stdout.strip()
                finally:
                    os.unlink(prompt_file)

            if not content:
                return {'error': 'No se pudo conectar con Claude. Configura una API key valida (sk-ant-api...) en Settings.'}
            start = content.find('[')
            end = content.rfind(']') + 1
            if start >= 0 and end > start:
                tasks = json_mod.loads(content[start:end])
            else:
                tasks = []
            return {'tasks': tasks}
        except subprocess.TimeoutExpired:
            return {'error': 'Claude CLI timeout (60s)'}
        except Exception as e:
            return {'error': str(e)}

    @http.route('/devops/meetings/create_tasks', type='json', auth='user')
    def meetings_create_tasks(self, meeting_id, tasks=None):
        """Create Odoo project.task records from analyzed tasks."""
        if not tasks:
            return {'error': 'No tasks provided'}
        meeting = request.env['devops.meeting'].sudo().browse(meeting_id)
        if not meeting.exists():
            return {'error': 'Reunion no encontrada'}

        project = meeting.project_id
        odoo_project = project.odoo_project_id
        if not odoo_project:
            # Auto-create Odoo project linked to devops project
            odoo_project = request.env['project.project'].sudo().create({
                'name': f'[DevOps] {project.name}',
            })
            project.sudo().write({'odoo_project_id': odoo_project.id})

        created_ids = []
        Tag = request.env['project.tags'].sudo()
        for t in tasks:
            tag_ids = []
            tag_name = t.get('tag', '')
            if tag_name:
                tag = Tag.search([('name', '=', tag_name)], limit=1)
                if not tag:
                    tag = Tag.create({'name': tag_name})
                tag_ids = [(4, tag.id)]
            task = request.env['project.task'].sudo().create({
                'name': t.get('name', 'Sin titulo'),
                'description': t.get('description', ''),
                'project_id': odoo_project.id,
                'priority': t.get('priority', '0'),
                'tag_ids': tag_ids,
            })
            created_ids.append(task.id)

        if created_ids:
            meeting.sudo().write({'task_ids': [(4, tid) for tid in created_ids]})

        return {'status': 'ok', 'count': len(created_ids), 'task_ids': created_ids}

    @http.route('/devops/meetings/tasks', type='json', auth='user')
    def meetings_tasks(self, meeting_id):
        """Get tasks linked to a meeting."""
        meeting = request.env['devops.meeting'].sudo().browse(meeting_id)
        if not meeting.exists():
            return {'tasks': []}
        return {'tasks': [{
            'id': t.id,
            'name': t.name,
            'state': t.state,
            'priority': t.priority,
            'stage': t.stage_id.name if t.stage_id else '',
        } for t in meeting.task_ids]}

    @http.route('/devops/tasks/from_commit', type='json', auth='user')
    def tasks_from_commit(self, project_id, commit_message=''):
        """Check if a commit message references tasks and close them.

        Patterns: closes #123, fixes #123, resolves #123, task #123
        """
        import re
        if not commit_message:
            return {'closed': []}
        project = request.env['devops.project'].browse(project_id)
        if not project.exists() or not project.odoo_project_id:
            return {'closed': []}

        # Find task references
        pattern = r'(?:closes?|fix(?:es)?|resolves?|task)\s*#(\d+)'
        matches = re.findall(pattern, commit_message, re.IGNORECASE)
        closed = []
        for task_id_str in matches:
            task_id = int(task_id_str)
            task = request.env['project.task'].sudo().browse(task_id)
            if task.exists() and task.project_id.id == project.odoo_project_id.id:
                # Move to done stage or set state
                done_stage = request.env['project.task.type'].sudo().search([
                    ('name', 'ilike', 'done'),
                    ('project_ids', 'in', project.odoo_project_id.id),
                ], limit=1)
                if not done_stage:
                    done_stage = request.env['project.task.type'].sudo().search([
                        ('name', 'ilike', 'hecho'),
                    ], limit=1)
                vals = {'state': '1_done'}
                if done_stage:
                    vals['stage_id'] = done_stage.id
                task.sudo().write(vals)
                closed.append({'id': task.id, 'name': task.name})
        return {'closed': closed}

    @http.route('/devops/meetings/delete', type='json', auth='user')
    def meetings_delete(self, meeting_id):
        """Delete a meeting."""
        meeting = request.env['devops.meeting'].browse(meeting_id)
        if not meeting.exists():
            return {'error': 'Reunion no encontrada'}
        meeting.unlink()
        return {'status': 'ok'}

    # ---- Reports / Dashboard ----

    @http.route('/devops/reports/dashboard', type='json', auth='user')
    def reports_dashboard(self, project_id):
        """Get dashboard data: tasks, meetings, commits summary."""
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {}

        # Tasks summary
        tasks = []
        task_stats = {'total': 0, 'done': 0, 'in_progress': 0, 'open': 0}
        if project.odoo_project_id:
            all_tasks = request.env['project.task'].sudo().search([
                ('project_id', '=', project.odoo_project_id.id),
            ], order='create_date desc', limit=50)
            for t in all_tasks:
                tasks.append({
                    'id': t.id, 'name': t.name, 'state': t.state,
                    'priority': t.priority, 'stage': t.stage_id.name if t.stage_id else '',
                    'date': t.create_date.isoformat() if t.create_date else '',
                    'user': t.user_ids[0].name if t.user_ids else '',
                    'tags': [tag.name for tag in t.tag_ids],
                })
                task_stats['total'] += 1
                if t.state == '1_done':
                    task_stats['done'] += 1
                elif t.state == '01_in_progress':
                    task_stats['in_progress'] += 1
                else:
                    task_stats['open'] += 1

        # Meetings summary
        meetings = request.env['devops.meeting'].sudo().search([
            ('project_id', '=', project_id),
        ], order='date desc', limit=20)
        meeting_stats = {
            'total': len(meetings),
            'transcribed': len(meetings.filtered(lambda m: m.transcription)),
            'with_tasks': len(meetings.filtered(lambda m: m.task_ids)),
        }

        # Instance summary
        instances = []
        for inst in project.instance_ids:
            instances.append({
                'name': inst.name, 'type': inst.instance_type,
                'state': inst.state, 'branch': inst.git_branch or '',
            })

        # Tag distribution
        tag_counts = {}
        for t in tasks:
            for tag in t.get('tags', []):
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

        return {
            'tasks': tasks, 'task_stats': task_stats,
            'meeting_stats': meeting_stats,
            'instances': instances,
            'tag_counts': tag_counts,
        }

    # ---- Project Members ----

    @http.route('/devops/project/autodetect', type='json', auth='user')
    def project_autodetect(self, service_name, project_id=None):
        """Auto-detect all project config from a systemd service name.
        Uses SSH if the project has connection_type='ssh'.
        """
        import subprocess, re

        project = None
        if project_id:
            project = request.env['devops.project'].browse(project_id)
            if not project.exists():
                project = None

        # Helper to run commands (local or SSH)
        def run_cmd(cmd_str):
            if project and project.connection_type == 'ssh' and project.ssh_host:
                ssh_cmd = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=10']
                if project.ssh_key_path and os.path.isfile(project.ssh_key_path):
                    ssh_cmd += ['-i', project.ssh_key_path]
                if project.ssh_port and project.ssh_port != 22:
                    ssh_cmd += ['-p', str(project.ssh_port)]
                ssh_cmd += [f'{project.ssh_user or "root"}@{project.ssh_host}', cmd_str]
                r = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=15)
            else:
                r = subprocess.run(cmd_str, shell=True, capture_output=True, text=True, timeout=10)
            return r.stdout.strip() if r.returncode == 0 else ''

        result = {'service_name': service_name}
        try:
            # Service status
            status = run_cmd(f'systemctl is-active {service_name}.service')
            result['active'] = status == 'active'

            # ExecStart
            exec_line = run_cmd(f'systemctl show {service_name}.service --property=ExecStart')
            m = re.search(r'-c\s+(\S+)', exec_line)
            config_path = m.group(1) if m else f'/etc/odoo/{service_name}.conf'
            m2 = re.search(r'(\S+)/odoo/odoo-bin', exec_line)
            if m2:
                result['instance_path'] = m2.group(1)

            # Read config
            config_content = run_cmd(f'cat {config_path}')
            if config_content:
                result['config_path'] = config_path
                for line in config_content.split('\n'):
                    line = line.strip()
                    if '=' not in line or line.startswith('#') or line.startswith('['):
                        continue
                    key, _, val = line.partition('=')
                    key, val = key.strip(), val.strip()
                    if key == 'http_port':
                        result['port'] = int(val)
                    elif key == 'gevent_port':
                        result['gevent_port'] = int(val)
                    elif key == 'db_name':
                        result['database_name'] = val
                    elif key == 'logfile':
                        result['logfile'] = val
                    elif key == 'addons_path':
                        result['addons_path'] = val
                        for p in val.split(','):
                            p = p.strip()
                            if 'enterprise' in p.lower():
                                result['enterprise_path'] = p
                            elif p and '/odoo/' not in p:
                                # Custom addons (not odoo core)
                                result['repo_path'] = p

            # Domain from nginx
            nginx_output = run_cmd(f'grep -r "server_name" /etc/nginx/sites-enabled/ 2>/dev/null || true')
            if nginx_output:
                port = result.get('port', 0)
                for line in nginx_output.split('\n'):
                    m_d = re.search(r'server_name\s+([^\s;]+)', line)
                    if m_d and m_d.group(1) != '_':
                        result['domain'] = m_d.group(1)
                        break

        except Exception as e:
            result['error'] = str(e)

        return result

    @http.route('/devops/project/members', type='json', auth='user')
    def project_members(self, project_id):
        """List project members."""
        members = request.env['devops.project.member'].sudo().search([
            ('project_id', '=', project_id),
        ])
        return {'members': [{
            'id': m.id,
            'user_id': m.user_id.id,
            'user_name': m.user_id.name,
            'user_login': m.user_id.login,
            'role': m.role,
        } for m in members]}

    @http.route('/devops/users/list', type='json', auth='user')
    def users_list(self):
        """List Odoo users that have any DevOps group (potential project members)."""
        devops_groups = request.env['res.groups'].sudo().search([
            ('privilege_id.name', 'ilike', 'PatchMyByte DevOps'),
        ])
        if not devops_groups:
            # Fallback: all internal users
            users = request.env['res.users'].sudo().search([('share', '=', False), ('active', '=', True)])
        else:
            users = devops_groups.mapped('user_ids')
        return {'users': [{
            'id': u.id,
            'name': u.name,
            'login': u.login,
        } for u in users.sorted('name')]}

    @http.route('/devops/project/members/add', type='json', auth='user')
    def project_member_add(self, project_id, user_login='', role='developer'):
        """Add a member to a project by login."""
        if not request.env.user.has_group('pmb_devops.group_devops_admin'):
            return {'error': 'Solo administradores'}
        user = request.env['res.users'].sudo().search([('login', '=', user_login)], limit=1)
        if not user:
            return {'error': f'Usuario "{user_login}" no encontrado'}
        existing = request.env['devops.project.member'].sudo().search([
            ('project_id', '=', project_id), ('user_id', '=', user.id),
        ], limit=1)
        if existing:
            return {'error': 'El usuario ya es miembro'}
        request.env['devops.project.member'].sudo().create({
            'project_id': project_id,
            'user_id': user.id,
            'role': role,
        })
        return {'status': 'ok'}

    @http.route('/devops/project/members/remove', type='json', auth='user')
    def project_member_remove(self, member_id):
        """Remove a member from a project."""
        if not request.env.user.has_group('pmb_devops.group_devops_admin'):
            return {'error': 'Solo administradores'}
        member = request.env['devops.project.member'].sudo().browse(member_id)
        if member.exists():
            member.unlink()
        return {'status': 'ok'}

    @http.route('/devops/project/members/update_role', type='json', auth='user')
    def project_member_update_role(self, member_id, role):
        """Change a member's role."""
        if not request.env.user.has_group('pmb_devops.group_devops_admin'):
            return {'error': 'Solo administradores'}
        member = request.env['devops.project.member'].sudo().browse(member_id)
        if member.exists():
            member.write({'role': role})
        return {'status': 'ok'}

    @http.route('/devops/settings/groq_key', type='json', auth='user')
    def settings_groq_key(self, key=''):
        """Save Groq API key."""
        if not request.env.user.has_group('pmb_devops.group_devops_admin'):
            return {'error': 'Solo administradores'}
        request.env['ir.config_parameter'].sudo().set_param('pmb_devops.groq_api_key', key)
        return {'status': 'ok'}

    @http.route('/devops/user/prefs', type='json', auth='user')
    def user_prefs_get(self):
        """Get user UI preferences."""
        user = request.env.user
        return {
            'git_panel_width': user.devops_git_panel_width or 280,
            'sidebar_minimized': user.devops_sidebar_minimized,
            'git_collapsed': user.devops_git_collapsed,
        }

    @http.route('/devops/user/prefs/save', type='json', auth='user')
    def user_prefs_save(self, git_panel_width=None, sidebar_minimized=None, git_collapsed=None):
        """Save user UI preferences."""
        vals = {}
        if git_panel_width is not None:
            vals['devops_git_panel_width'] = max(150, min(600, int(git_panel_width)))
        if sidebar_minimized is not None:
            vals['devops_sidebar_minimized'] = bool(sidebar_minimized)
        if git_collapsed is not None:
            vals['devops_git_collapsed'] = bool(git_collapsed)
        if vals:
            request.env.user.sudo().write(vals)
        return {'status': 'ok'}

    @http.route('/devops/git/stage', type='json', auth='user')
    def git_stage(self, project_id, repo_path=''):
        """Stage all changes (git add -A)."""
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}
        is_ssh = project.connection_type == 'ssh' and project.ssh_host
        if not repo_path or (not is_ssh and not os.path.isdir(repo_path)):
            return {'error': 'Repo path not found'}
        from ..utils import ssh_utils
        result = ssh_utils.execute_command(project, ['git', 'add', '-A'], cwd=repo_path, timeout=15)
        if result.returncode != 0:
            return {'error': result.stderr.strip() or 'git add failed'}
        return {'status': 'ok'}

    @http.route('/devops/git/commit', type='json', auth='user')
    def git_commit(self, project_id, repo_path='', message=''):
        """Stage all and commit with the given message."""
        if not self._is_git_authed():
            return {'error': 'Autenticación requerida', 'auth_required': True}
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}
        is_ssh = project.connection_type == 'ssh' and project.ssh_host
        if not repo_path or (not is_ssh and not os.path.isdir(repo_path)):
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
        # Auto-close referenced tasks
        closed_tasks = []
        try:
            closed = self.tasks_from_commit(project_id, message)
            closed_tasks = closed.get('closed', [])
        except Exception:
            pass
        return {'status': 'ok', 'output': result.stdout.strip(), 'closed_tasks': closed_tasks}

    @http.route('/devops/git/push', type='json', auth='user')
    def git_push(self, project_id, repo_path=''):
        """Push current branch to origin."""
        if not self._is_git_authed():
            return {'error': 'Autenticación requerida', 'auth_required': True}
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}
        is_ssh = project.connection_type == 'ssh' and project.ssh_host
        if not repo_path or (not is_ssh and not os.path.isdir(repo_path)):
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
        if not self._is_git_authed():
            return {'error': 'Autenticación requerida', 'auth_required': True}
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}
        is_ssh = project.connection_type == 'ssh' and project.ssh_host
        if not repo_path:
            return {'error': 'Repo path not found'}
        if not is_ssh and not os.path.isdir(repo_path):
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
        # For SSH projects, run merge on remote server
        is_ssh = project.connection_type == 'ssh' and project.ssh_host
        if not repo_path:
            repo_path = project.repo_path
        if not is_ssh and (not repo_path or not os.path.isdir(repo_path)):
            return {'error': 'Repo path not found'}

        # Validate merge direction
        # Promote: development → staging → main (admin required for → main)
        # Sync:    main → staging, main → development (admin only)
        is_admin = request.env.user.has_group('pmb_devops.group_devops_admin')
        allowed_merges = {
            'development': ['staging'],
        }
        if is_admin:
            allowed_merges['staging'] = ['main']
            allowed_merges.setdefault('main', [])
            allowed_merges['main'].extend(['staging', 'development'])
        allowed_targets = allowed_merges.get(source_branch, [])
        if target_branch not in allowed_targets:
            return {'error': f'No se permite merge de {source_branch} → {target_branch}.'}

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

            # Check if we can write to this repo
            r_test = ssh_utils.execute_command(project, ['test', '-w', f'{repo_path}/.git', '&&', 'echo', 'writable'], cwd=repo_path, timeout=5)
            can_write = 'writable' in (r_test.stdout if r_test.returncode == 0 else '')

            if can_write:
                # We have write access: checkout + merge (traditional)
                git_cmd = ['git']
            else:
                # No write access — find repo owner and run as them via sudo
                import subprocess as sp
                stat_r = sp.run(['stat', '-c', '%U', f'{repo_path}/.git'], capture_output=True, text=True, timeout=5)
                repo_owner = stat_r.stdout.strip() if stat_r.returncode == 0 else ''
                if repo_owner and repo_owner != 'root':
                    git_cmd = ['sudo', '-u', repo_owner, 'git']
                else:
                    git_cmd = ['sudo', 'git']

            r = ssh_utils.execute_command(project, git_cmd + ['rev-parse', '--abbrev-ref', 'HEAD'], cwd=repo_path, timeout=5)
            original_branch = r.stdout.strip() if r.returncode == 0 else ''
            # Clean untracked files and stash changes to avoid merge conflicts
            ssh_utils.execute_command(project, git_cmd + ['stash', '--include-untracked'], cwd=repo_path, timeout=15)
            ssh_utils.execute_command(project, git_cmd + ['clean', '-fd'], cwd=repo_path, timeout=15)
            r = ssh_utils.execute_command(project, git_cmd + ['checkout', target_branch], cwd=repo_path, timeout=15)
            if r.returncode != 0:
                ssh_utils.execute_command(project, git_cmd + ['stash', 'pop'], cwd=repo_path, timeout=15)
                return {'error': f'Error al cambiar a {target_branch}: {r.stderr.strip()}'}
            ssh_utils.execute_command(project, git_cmd + ['pull', 'origin', target_branch], cwd=repo_path, timeout=60)
            result = ssh_utils.execute_command(project, git_cmd + [
                'merge', f'origin/{source_branch}',
                '-m', f'Merge {source_branch} into {target_branch}',
            ], cwd=repo_path, timeout=60)
            if result.returncode != 0:
                ssh_utils.execute_command(project, git_cmd + ['merge', '--abort'], cwd=repo_path, timeout=5)

            if result.returncode != 0:
                if original_branch:
                    ssh_utils.execute_command(project, git_cmd + ['checkout', original_branch], cwd=repo_path, timeout=15)
                return {'error': f'Conflicto de merge: {result.stderr.strip() or result.stdout.strip()}'}
            # Push
            push_r = ssh_utils.execute_command(project, git_cmd + ['push', 'origin', target_branch], cwd=repo_path, timeout=60)
            # Return to original branch and restore stash
            if original_branch and original_branch != target_branch:
                ssh_utils.execute_command(project, git_cmd + ['checkout', original_branch], cwd=repo_path, timeout=15)
            ssh_utils.execute_command(project, git_cmd + ['stash', 'pop'], cwd=repo_path, timeout=15)
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
            'subdomain_base': project.subdomain_base or '',
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
            'odoo_project_id': project.odoo_project_id.id if project.odoo_project_id else False,
            'odoo_project_name': project.odoo_project_id.name if project.odoo_project_id else '',
            'ssh_key_configured': bool(project.ssh_key_path and os.path.exists(project.ssh_key_path)),
        }

    @http.route('/devops/project/save', type='json', auth='user')
    def project_save(self, project_id=None, **vals):
        """Create or update a project."""
        allowed_fields = [
            'name', 'domain', 'subdomain_base', 'repo_path', 'enterprise_path', 'database_name',
            'connection_type', 'ssh_host', 'ssh_user', 'ssh_port',
            'max_staging', 'max_development', 'auto_destroy_hours', 'odoo_service_name',
            'production_branch', 'odoo_project_id',
            'github_client_id', 'github_client_secret',
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
