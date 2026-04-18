"""Docker runtime orchestrator for devops.instance.

When `devops.instance.project_id.runtime == 'docker'`, the normal
action_create_instance / action_destroy paths short-circuit to the
methods here instead of the systemd pipeline. Each docker instance
lives in its own compose stack on the client host under
`/opt/pmb-docker/<project>/<instance>/`.

Deploys are asynchronous (subprocess.Popen) to match the systemd
pipeline pattern — the HTTP request returns immediately, the deploy
script runs for minutes, updating devops_instance via psql.
"""
import logging
import os
import re
import secrets
import stat
import subprocess

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.modules.module import get_module_path

from ..utils import ssh_utils

_logger = logging.getLogger(__name__)

DOCKER_ROOT = '/opt/pmb-docker'
NGINX_SITES_DIR = '/etc/nginx/sites-enabled'
ODOO_CORE_VOLUME_PREFIX = 'pmb-odoo-core'
STAGING_ROOT = '/tmp/pmb_docker_deploy'


class DevopsInstanceDocker(models.Model):
    _inherit = 'devops.instance'

    code_server_port = fields.Integer(string='Puerto code-server')
    code_server_token = fields.Char(string='Password code-server')
    db_password = fields.Char(string='Password Postgres (container)')
    docker_compose_path = fields.Char(string='Ruta compose')

    # ------------------------------------------------------------------
    # Dispatch hooks — extend the existing lifecycle
    # ------------------------------------------------------------------

    def action_create_instance(self):
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
    # Docker create — async via subprocess.Popen
    # ------------------------------------------------------------------

    def _action_create_instance_docker(self):
        """Stage templates on the hub, spawn a detached deploy script
        that SCPs them to the client host and runs the full provision +
        compose + nginx + certbot pipeline. The script updates
        devops_instance via psql as it progresses."""
        self._validate_instance_limits()

        project = self.project_id
        odoo_port = self._find_free_port()
        # Pick longpoll / code-server with gaps that avoid typical Odoo
        # ranges (8069-8079) and gevent (9069-9079) on client hosts.
        longpoll_port = self._find_free_port_distinct(avoid={odoo_port})
        code_port = self._find_free_port_distinct(avoid={odoo_port, longpoll_port})

        safe_name = re.sub(r'[^a-z0-9-]', '-', self.name.lower()).strip('-') or 'inst'
        project_code = re.sub(r'[^a-z0-9]', '', project.name.lower()) or 'proj'
        stack = f"{project_code}-{safe_name}"
        db_name = f"{project_code}_{safe_name}"
        compose_dir = f"{DOCKER_ROOT}/{project_code}/{safe_name}"

        db_password = secrets.token_urlsafe(24)
        cs_token = secrets.token_urlsafe(24)
        admin_password = secrets.token_urlsafe(16)

        # Domain strategy: production = bare domain; dev/staging = subdomain.
        domain = project.domain or ''
        sub_base = project.subdomain_base or domain
        if self.instance_type == 'production' or not sub_base:
            full_domain = domain
            subdomain = ''
        else:
            full_domain = f"{safe_name}.{sub_base}"
            subdomain = safe_name

        self.write({
            'port': odoo_port,
            'gevent_port': longpoll_port,
            'code_server_port': code_port,
            'code_server_token': cs_token,
            'db_password': db_password,
            'database_name': db_name,
            'service_name': f"pmb-{stack}",
            'docker_compose_path': f"{compose_dir}/docker-compose.yml",
            'subdomain': subdomain,
            'full_domain': full_domain,
            'state': 'creating',
            'creation_step': 'Encolando despliegue Docker...',
        })
        self.env.cr.commit()

        odoo_version = self._detect_odoo_version()
        addons_path_conf = self._compute_addons_path(project)
        ctx = {
            'instance_id': self.id,
            'project_code': project_code,
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
            'addons_path_conf': addons_path_conf,
            'workers': 2,
            'domain': full_domain,
        }

        # Stage rendered templates + Dockerfile on the hub
        staging = f"{STAGING_ROOT}/{self.id}"
        os.makedirs(staging, exist_ok=True)
        self._write(staging, 'docker-compose.yml', self._render_template('docker-compose.yml.j2', ctx))
        self._write(staging, 'odoo.conf', self._render_template('odoo.conf.j2', ctx))
        self._write(staging, 'nginx.vhost', self._render_template('nginx.vhost.j2', ctx))

        mod_path = get_module_path('pmb_devops')
        # Copy Dockerfile (version-agnostic) + the version-specific
        # requirements so the remote can build the image if absent.
        for fn in ('Dockerfile', f'requirements.odoo{odoo_version}.txt'):
            src = f"{mod_path}/data/docker/{fn}"
            if os.path.exists(src):
                with open(src) as f:
                    self._write(staging, fn, f.read())

        # Generate the deploy script
        dbname = self.env.cr.dbname
        script = self._build_deploy_script(ctx, staging, compose_dir, full_domain, dbname)
        script_path = f"{staging}/deploy.sh"
        with open(script_path, 'w') as f:
            f.write(script)
        os.chmod(script_path, 0o755)

        # Launch detached — HTTP returns immediately; script runs 2-30 min
        subprocess.Popen(
            ['/bin/bash', script_path],
            start_new_session=True,
            stdout=open(f"{staging}/deploy.log", 'w'),
            stderr=subprocess.STDOUT,
        )
        return {'status': 'queued', 'instance_id': self.id}

    def _action_destroy_docker(self):
        """docker compose down -v + cleanup nginx + certbot renewal hook."""
        self.ensure_one()
        project = self.project_id
        self.write({'state': 'destroying'})
        self.env.cr.commit()

        compose_dir = self._docker_instance_dir()
        domain = self.full_domain or ''

        if compose_dir:
            ssh_utils.execute_command(
                project, ['docker', 'compose', 'down', '-v'],
                timeout=120, cwd=compose_dir,
            )
            ssh_utils.execute_command(project, ['rm', '-rf', compose_dir], timeout=30)

        if domain:
            ssh_utils.execute_command(
                project, ['sudo', 'rm', '-f', f"{NGINX_SITES_DIR}/{domain}"],
                timeout=10,
            )
            ssh_utils.execute_command(project, ['sudo', 'nginx', '-s', 'reload'], timeout=15)

        self.unlink()
        return True

    def _docker_compose(self, subcmd):
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

    def _find_free_port_distinct(self, avoid):
        """Get a free port that's also not in the avoid set (for
        allocating longpoll/code-server alongside the main odoo port
        without racing ourselves)."""
        import subprocess as _sp
        project = self.project_id
        is_ssh = project.connection_type == 'ssh' and project.ssh_host
        used = set(self.search([('port', '!=', False)]).mapped('port'))
        used |= set(self.search([('gevent_port', '!=', False)]).mapped('gevent_port'))
        used |= set(self.search([('code_server_port', '!=', False)]).mapped('code_server_port'))
        used |= avoid
        try:
            if is_ssh:
                res = ssh_utils.execute_command(project, ['ss', '-tlnH'])
                out = res.stdout if res.returncode == 0 else ''
            else:
                out = _sp.run(['ss', '-tlnH'], capture_output=True, text=True, timeout=5).stdout
            for line in out.split('\n'):
                parts = line.split()
                if len(parts) >= 4 and ':' in parts[3]:
                    try:
                        used.add(int(parts[3].rsplit(':', 1)[1]))
                    except ValueError:
                        pass
        except Exception:
            pass
        for port in range(18000, 18500):
            if port not in used:
                return port
        raise UserError(_("No hay puertos disponibles en rango 18000-18499."))

    def _docker_instance_dir(self):
        if self.docker_compose_path:
            return self.docker_compose_path.rsplit('/', 1)[0]
        return ''

    def _compute_addons_path(self, project):
        """Build the Odoo `addons_path` value as it will appear inside the
        container. The core volume is always mounted at /mnt/odoo
        (containing the Odoo source tree). The project's `repo_path` is
        mounted at /mnt/addons — but different projects structure their
        repos differently (MAHA has enterprise/ + addons_maha/ as
        subdirs; Asistente has custom_addons as the repo root).

        We probe the host filesystem via ssh_utils to enumerate
        immediate subdirectories of repo_path that contain at least one
        folder with a __manifest__.py, and include those as separate
        addons_path entries. enterprise_path is always included if set.
        """
        container_parts = [
            '/mnt/odoo/odoo/addons',
            '/mnt/odoo/addons',
        ]
        # Probe repo_path for addon-containing subdirs
        repo = project.repo_path or ''
        if repo:
            # Single shell command lists every direct child of repo that has
            # at least one addon manifest inside it (depth 2 search).
            probe = (
                f"for d in {repo}/*/; do "
                f"  if ls \"$d\"*/__manifest__.py >/dev/null 2>&1; then "
                f"    basename \"$d\"; "
                f"  fi; "
                f"done"
            )
            try:
                res = ssh_utils.execute_command(project, ['bash', '-c', probe], timeout=15)
                subs = [s.strip() for s in (res.stdout or '').split('\n') if s.strip()]
            except Exception:
                subs = []
            for sub in subs:
                container_parts.append(f'/mnt/addons/{sub}')
            if not subs:
                # Fallback: maybe the repo itself is the addons dir
                container_parts.append('/mnt/addons')
        ent = project.enterprise_path or ''
        if ent and repo and ent.startswith(repo + '/'):
            rel = ent[len(repo) + 1:]
            entry = f'/mnt/addons/{rel}'
            if entry not in container_parts:
                container_parts.append(entry)
        # Deduplicate preserving order
        seen, result = set(), []
        for p in container_parts:
            if p not in seen:
                seen.add(p); result.append(p)
        return ','.join(result)

    def _detect_odoo_version(self):
        project = self.project_id
        if not project.repo_path:
            return '19'
        # Standard Odoo layout: <repo>/odoo/odoo/release.py (the inner
        # `odoo` is the Python package). Fall back to the flatter layout
        # just in case a project pinned the package at the top level.
        for rel in ('odoo/odoo/release.py', 'odoo/release.py'):
            release = ssh_utils.read_text(
                project, f"{project.repo_path}/{rel}", timeout=10,
            )
            m = re.search(r'version_info\s*=\s*\((\d+)', release or '')
            if m:
                return m.group(1)
        return '19'

    def _render_template(self, name, ctx):
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

    @staticmethod
    def _write(dirpath, name, content):
        with open(os.path.join(dirpath, name), 'w') as f:
            f.write(content)

    def _ssh_prefix(self):
        """Return the ssh command list (without the remote command) for
        SCP/ssh from inside the deploy shell script."""
        p = self.project_id
        if p.connection_type == 'local':
            return None  # local — no ssh hop
        args = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=10']
        if p.ssh_key_path:
            args += ['-i', p.ssh_key_path]
        if p.ssh_port and p.ssh_port != 22:
            args += ['-p', str(p.ssh_port)]
        args.append(f"{p.ssh_user}@{p.ssh_host}")
        return args

    def _scp_prefix(self):
        p = self.project_id
        if p.connection_type == 'local':
            return None
        args = ['scp', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=10']
        if p.ssh_key_path:
            args += ['-i', p.ssh_key_path]
        if p.ssh_port and p.ssh_port != 22:
            args += ['-P', str(p.ssh_port)]
        return args

    # ------------------------------------------------------------------
    # Deploy script (runs on hub, orchestrates remote host via ssh/scp)
    # ------------------------------------------------------------------

    def _build_deploy_script(self, ctx, staging, compose_dir, full_domain, dbname):
        """Generate the bash script that provisions + deploys.
        Structure: prelude (DB-updating helpers) → provision-if-needed
        → push files → docker compose up → nginx + certbot → verify."""
        p = self.project_id
        is_local = p.connection_type == 'local'
        ssh_cmd = ' '.join(self._ssh_prefix() or ['bash', '-c'])
        scp_cmd = ' '.join(self._scp_prefix() or ['cp'])
        remote_tag = '(local)' if is_local else f"{p.ssh_user}@{p.ssh_host}"
        host_addons = ctx['addons_host_path']
        version = ctx['odoo_version']
        marker = f"{DOCKER_ROOT}/.provisioned-{version}"
        image_tag = f"pmb/odoo:{version}"
        vol_name = f"{ODOO_CORE_VOLUME_PREFIX}-{version}"
        code_server_port = ctx['code_server_port']
        instance_port = ctx['instance_port']
        longpoll_port = ctx['longpoll_port']

        # Build the RUN / PUT wrappers
        if is_local:
            run_header = 'RUN() { "$@"; }\n' \
                         'RUN_SH() { bash -c "$1"; }\n' \
                         'PUT() { cp "$1" "$2"; }\n'
        else:
            run_header = (
                f'SSH=( {ssh_cmd} )\n'
                f'SCP=( {scp_cmd} )\n'
                'RUN() { "${SSH[@]}" "$(printf \'%q \' "$@")"; }\n'
                'RUN_SH() { "${SSH[@]}" "$1"; }\n'
                'PUT() { "${SCP[@]}" "$1" "' + f"{p.ssh_user}@{p.ssh_host}" + ':$2"; }\n'
            )

        # PG URI the deploy script uses for status updates. Hub is local
        # to the DB, so psql without args works.
        odoo_version = ctx['odoo_version']

        script = f"""#!/bin/bash
# Auto-generated by pmb_devops docker orchestrator
set -uo pipefail
ID={self.id}
DB="{dbname}"
STAGING="{staging}"
LOG="$STAGING/deploy.log"
COMPOSE_DIR="{compose_dir}"
DOMAIN="{full_domain}"
IMAGE="{image_tag}"
VOL="{vol_name}"
MARKER="{marker}"
ODOO_VER="{odoo_version}"
ADDONS_HOST="{host_addons}"
INSTANCE_PORT={instance_port}
LONGPOLL_PORT={longpoll_port}
CODE_SERVER_PORT={code_server_port}

{run_header}

STEP() {{
    echo "[$(date +%H:%M:%S)] $1" >> "$LOG"
    psql -q "$DB" -c "UPDATE devops_instance SET creation_step='$1', creation_pid=$$ WHERE id=$ID;" 2>/dev/null
}}
FAIL() {{
    echo "[FAIL] $1" >> "$LOG"
    psql -q "$DB" -c "UPDATE devops_instance SET state='error', creation_step='Error: $1', creation_pid=0 WHERE id=$ID;" 2>/dev/null
    exit 1
}}

psql -q "$DB" -c "UPDATE devops_instance SET creation_pid=$$ WHERE id=$ID;" 2>/dev/null
echo "=== pmb docker deploy id=$ID → {remote_tag} @ $(date) ===" >> "$LOG"

# ----- Phase 1: Provision host (idempotent) -----------------------------
STEP "Revisando Docker en host..."
if ! RUN test -f "$MARKER"; then
    STEP "Instalando Docker (primera vez)..."
    if ! RUN which docker >/dev/null 2>&1; then
        RUN_SH "curl -fsSL https://get.docker.com | sh" >> "$LOG" 2>&1 || FAIL "docker install failed"
    fi
    RUN docker compose version >/dev/null 2>&1 || FAIL "docker compose plugin missing"

    STEP "Pull code-server image..."
    RUN docker pull codercom/code-server:latest >> "$LOG" 2>&1 || FAIL "pull code-server failed"

    STEP "Preparando volumen Odoo core..."
    if ! RUN docker volume inspect "$VOL" >/dev/null 2>&1; then
        RUN docker volume create "$VOL" >> "$LOG" 2>&1 || FAIL "volume create failed"
    fi
    # Seed volume from a reasonable Odoo checkout on the remote host
    SEEDED=$(RUN_SH "docker run --rm -v $VOL:/v alpine sh -c 'ls /v 2>/dev/null | head -1'" 2>/dev/null | tr -d '[:space:]')
    if [ -z "$SEEDED" ]; then
        STEP "Sembrando /mnt/odoo en volumen (puede tardar)..."
        # Try candidate Odoo source dirs in order
        for SRC in "$ADDONS_HOST/odoo" "/opt/odoo$ODOO_VER" "/opt/odoo$ODOO_VER/odoo" "$ADDONS_HOST"; do
            if RUN test -d "$SRC/odoo-bin" -o -f "$SRC/odoo-bin"; then
                SRC_OK="$SRC"; break
            fi
        done
        # Fallback: look for any odoo-bin under / (shallow)
        if [ -z "${{SRC_OK:-}}" ]; then
            SRC_OK=$(RUN_SH "find /opt /srv /root -maxdepth 4 -name odoo-bin -printf '%h\\n' 2>/dev/null | head -1")
        fi
        if [ -z "${{SRC_OK:-}}" ]; then
            FAIL "no odoo source found on host to seed $VOL"
        fi
        echo "seeding from $SRC_OK" >> "$LOG"
        RUN_SH "docker run --rm -v $VOL:/dst -v $SRC_OK:/src:ro alpine sh -c 'cp -a /src/. /dst/'" >> "$LOG" 2>&1 \\
            || FAIL "volume seed failed"
    fi

    STEP "Construyendo imagen $IMAGE..."
    if ! RUN docker image inspect "$IMAGE" >/dev/null 2>&1; then
        RUN mkdir -p /tmp/pmb-img-build
        PUT "$STAGING/Dockerfile" /tmp/pmb-img-build/Dockerfile >> "$LOG" 2>&1 \\
            || FAIL "PUT Dockerfile failed"
        # Remote expects the file literally named "requirements.txt" (the
        # Dockerfile is version-agnostic; we rename at SCP-time).
        PUT "$STAGING/requirements.odoo$ODOO_VER.txt" /tmp/pmb-img-build/requirements.txt >> "$LOG" 2>&1 \\
            || FAIL "PUT requirements failed"
        RUN_SH "cd /tmp/pmb-img-build && docker build -t $IMAGE ." >> "$LOG" 2>&1 \\
            || FAIL "docker build failed"
    fi

    RUN mkdir -p "{DOCKER_ROOT}"
    RUN_SH "touch $MARKER" || true
fi

# ----- Phase 2: Push instance files -------------------------------------
STEP "Publicando compose + odoo.conf..."
RUN mkdir -p "$COMPOSE_DIR" || FAIL "mkdir compose dir"
PUT "$STAGING/docker-compose.yml" "$COMPOSE_DIR/docker-compose.yml" >> "$LOG" 2>&1 \\
    || FAIL "PUT compose"
PUT "$STAGING/odoo.conf" "$COMPOSE_DIR/odoo.conf" >> "$LOG" 2>&1 \\
    || FAIL "PUT odoo.conf"

# ----- Phase 3: Start stack ---------------------------------------------
STEP "docker compose up -d..."
RUN_SH "cd $COMPOSE_DIR && docker compose up -d" >> "$LOG" 2>&1 \\
    || FAIL "docker compose up failed"

# ----- Phase 3b: Wait for Postgres, init Odoo DB ------------------------
STACK="pmb-{ctx['project_code']}-{ctx['instance_name']}"
DB_CONTAINER="$STACK-db"
ODOO_CONTAINER="$STACK-odoo"
DB_NAME="{ctx['db_name']}"

STEP "Esperando Postgres..."
for i in $(seq 1 30); do
    if RUN docker exec "$DB_CONTAINER" pg_isready -U odoo >/dev/null 2>&1; then
        break
    fi
    sleep 2
done

# Only initialize a fresh DB. If the Odoo DB was already initialized
# (e.g. a previous deploy got this far), skip to avoid clobbering.
INITIALIZED=$(RUN_SH "docker exec $DB_CONTAINER psql -U odoo -d $DB_NAME -tAc \\"SELECT to_regclass('public.res_users') IS NOT NULL\\" 2>/dev/null" | tr -d '[:space:]')
if [ "$INITIALIZED" != "t" ]; then
    STEP "Inicializando DB Odoo (base)..."
    # Stop the running odoo worker so the init process can bind freely.
    RUN_SH "cd $COMPOSE_DIR && docker compose stop odoo" >> "$LOG" 2>&1 || true
    RUN_SH "docker run --rm --network ${{STACK}}_default \\
        -v $COMPOSE_DIR/odoo.conf:/etc/odoo/odoo.conf:ro \\
        -v pmb-odoo-core-$ODOO_VER:/mnt/odoo:ro \\
        -v $ADDONS_HOST:/mnt/addons:ro \\
        -v ${{STACK}}_filestore:/var/lib/odoo \\
        $IMAGE python /mnt/odoo/odoo-bin -c /etc/odoo/odoo.conf \\
        -d $DB_NAME -i base --stop-after-init --no-http --without-demo=all" >> "$LOG" 2>&1 \\
        || FAIL "odoo init -i base failed"
    STEP "Arrancando Odoo worker..."
    RUN_SH "cd $COMPOSE_DIR && docker compose up -d odoo" >> "$LOG" 2>&1 \\
        || FAIL "restart odoo after init failed"
fi

# ----- Phase 4: Nginx vhost + certbot -----------------------------------
STEP "Configurando nginx ($DOMAIN)..."
PUT "$STAGING/nginx.vhost" "/tmp/pmb.vhost.$$" >> "$LOG" 2>&1 || FAIL "PUT nginx"
RUN_SH "sudo mv /tmp/pmb.vhost.$$ {NGINX_SITES_DIR}/$DOMAIN && sudo nginx -t" >> "$LOG" 2>&1 \\
    || FAIL "nginx config invalid"
RUN_SH "sudo systemctl reload nginx" >> "$LOG" 2>&1 || true

STEP "Obteniendo SSL ($DOMAIN)..."
RUN_SH "sudo certbot --nginx -d $DOMAIN --non-interactive --agree-tos --email admin@patchmybyte.com --redirect" >> "$LOG" 2>&1 || echo "SSL skipped (HTTP still works)" >> "$LOG"

# ----- Phase 5: Wait for Odoo, verify ------------------------------------
STEP "Esperando Odoo..."
for i in $(seq 1 60); do
    HTTP=$(RUN_SH "curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:$INSTANCE_PORT/web/login --max-time 5" 2>/dev/null || echo 000)
    if [ "$HTTP" = "200" ] || [ "$HTTP" = "303" ] || [ "$HTTP" = "302" ]; then
        break
    fi
    sleep 5
done

STEP "Verificando $DOMAIN..."
H1=$(RUN_SH "curl -sk -o /dev/null -w '%{{http_code}}' https://$DOMAIN/web/login --max-time 15" 2>/dev/null || echo 000)
if [ "$H1" = "200" ] || [ "$H1" = "303" ] || [ "$H1" = "302" ]; then
    psql -q "$DB" -c "UPDATE devops_instance SET state='running', creation_step='Listo (HTTPS).', creation_pid=0 WHERE id=$ID;"
    echo "=== OK https=$H1 ===" >> "$LOG"
    exit 0
fi
H2=$(RUN_SH "curl -s -o /dev/null -w '%{{http_code}}' http://$DOMAIN/web/login --max-time 15" 2>/dev/null || echo 000)
if [ "$H2" = "200" ] || [ "$H2" = "303" ] || [ "$H2" = "302" ]; then
    psql -q "$DB" -c "UPDATE devops_instance SET state='running', creation_step='Listo (HTTP).', creation_pid=0 WHERE id=$ID;"
    echo "=== OK http=$H2 ===" >> "$LOG"
    exit 0
fi
H3=$(RUN_SH "curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:$INSTANCE_PORT/web/login --max-time 15" 2>/dev/null || echo 000)
if [ "$H3" = "200" ] || [ "$H3" = "303" ] || [ "$H3" = "302" ]; then
    psql -q "$DB" -c "UPDATE devops_instance SET state='running', creation_step='Listo (port only).', creation_pid=0 WHERE id=$ID;"
    echo "=== OK port=$H3 ===" >> "$LOG"
    exit 0
fi
FAIL "HTTP verify failed (https=$H1 http=$H2 port=$H3)"
"""
        return script


class DevopsProjectDocker(models.Model):
    _inherit = 'devops.project'

    # No-op: provisioning runs inside the deploy script now. Kept only
    # so legacy callers that called project._ensure_docker_host() don't
    # KeyError.

    def _ensure_docker_host(self):
        """Deprecated: provisioning moved into the per-instance deploy
        script (devops_instance_docker._build_deploy_script). This remains
        as a no-op shim for backward compatibility."""
        return True
