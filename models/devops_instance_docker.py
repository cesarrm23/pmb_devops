"""Docker runtime orchestrator for devops.instance.

When `devops.instance.project_id.runtime == 'docker'`, the normal
action_create_instance / action_destroy paths short-circuit to the
methods here instead of the systemd pipeline. Each docker instance
lives in its own compose stack on the client host under
`/opt/pmb-docker/<project>/<instance>/`.
"""
import logging
import secrets

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.modules.module import get_module_path

from ..utils import ssh_utils

_logger = logging.getLogger(__name__)

DOCKER_ROOT = '/opt/pmb-docker'
NGINX_SNIPPETS_DIR = '/etc/nginx/snippets'
ODOO_CORE_VOLUME_PREFIX = 'pmb-odoo-core'


class DevopsInstanceDocker(models.Model):
    _inherit = 'devops.instance'

    # ---- Docker-specific fields (only meaningful when runtime='docker') ----
    code_server_port = fields.Integer(string='Puerto code-server')
    code_server_token = fields.Char(string='Password code-server')
    db_password = fields.Char(string='Password Postgres (container)')
    docker_compose_path = fields.Char(string='Ruta compose')

    # ------------------------------------------------------------------
    # Dispatch hooks — extend the existing lifecycle
    # ------------------------------------------------------------------

    def action_create_instance(self):
        """Short-circuit to the Docker path when the project opted in,
        otherwise fall through to the systemd pipeline."""
        self.ensure_one()
        if self.project_id.runtime == 'docker':
            return self._action_create_instance_docker()
        return super().action_create_instance()

    def action_destroy(self):
        self.ensure_one()
        if self.project_id.runtime == 'docker':
            return self._action_destroy_docker()
        return super().action_destroy()

    def action_start(self):
        self.ensure_one()
        if self.project_id.runtime == 'docker':
            return self._docker_compose(['start'])
        return super().action_start()

    def action_stop(self):
        self.ensure_one()
        if self.project_id.runtime == 'docker':
            return self._docker_compose(['stop'])
        return super().action_stop()

    def action_restart(self):
        self.ensure_one()
        if self.project_id.runtime == 'docker':
            return self._docker_compose(['restart'])
        return super().action_restart()

    # ------------------------------------------------------------------
    # Docker create — synchronous for now (deploy is fast: ~3-5s cold)
    # ------------------------------------------------------------------

    def _action_create_instance_docker(self):
        """Render compose + nginx snippet, push to remote, `docker compose up`."""
        self._validate_instance_limits()

        project = self.project_id
        odoo_port = self._find_free_port()
        longpoll_port = odoo_port + 1000
        # Pick code-server port in a separate band so we don't collide
        code_port = odoo_port + 1500

        safe_name = self.name.replace(' ', '-').lower()
        stack = f"{project.name.replace(' ', '').lower()}-{safe_name}"
        db_name = f"{project.name.replace(' ', '_').lower()}_{safe_name}"
        compose_dir = f"{DOCKER_ROOT}/{project.name.lower()}/{safe_name}"

        db_password = secrets.token_urlsafe(24)
        cs_token = secrets.token_urlsafe(24)
        admin_password = secrets.token_urlsafe(16)

        # Compute domain (reuse existing pattern)
        domain = project.domain or ''
        sub_base = project.subdomain_base or domain
        if self.instance_type == 'production' or not sub_base:
            full_domain = domain
        else:
            full_domain = f"{safe_name}.{sub_base}"

        self.write({
            'port': odoo_port,
            'gevent_port': longpoll_port,
            'code_server_port': code_port,
            'code_server_token': cs_token,
            'db_password': db_password,
            'database_name': db_name,
            'service_name': f"pmb-{stack}",  # docker stack name, no systemd
            'docker_compose_path': f"{compose_dir}/docker-compose.yml",
            'subdomain': '' if self.instance_type == 'production' else safe_name,
            'state': 'creating',
            'creation_step': 'Provisionando host Docker...',
        })
        self.env.cr.commit()

        try:
            project._ensure_docker_host()
            self.write({'creation_step': 'Renderizando plantillas...'})
            self.env.cr.commit()

            odoo_version = self._detect_odoo_version()
            ctx = {
                'project_code': project.name.lower().replace(' ', ''),
                'instance_name': safe_name,
                'odoo_version': odoo_version,
                'instance_port': odoo_port,
                'longpoll_port': longpoll_port,
                'code_server_port': code_port,
                'db_name': db_name,
                'db_password': db_password,
                'admin_password': admin_password,
                'code_server_token': cs_token,
                'addons_host_path': project.repo_path or '/opt/code',
                'workers': 2,
            }

            compose_yml = self._render_template('docker-compose.yml.j2', ctx)
            odoo_conf = self._render_template('odoo.conf.j2', ctx)
            nginx_loc = self._render_template('nginx.location.j2', ctx)

            ssh_utils.execute_command(project, ['mkdir', '-p', compose_dir], timeout=15)
            ssh_utils.write_text(project, f"{compose_dir}/docker-compose.yml", compose_yml)
            ssh_utils.write_text(project, f"{compose_dir}/odoo.conf", odoo_conf)
            snippet_name = f"pmb-{stack}.conf"
            ssh_utils.execute_command(project, ['sudo', 'mkdir', '-p', NGINX_SNIPPETS_DIR], timeout=10)
            # Write nginx snippet via sudo tee so root can own it
            self._ssh_write_as_root(project, f"{NGINX_SNIPPETS_DIR}/{snippet_name}", nginx_loc)

            self.write({'creation_step': 'docker compose up -d...'})
            self.env.cr.commit()
            res = ssh_utils.execute_command(
                project, ['docker', 'compose', 'up', '-d'],
                timeout=600, cwd=compose_dir,
            )
            if res.returncode != 0:
                raise UserError(_("docker compose up falló:\n%s") % (res.stderr or res.stdout))

            self.write({'creation_step': 'Recargando nginx...'})
            self.env.cr.commit()
            res = ssh_utils.execute_command(project, ['sudo', 'nginx', '-t'], timeout=15)
            if res.returncode == 0:
                ssh_utils.execute_command(project, ['sudo', 'nginx', '-s', 'reload'], timeout=15)
            else:
                _logger.warning("nginx -t falló en %s: %s", project.name, res.stderr)

            self.write({
                'state': 'running',
                'creation_step': 'Listo.',
                'creation_pid': 0,
            })
            return {'status': 'ok', 'compose_dir': compose_dir}
        except Exception as e:
            _logger.exception("Docker deploy failed for instance %s", self.id)
            self.write({
                'state': 'error',
                'creation_step': f'Error: {e}',
                'creation_pid': 0,
            })
            raise

    def _action_destroy_docker(self):
        """docker compose down + cleanup nginx snippet."""
        self.ensure_one()
        project = self.project_id
        self.write({'state': 'destroying'})
        self.env.cr.commit()

        compose_dir = self._docker_instance_dir()
        stack = self.service_name.replace('pmb-', '') if self.service_name else ''

        # Stack down (best-effort — continue even if compose is already gone)
        if compose_dir:
            ssh_utils.execute_command(
                project, ['docker', 'compose', 'down', '-v'],
                timeout=120, cwd=compose_dir,
            )
            ssh_utils.execute_command(project, ['rm', '-rf', compose_dir], timeout=30)

        if stack:
            snippet = f"{NGINX_SNIPPETS_DIR}/pmb-{stack}.conf"
            ssh_utils.execute_command(project, ['sudo', 'rm', '-f', snippet], timeout=10)
            ssh_utils.execute_command(project, ['sudo', 'nginx', '-s', 'reload'], timeout=15)

        self.unlink()
        return True

    def _docker_compose(self, subcmd):
        """Run `docker compose <subcmd>` in this instance's compose dir."""
        self.ensure_one()
        compose_dir = self._docker_instance_dir()
        if not compose_dir:
            raise UserError(_("Instancia Docker sin directorio compose."))
        res = ssh_utils.execute_command(
            self.project_id, ['docker', 'compose'] + subcmd,
            timeout=120, cwd=compose_dir,
        )
        return {
            'returncode': res.returncode,
            'stdout': (res.stdout or '')[-4000:],
            'stderr': (res.stderr or '')[-2000:],
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _docker_instance_dir(self):
        if self.docker_compose_path:
            # strip /docker-compose.yml
            return self.docker_compose_path.rsplit('/', 1)[0]
        return ''

    def _detect_odoo_version(self):
        """Best-effort Odoo version detection for the base image tag.
        Reads it from project.repo_path/odoo/release.py if available;
        falls back to '19'."""
        project = self.project_id
        if not project.repo_path:
            return '19'
        release = ssh_utils.read_text(
            project, f"{project.repo_path}/odoo/release.py", timeout=10,
        )
        # version_info = (18, 0, ...) — take first int
        import re
        m = re.search(r'version_info\s*=\s*\((\d+)', release or '')
        return m.group(1) if m else '19'

    def _render_template(self, name, ctx):
        """Render a Jinja2 template from pmb_devops/data/docker/."""
        try:
            import jinja2
        except ImportError:
            raise UserError(_("Jinja2 no está disponible en el hub."))
        mod_path = get_module_path('pmb_devops')
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(f"{mod_path}/data/docker"),
            autoescape=False,
            undefined=jinja2.StrictUndefined,
            keep_trailing_newline=True,
        )
        return env.get_template(name).render(**ctx)

    def _ssh_write_as_root(self, project, path, content):
        """Write a file as root on the remote host (for /etc/*). Falls back
        to a plain write for local projects where the hub already runs as
        a privileged user or when sudo is configured without password."""
        if project.connection_type == 'local':
            return ssh_utils.write_text(project, path, content)
        # Pipe via sudo tee
        import subprocess
        ssh_args = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=10']
        if project.ssh_key_path:
            ssh_args += ['-i', project.ssh_key_path]
        if project.ssh_port and project.ssh_port != 22:
            ssh_args += ['-p', str(project.ssh_port)]
        ssh_args.append(f'{project.ssh_user}@{project.ssh_host}')
        ssh_args.append(f'sudo tee {path!r} > /dev/null')
        return subprocess.run(
            ssh_args, input=content, capture_output=True, text=True, timeout=30,
        ).returncode == 0


