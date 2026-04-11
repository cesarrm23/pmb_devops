"""Infrastructure pipeline implementation for devops.instance.

Extends devops.instance with actual create/start/stop/restart/destroy
logic using system commands through infra_utils.
"""
import logging
import os
import subprocess
import time

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..utils import infra_utils

_logger = logging.getLogger(__name__)


class DevopsInstanceInfra(models.Model):
    _inherit = 'devops.instance'

    # ------------------------------------------------------------------
    # Action: Create Instance (background pipeline)
    # ------------------------------------------------------------------

    def action_create_instance(self):
        """Start instance creation in background.

        Validates limits, assigns port/names, writes initial infra fields,
        then spawns a background thread for the heavy work (DB clone, etc.)
        and returns immediately so the HTTP request is not blocked.
        """
        self.ensure_one()
        project = self.project_id

        # ---- Step 1: Validate limits ----
        self._validate_instance_limits()

        # ---- Step 2: Assign ports ----
        port = self._find_free_port()
        gevent_port = port + 1000

        # ---- Step 3: Generate names ----
        safe_name = self.name.replace(' ', '-').lower()
        service_name = f"odoo-{project.name.replace(' ', '').lower()}-{safe_name}"
        db_name = f"{project.name.replace(' ', '_').lower()}_{safe_name}"
        if self.instance_type == 'production':
            subdomain = ''
        else:
            subdomain = f"{safe_name}"
        instance_path = f"/opt/instances/{service_name}"
        config_path = f"/etc/{service_name}.conf"
        domain = project.domain or ''
        if subdomain and domain:
            full_domain = f"{subdomain}.{domain}"
        elif domain:
            full_domain = domain
        else:
            raise UserError(
                _("El proyecto no tiene dominio configurado.")
            )
        nginx_path = f"/etc/nginx/sites-enabled/{full_domain}"

        # Determine git branch name
        git_branch = safe_name  # default: instance name as branch
        if self.instance_type == 'staging':
            git_branch = 'staging' if safe_name == 'staging-1' else safe_name
        elif self.instance_type == 'production':
            git_branch = project.production_branch or 'main'

        # Write names to record
        self.write({
            'port': port,
            'gevent_port': gevent_port,
            'service_name': service_name,
            'database_name': db_name,
            'subdomain': subdomain,
            'odoo_config_path': config_path,
            'instance_path': instance_path,
            'nginx_config_path': nginx_path,
            'git_branch': git_branch,
            'state': 'creating',
            'creation_step': 'Iniciando...',
        })
        self.env.cr.commit()  # persist so SPA can see the record immediately

        # Spawn background bash script for the heavy pipeline
        instance_id = self.id
        dbname = self.env.cr.dbname
        self._launch_creation_script(instance_id, dbname)
        _logger.info("Background creation script launched for instance %s", instance_id)
        # Return immediately -- SPA will poll status

    def _launch_creation_script(self, instance_id, dbname):
        """Launch a background bash script that runs the creation pipeline.

        Uses psql to update creation_step so the SPA can poll progress.
        This avoids the complexity of spawning an Odoo process.
        """
        rec = self.browse(instance_id)
        project = rec.project_id

        # Determine clone source DB and filestore
        source_db = ''
        source_filestore = ''
        source_instance = None
        if rec.cloned_from_id and rec.cloned_from_id.database_name:
            source_instance = rec.cloned_from_id
        elif project.production_instance_id and project.production_instance_id.database_name:
            source_instance = project.production_instance_id

        if source_instance:
            source_db = source_instance.database_name
            source_filestore = f"{source_instance.instance_path}/.local/share/Odoo/filestore/{source_instance.database_name}"
        elif project.database_name:
            source_db = project.database_name

        # Build addons path using instance-local paths
        # Enterprise is only included if configured on the project
        inst_path = rec.instance_path
        addons_path = f"{inst_path}/odoo/odoo/addons,{inst_path}/odoo/addons"
        if project.enterprise_path:
            addons_path += f",{inst_path}/enterprise"
        addons_path += f",/opt/odooAL/custom_addons"
        addons_path += f",{inst_path}/cremara_addons"

        # Git repo URL for cloning into instance
        repo_url = project.repo_url or ''
        if not repo_url and project.repo_path:
            try:
                from ..utils import ssh_utils
                result = ssh_utils.execute_command(project, ['git', 'remote', 'get-url', 'origin'], cwd=project.repo_path)
                if result.returncode == 0:
                    repo_url = result.stdout.strip()
            except Exception:
                pass

        enterprise_path = project.enterprise_path or ''

        script = f"""#!/bin/bash
set -euo pipefail
ID={instance_id}
DB="{dbname}"
STEP() {{ psql -q "$DB" -c "UPDATE devops_instance SET creation_step='$1', creation_pid=$$ WHERE id=$ID;" 2>/dev/null; }}
FAIL() {{ psql -q "$DB" -c "UPDATE devops_instance SET state='error', creation_step='Error: $1', creation_pid=0 WHERE id=$ID;" 2>/dev/null; exit 1; }}

# Save PID immediately
psql -q "$DB" -c "UPDATE devops_instance SET creation_pid=$$ WHERE id=$ID;" 2>/dev/null

exec >> /var/log/odoo/pmb_creation.log 2>&1
echo "=== Creating instance {rec.name} (id=$ID) pid=$$ at $(date) ==="

# Step 1: Clone database — idempotent: skip if DB already has Odoo tables
STEP "Clonando base de datos ({source_db})..."
if [ -n "{source_db}" ]; then
    DB_READY=0
    if psql -q "{rec.database_name}" -c "SELECT 1 FROM ir_module_module LIMIT 1" 2>/dev/null | grep -q 1; then
        DB_READY=1
    fi
    if [ "$DB_READY" -eq 0 ]; then
        # Kill any lingering connections to the target DB
        psql -q "{dbname}" -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{rec.database_name}' AND pid != pg_backend_pid();" 2>/dev/null
        sleep 1
        dropdb --if-exists "{rec.database_name}" 2>/dev/null
        createdb -O odooal "{rec.database_name}" || FAIL "createdb failed"
        set +e
        pg_dump "{source_db}" 2>/dev/null | psql -q "{rec.database_name}" 2>/dev/null
        PG_EXIT=${{PIPESTATUS[0]}}
        set -e
        if [ "$PG_EXIT" -ne 0 ]; then
            FAIL "pg_dump failed (exit $PG_EXIT)"
        fi
    else
        echo "DB {rec.database_name} already has data, skipping clone"
    fi
fi

# Step 1b: Copy filestore — idempotent: skip if dir already has files
if [ -n "{source_filestore}" ] && [ -d "{source_filestore}" ]; then
    FSDEST="{rec.instance_path}/.local/share/Odoo/filestore/{rec.database_name}"
    if [ ! -d "$FSDEST" ] || [ -z "$(ls -A "$FSDEST" 2>/dev/null)" ]; then
        STEP "Copiando filestore..."
        sudo mkdir -p "$FSDEST"
        sudo rsync -a "{source_filestore}/" "$FSDEST/" || echo "WARNING: filestore copy failed"
        sudo chown -R odooal:odooal "{rec.instance_path}/.local/share/Odoo"
    else
        echo "Filestore already exists, skipping"
    fi
fi

# Step 2: Create directory + copy odoo/enterprise + clone repo
STEP "Preparando directorio de instancia..."
sudo mkdir -p "{inst_path}/.local/share/Odoo"
sudo chown -R odooal:odooal "{inst_path}"

# Copy Odoo source — idempotent: skip if odoo-bin exists
if [ ! -f "{inst_path}/odoo/odoo-bin" ]; then
    STEP "Copiando Odoo source..."
    rsync -a --exclude='__pycache__' /opt/odooAL/odoo/ "{inst_path}/odoo/"
else
    echo "Odoo source already exists, skipping"
fi

# Copy enterprise addons — only if project has enterprise configured
if [ -n "{enterprise_path}" ] && [ -d "{enterprise_path}" ]; then
    if [ ! -d "{inst_path}/enterprise/account" ]; then
        STEP "Copiando enterprise addons..."
        sudo rsync -a --exclude='__pycache__' "{enterprise_path}/" "{inst_path}/enterprise/"
        sudo chown -R odooal:odooal "{inst_path}/enterprise"
    else
        echo "Enterprise addons already exist, skipping"
    fi
else
    echo "Enterprise no configurado para este proyecto, saltando"
fi

# Symlink venv (shared, read-only executables)
ln -sfn /opt/odooAL/.venv "{inst_path}/.venv"

# Clone custom addons repo — idempotent: skip if dir exists
if [ -n "{repo_url}" ] && [ ! -d "{inst_path}/cremara_addons/.git" ]; then
    STEP "Clonando repositorio de addons..."
    rm -rf "{inst_path}/cremara_addons" 2>/dev/null
    git clone "{repo_url}" "{inst_path}/cremara_addons" --branch staging --single-branch 2>/dev/null || \
    git clone "{repo_url}" "{inst_path}/cremara_addons" 2>/dev/null || \
    echo "WARNING: repo clone failed"
else
    echo "Addons repo already cloned, skipping"
fi

# Enforce .gitignore in cloned repo (remove tracked .pyc etc.)
if [ -d "{inst_path}/cremara_addons/.git" ]; then
    STEP "Aplicando .gitignore..."
    python3 -c "
import sys; sys.path.insert(0, '/opt/odooAL/custom_addons/pmb_devops')
from utils.git_utils import ensure_gitignore
ensure_gitignore('{inst_path}/cremara_addons')
" 2>/dev/null || echo "WARNING: .gitignore enforcement skipped"
fi

# Step 3: Odoo config
STEP "Generando configuración Odoo..."
sudo tee "{rec.odoo_config_path}" > /dev/null << 'CONF'
[options]
addons_path = {addons_path}
admin_passwd = False
data_dir = {rec.instance_path}/.local/share/Odoo
db_name = {rec.database_name}
db_user = odooal
http_port = {rec.port}
gevent_port = {rec.gevent_port}
list_db = False
log_handler = :INFO
logfile = /var/log/odoo/{rec.service_name}.log
max_cron_threads = 1
proxy_mode = True
server_wide_modules = base,web
workers = 2
without_demo = True
CONF

# Step 4: Systemd service
STEP "Creando servicio systemd..."
sudo tee "/etc/systemd/system/{rec.service_name}.service" > /dev/null << 'SVC'
[Unit]
Description=Odoo {rec.service_name}
After=network.target postgresql.service
[Service]
Type=simple
User=odooal
Group=odooal
ExecStart={inst_path}/.venv/bin/python {inst_path}/odoo/odoo-bin -c {rec.odoo_config_path}
WorkingDirectory={rec.instance_path}
Restart=on-failure
RestartSec=5s
LimitNOFILE=65535
Environment=PYTHONUNBUFFERED=1
[Install]
WantedBy=multi-user.target
SVC
sudo systemctl daemon-reload
sudo systemctl enable "{rec.service_name}"

# Step 5: Nginx (HTTP only first — certbot adds SSL later)
STEP "Configurando Nginx..."
DOMAIN="{rec.full_domain or f'{rec.subdomain}.{project.domain}'}"
sudo tee "/etc/nginx/sites-enabled/$DOMAIN" > /dev/null << NGINX
upstream odoo_{instance_id} {{
  server 127.0.0.1:{rec.port};
}}
upstream odoochat_{instance_id} {{
  server 127.0.0.1:{rec.gevent_port};
}}
server {{
  listen 80;
  server_name $DOMAIN;
  proxy_read_timeout 720s;
  proxy_connect_timeout 720s;
  proxy_send_timeout 720s;
  location /websocket {{
    proxy_pass http://odoochat_{instance_id};
    proxy_set_header Upgrade \\$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header X-Forwarded-Host \\$http_host;
    proxy_set_header X-Forwarded-For \\$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \\$scheme;
    proxy_set_header X-Real-IP \\$remote_addr;
  }}
  location / {{
    proxy_set_header X-Forwarded-Host \\$host;
    proxy_set_header X-Forwarded-For \\$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \\$scheme;
    proxy_set_header X-Real-IP \\$remote_addr;
    proxy_redirect off;
    proxy_http_version 1.1;
    proxy_pass http://odoo_{instance_id};
  }}
  gzip on;
  gzip_types text/css text/plain application/xml application/json application/javascript;
  client_max_body_size 100M;
}}
NGINX
sudo nginx -t || FAIL "nginx config invalid"
sudo systemctl reload nginx

# Step 6: Start service
STEP "Iniciando servicio Odoo..."
sudo systemctl start "{rec.service_name}"
sleep 5

# Step 7: SSL
STEP "Obteniendo certificado SSL..."
sudo certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --email admin@patchmybyte.com --redirect 2>&1 || echo "SSL cert failed (HTTP still works)"

# Step 8: Verify
STEP "Verificando..."
HTTP=$(curl -sk -o /dev/null -w '%{{http_code}}' "https://$DOMAIN/web/login" --max-time 15 2>/dev/null || echo "000")
if [ "$HTTP" = "200" ] || [ "$HTTP" = "303" ] || [ "$HTTP" = "302" ]; then
    psql -q "$DB" -c "UPDATE devops_instance SET state='running', creation_step='', creation_pid=0 WHERE id=$ID;"
    echo "=== Instance {rec.name} created successfully (HTTP $HTTP) ==="
else
    # Try HTTP (no SSL)
    HTTP2=$(curl -sk -o /dev/null -w '%{{http_code}}' "http://$DOMAIN/web/login" --max-time 15 2>/dev/null || echo "000")
    if [ "$HTTP2" = "200" ] || [ "$HTTP2" = "303" ] || [ "$HTTP2" = "302" ]; then
        psql -q "$DB" -c "UPDATE devops_instance SET state='running', creation_step='', creation_pid=0 WHERE id=$ID;"
        echo "=== Instance {rec.name} created (HTTP $HTTP2, no SSL) ==="
    else
        FAIL "HTTP verify failed ($HTTP / $HTTP2)"
    fi
fi
"""
        # Write script and execute
        script_path = f"/tmp/pmb_create_{instance_id}.sh"
        with open(script_path, 'w') as f:
            f.write(script)
        os.chmod(script_path, 0o755)

        subprocess.Popen(
            ['/bin/bash', script_path],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _update_step(self, step_text):
        """Update creation_step using commit so it's visible to the SPA immediately."""
        self.env.cr.commit()  # commit current work
        self.write({'creation_step': step_text})
        self.env.cr.commit()  # commit the status update

    def _run_creation_pipeline(self):
        """Execute all creation steps sequentially.

        This runs inside a background thread with its own cursor.
        Each step commits progress so the SPA can poll and display it.
        """
        self.ensure_one()
        project = self.project_id
        errors = []

        try:
            # ---- Step 1: Create git branch if needed ----
            if self.branch_id and project.repo_path:
                self._update_step('Configurando rama git...')
                try:
                    from ..utils import git_utils
                    git_utils.git_fetch(project)
                    _logger.info(
                        "Branch %s assigned to instance %s",
                        self.branch_id.name, self.name,
                    )
                except Exception as e:
                    _logger.warning("Git branch setup skipped: %s", e)

            # ---- Step 2: Clone database ----
            source_db = None
            if self.cloned_from_id and self.cloned_from_id.database_name:
                source_db = self.cloned_from_id.database_name
            elif project.database_name:
                source_db = project.database_name

            if source_db:
                self._update_step(
                    f'Clonando base de datos ({source_db})...'
                )
                _logger.info(
                    "Cloning database %s -> %s",
                    source_db, self.database_name,
                )
                infra_utils.clone_database(
                    source_db, self.database_name, timeout=1800,
                )
            else:
                self._update_step('Creando base de datos vacía...')
                _logger.warning(
                    "No source database for cloning; "
                    "instance %s will start with empty database", self.name,
                )

            # ---- Step 3: Create instance directory + symlink ----
            self._update_step('Creando directorio de instancia...')
            infra_utils.create_instance_directory(self.instance_path)
            if project.repo_path:
                infra_utils.sudo_run(
                    f"ln -sfn {project.repo_path} {self.instance_path}/addons"
                )

            # ---- Step 4: Generate Odoo config ----
            self._update_step('Generando configuración Odoo...')
            addons_path = f"{self.instance_path}/addons"
            infra_utils.create_odoo_config(
                service_name=self.service_name,
                db_name=self.database_name,
                port=self.port,
                gevent_port=self.gevent_port,
                instance_path=self.instance_path,
                addons_path=addons_path,
            )

            # ---- Step 5: Create systemd service ----
            self._update_step('Creando servicio systemd...')
            infra_utils.create_systemd_service(
                service_name=self.service_name,
                config_path=self.odoo_config_path,
                instance_path=self.instance_path,
            )

            # ---- Step 6: Create nginx vhost ----
            self._update_step('Configurando Nginx...')
            infra_utils.create_nginx_vhost(
                domain=self.full_domain,
                port=self.port,
                gevent_port=self.gevent_port,
                instance_id=self.id,
                instance_name=self.service_name,
            )

            # ---- Step 7: Reload nginx ----
            infra_utils.reload_nginx()

            # ---- Step 8: Start service ----
            self._update_step('Iniciando servicio Odoo...')
            infra_utils.start_service(self.service_name, timeout=30)
            time.sleep(5)

            # ---- Step 9: SSL certificate ----
            self._update_step('Obteniendo certificado SSL...')
            try:
                infra_utils.obtain_ssl_cert(self.full_domain)
            except Exception as e:
                errors.append(f"SSL certificate: {e}")
                _logger.warning(
                    "SSL cert failed for %s: %s", self.full_domain, e,
                )

            # ---- Step 10: Verify HTTP ----
            self._update_step('Verificando...')
            http_ok = False
            try:
                verify = subprocess.run(
                    [
                        'curl', '-s', '-o', '/dev/null', '-w',
                        '%{http_code}', '-k',
                        f'https://{self.full_domain}/web/login',
                        '--max-time', '15',
                    ],
                    capture_output=True, text=True, timeout=20,
                )
                if verify.stdout.strip() in ('200', '303', '302'):
                    http_ok = True
                else:
                    errors.append(
                        f"HTTP check returned {verify.stdout.strip()}"
                    )
            except Exception as e:
                errors.append(f"HTTP verify: {e}")

            # ---- Step 11: Update state ----
            if http_ok:
                self.write({'state': 'running', 'creation_step': ''})
            else:
                self.write({
                    'state': 'error',
                    'creation_step': (
                        f'HTTP {verify.stdout.strip()}'
                        if not errors
                        else errors[-1]
                    ),
                })

            self._update_activity()

            # ---- Step 12: Post chatter log ----
            body = _(
                "<b>Instancia creada</b><br/>"
                "Puerto: %(port)s | Gevent: %(gevent)s<br/>"
                "BD: %(db)s<br/>"
                "Dominio: %(domain)s<br/>"
                "Servicio: %(service)s<br/>",
                port=self.port,
                gevent=self.gevent_port,
                db=self.database_name,
                domain=self.full_domain,
                service=self.service_name,
            )
            if errors:
                body += _(
                    "<br/><b>Advertencias:</b><br/>%s"
                ) % '<br/>'.join(errors)
            self.message_post(body=body)
            self.env.cr.commit()
            _logger.info("Instance %s created successfully", self.name)

        except Exception as e:
            _logger.exception("Creation pipeline failed for %s", self.name)
            self.write({
                'state': 'error',
                'creation_step': f'Error: {str(e)[:200]}',
            })
            self.message_post(
                body=_("<b>Error creando instancia:</b><br/>%s") % str(e),
            )
            self.env.cr.commit()

    # ------------------------------------------------------------------
    # Limit validation
    # ------------------------------------------------------------------

    def _validate_instance_limits(self):
        """Check that project instance limits are not exceeded."""
        project = self.project_id
        if self.instance_type == 'staging':
            current = self.search_count([
                ('project_id', '=', project.id),
                ('instance_type', '=', 'staging'),
                ('state', 'not in', ['destroying']),
                ('id', '!=', self.id),
            ])
            if current >= project.max_staging:
                raise UserError(
                    _("Límite de instancias staging alcanzado (%d/%d).") % (
                        current, project.max_staging,
                    )
                )
        elif self.instance_type == 'development':
            current = self.search_count([
                ('project_id', '=', project.id),
                ('instance_type', '=', 'development'),
                ('state', 'not in', ['destroying']),
                ('id', '!=', self.id),
            ])
            if current >= project.max_development:
                raise UserError(
                    _("Límite de instancias development alcanzado (%d/%d).") % (
                        current, project.max_development,
                    )
                )

    # ------------------------------------------------------------------
    # Action: Start
    # ------------------------------------------------------------------

    def action_start(self):
        """Start the instance's systemd service."""
        self.ensure_one()
        if not self.service_name:
            raise UserError(_("No hay servicio systemd configurado."))

        try:
            infra_utils.start_service(self.service_name)
        except Exception as e:
            raise UserError(_("Error iniciando servicio: %s") % str(e))

        self._check_service_status()
        self._update_activity()
        self.message_post(
            body=_("Servicio '%s' iniciado.") % self.service_name,
        )

    # ------------------------------------------------------------------
    # Action: Stop
    # ------------------------------------------------------------------

    def action_stop(self):
        """Stop the instance's systemd service.
        Production instances cannot be stopped.
        """
        self.ensure_one()
        if self.instance_type == 'production':
            raise UserError(
                _("No se puede detener una instancia de producción. "
                  "Use reiniciar en su lugar.")
            )
        if not self.service_name:
            raise UserError(_("No hay servicio systemd configurado."))

        try:
            infra_utils.stop_service(self.service_name)
        except Exception as e:
            raise UserError(_("Error deteniendo servicio: %s") % str(e))

        self._check_service_status()
        self._update_activity()
        self.message_post(
            body=_("Servicio '%s' detenido.") % self.service_name,
        )

    # ------------------------------------------------------------------
    # Action: Restart
    # ------------------------------------------------------------------

    def action_restart(self):
        """Restart the instance's systemd service."""
        self.ensure_one()
        if not self.service_name:
            raise UserError(_("No hay servicio systemd configurado."))

        try:
            infra_utils.restart_service(self.service_name)
        except Exception as e:
            raise UserError(_("Error reiniciando servicio: %s") % str(e))

        self._check_service_status()
        self._update_activity()
        self.message_post(
            body=_("Servicio '%s' reiniciado.") % self.service_name,
        )

    # ------------------------------------------------------------------
    # Action: Destroy
    # ------------------------------------------------------------------

    def action_destroy(self):
        """Full destruction pipeline for an instance.

        Production instances cannot be destroyed.

        Steps:
        1. Stop & disable systemd service
        2. Drop database
        3. Remove Odoo config
        4. Remove nginx vhost
        5. Remove instance directory
        6. Delete the record
        """
        self.ensure_one()
        if self.instance_type == 'production':
            raise UserError(
                _("No se puede destruir una instancia de producción.")
            )

        self.write({'state': 'destroying'})
        self.env.cr.commit()

        errors = []

        # 1. Stop & remove systemd service
        if self.service_name:
            try:
                infra_utils.remove_systemd_service(self.service_name)
            except Exception as e:
                errors.append(f"Systemd: {e}")
                _logger.warning("Error removing service %s: %s", self.service_name, e)

        # 2. Drop database
        if self.database_name:
            try:
                infra_utils.drop_database(self.database_name)
            except Exception as e:
                errors.append(f"Database: {e}")
                _logger.warning("Error dropping database %s: %s", self.database_name, e)

        # 3. Remove Odoo config
        if self.odoo_config_path:
            try:
                infra_utils.sudo_run(f"rm -f {self.odoo_config_path}")
            except Exception as e:
                errors.append(f"Config: {e}")

        # 4. Remove nginx vhost
        if self.nginx_config_path:
            try:
                infra_utils.remove_nginx_vhost(self.nginx_config_path)
            except Exception as e:
                errors.append(f"Nginx: {e}")
                _logger.warning(
                    "Error removing nginx config %s: %s",
                    self.nginx_config_path, e,
                )

        # 5. Remove instance directory
        if self.instance_path:
            try:
                infra_utils.remove_instance_directory(self.instance_path)
            except Exception as e:
                errors.append(f"Directory: {e}")
                _logger.warning(
                    "Error removing instance directory %s: %s",
                    self.instance_path, e,
                )

        # 6. Delete associated branch
        if self.branch_id:
            try:
                self.branch_id.unlink()
            except Exception as e:
                errors.append(f"Branch: {e}")

        # Log before deleting the record
        instance_name = self.name
        project = self.project_id

        if errors:
            _logger.warning(
                "Instance %s destroyed with errors: %s",
                instance_name, '; '.join(errors),
            )

        # 6. Delete the record
        self.unlink()

        # Post to project chatter
        if project:
            body = _(
                "<b>Instancia '%s' destruida.</b>",
                instance_name,
            )
            if errors:
                body += _("<br/><b>Errores:</b><br/>%s") % '<br/>'.join(errors)
            project.message_post(body=body)

        _logger.info("Instance %s fully destroyed", instance_name)

    # ------------------------------------------------------------------
    # Status check
    # ------------------------------------------------------------------

    def _check_service_status(self):
        """Check systemd service status and update state field."""
        for rec in self:
            if not rec.service_name:
                continue
            status = infra_utils.is_service_active(rec.service_name)
            if status == 'active':
                rec.state = 'running'
            elif status in ('inactive', 'deactivating'):
                rec.state = 'stopped'
            else:
                rec.state = 'error'

    # ---- Cron methods ----

    @api.model
    def _cron_auto_stop(self):
        """Stop staging/dev instances with >1h inactivity."""
        from datetime import timedelta
        cutoff = fields.Datetime.now() - timedelta(hours=1)
        instances = self.search([
            ('instance_type', 'in', ['staging', 'development']),
            ('state', '=', 'running'),
            ('last_activity', '<', cutoff),
        ])
        for inst in instances:
            try:
                inst.action_stop()
            except Exception as e:
                _logger.warning("Auto-stop failed for %s: %s", inst.name, e)

    @api.model
    def _cron_auto_destroy(self):
        """Destroy stopped development instances after configured hours."""
        from datetime import timedelta
        instances = self.search([
            ('instance_type', '=', 'development'),
            ('state', '=', 'stopped'),
        ])
        for inst in instances:
            hours = inst.project_id.auto_destroy_hours
            if hours is None:
                hours = 24
            if hours <= 0:
                continue  # 0 = never auto-destroy
            cutoff = fields.Datetime.now() - timedelta(hours=hours)
            if inst.last_activity and inst.last_activity < cutoff:
                try:
                    inst.action_destroy()
                except Exception as e:
                    _logger.warning("Auto-destroy failed for %s: %s", inst.name, e)

    @api.model
    def _cron_creation_watchdog(self):
        """Detect stuck 'creating' instances and relaunch their scripts.

        Runs every 2 minutes. If an instance is in 'creating' state but
        its creation_pid is dead (process no longer exists), relaunches
        the creation script. The script is idempotent so it will skip
        already-completed steps.
        """
        stuck = self.search([('state', '=', 'creating')])
        for inst in stuck:
            pid = inst.creation_pid
            if pid and pid > 0:
                # Check if process is still alive
                try:
                    os.kill(pid, 0)  # signal 0 = check existence
                    continue  # process alive, nothing to do
                except ProcessLookupError:
                    pass  # process dead, need to relaunch
                except PermissionError:
                    continue  # process exists but different user

            # Process is dead or no PID — relaunch
            script_path = f"/tmp/pmb_create_{inst.id}.sh"
            if not os.path.exists(script_path):
                # Script gone (server rebooted?) — regenerate it
                _logger.info(
                    "Watchdog: regenerating creation script for %s (id=%s)",
                    inst.name, inst.id,
                )
                dbname = self.env.cr.dbname
                inst._launch_creation_script(inst.id, dbname)
            else:
                _logger.info(
                    "Watchdog: relaunching creation script for %s (id=%s, old pid=%s)",
                    inst.name, inst.id, pid,
                )
                subprocess.Popen(
                    ['/bin/bash', script_path],
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

    @api.model
    def _cron_health_check(self):
        """Check service status of all running/error instances."""
        instances = self.search([
            ('state', 'in', ['running', 'error']),
            ('service_name', '!=', False),
        ])
        instances._check_service_status()
