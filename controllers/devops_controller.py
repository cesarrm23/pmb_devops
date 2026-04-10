"""API endpoints for the PMB DevOps SPA."""
import json
import logging

from odoo import http
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
    def instance_poll_status(self, instance_id):
        """Poll instance creation status."""
        instance = request.env['devops.instance'].browse(instance_id)
        if not instance.exists():
            return {'error': 'Not found'}
        return {
            'state': instance.state,
            'creation_step': instance.creation_step or '',
        }

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

    @http.route('/devops/branch/history', type='json', auth='user')
    def branch_history(self, project_id, branch_name, limit=20):
        """Get commit history for a branch."""
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}

        from ..utils import git_utils
        commits = git_utils.git_log(project, branch=branch_name, count=limit)
        return {'commits': commits}

    @http.route('/devops/commit/detail', type='json', auth='user')
    def commit_detail(self, project_id, commit_hash):
        """Get commit detail: full message + changed files."""
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}

        from ..utils import ssh_utils

        # Get full commit message (body)
        result = ssh_utils.execute_command(project, [
            'git', 'log', '-1', '--format=%B', commit_hash,
        ], cwd=project.repo_path)
        body = result.stdout.strip() if result.returncode == 0 else ''

        # Get changed files with stats
        result = ssh_utils.execute_command(project, [
            'git', 'diff-tree', '--no-commit-id', '-r', '--name-status', commit_hash,
        ], cwd=project.repo_path)
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
        ], cwd=project.repo_path)
        stat = result.stdout.strip() if result.returncode == 0 else ''

        return {'body': body, 'files': files, 'stat': stat}

    @http.route('/devops/branch/merge', type='json', auth='user')
    def branch_merge(self, project_id, source_branch, target_branch):
        """Merge source branch into target."""
        project = request.env['devops.project'].browse(project_id)
        if not project.exists():
            return {'error': 'Proyecto no encontrado'}

        from ..utils import ssh_utils
        try:
            # Checkout target
            ssh_utils.execute_command(project, ['git', 'checkout', target_branch], cwd=project.repo_path)
            # Merge source
            result = ssh_utils.execute_command(
                project,
                ['git', 'merge', source_branch, '-m', f'Merge {source_branch} into {target_branch}'],
                cwd=project.repo_path,
            )
            if result.returncode != 0:
                return {'status': 'error', 'error': result.stderr}
            # Push
            ssh_utils.execute_command(project, ['git', 'push', 'origin', target_branch], cwd=project.repo_path)
            return {'status': 'ok', 'output': result.stdout}
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

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