class DevopsProjectDocker(models.Model):
    _inherit = 'devops.project'

    def _ensure_docker_host(self):
        """Idempotent host setup: ensures docker + compose are installed,
        pulls the code-server image, ensures the shared Odoo core volume
        exists, builds or pulls the pmb/odoo:<ver> image.
        Called before deploying the first Docker instance on a host."""
        self.ensure_one()

        # 1. Check docker binary
        res = ssh_utils.execute_command(self, ['which', 'docker'], timeout=10)
        if res.returncode != 0:
            res = ssh_utils.execute_command_shell(
                self,
                'curl -fsSL https://get.docker.com | sudo sh',
                timeout=300,
            )
            if res.returncode != 0:
                raise UserError(_("No se pudo instalar docker: %s") % (res.stderr or ''))

        # 2. Check compose plugin
        res = ssh_utils.execute_command(self, ['docker', 'compose', 'version'], timeout=10)
        if res.returncode != 0:
            # docker-compose-plugin is installed by get.docker.com on Debian/Ubuntu
            raise UserError(_("docker compose no disponible tras instalación base."))

        # 3. Pull code-server (public image, no build needed)
        ssh_utils.execute_command(
            self, ['docker', 'pull', 'codercom/code-server:latest'],
            timeout=600,
        )

        # 4. Ensure Odoo core shared volume — populated from host's Odoo checkout
        odoo_version = '19'  # TODO: detect from repo
        vol = f"{ODOO_CORE_VOLUME_PREFIX}-{odoo_version}"
        res = ssh_utils.execute_command_shell(
            self, f"docker volume ls -q -f name=^{vol}$", timeout=10,
        )
        if not (res.stdout or '').strip():
            # Create empty volume; the operator must seed it separately
            # (rsync from /opt/odoo<ver> into the volume). We don't auto-
            # populate here because copying ~500MB via exec is slow and
            # may race; README covers the one-time bootstrap.
            ssh_utils.execute_command(self, ['docker', 'volume', 'create', vol], timeout=15)
            _logger.warning(
                "Created empty volume %s on %s — seed it with the host's "
                "Odoo checkout before deploying instances.",
                vol, self.ssh_host or 'local',
            )

        # 5. Build base image if absent. Build context is uploaded to
        # /tmp/pmb-docker-build-<ver> on the remote host.
        img = f"pmb/odoo:{odoo_version}"
        res = ssh_utils.execute_command_shell(
            self, f"docker image inspect {img} >/dev/null 2>&1 && echo ok || echo miss",
            timeout=10,
        )
        if 'ok' not in (res.stdout or ''):
            self._build_odoo_image_on_host(odoo_version)

        return True

    def _build_odoo_image_on_host(self, odoo_version):
        """Upload the Dockerfile + requirements to the remote host and build
        pmb/odoo:<ver>. Runs once per host per version."""
        self.ensure_one()
        build_dir = f"/tmp/pmb-docker-build-{odoo_version}"
        mod_path = get_module_path('pmb_devops')
        dockerfile_src = f"{mod_path}/data/docker/Dockerfile.odoo{odoo_version}"
        req_src = f"{mod_path}/data/docker/requirements.odoo{odoo_version}.txt"

        with open(dockerfile_src, 'r') as f:
            dockerfile = f.read()
        with open(req_src, 'r') as f:
            requirements = f.read()

        ssh_utils.execute_command(self, ['mkdir', '-p', build_dir], timeout=10)
        ssh_utils.write_text(self, f"{build_dir}/Dockerfile", dockerfile)
        ssh_utils.write_text(self, f"{build_dir}/requirements.odoo{odoo_version}.txt", requirements)

        res = ssh_utils.execute_command(
            self,
            ['docker', 'build', '-t', f"pmb/odoo:{odoo_version}",
             '-f', f"{build_dir}/Dockerfile", build_dir],
            timeout=1800,  # 30 min for cold build with apt + pip
        )
        if res.returncode != 0:
            raise UserError(_(
                "docker build pmb/odoo:%s falló:\n%s"
            ) % (odoo_version, res.stderr or res.stdout))
        return True
