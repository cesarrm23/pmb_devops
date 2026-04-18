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
        # Use subdomain_base for staging/dev if configured, else fall back to domain
        domain = project.domain or ''
        sub_base = project.subdomain_base or domain
        if subdomain and sub_base:
            full_domain = f"{subdomain}.{sub_base}"
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

        For local projects: uses psql to update creation_step directly.
        For SSH projects: runs script on remote server via SCP + nohup,
        writes status/log to files, polled via SSH by the SPA.
        """
        rec = self.browse(instance_id)
        project = rec.project_id
        is_ssh = project.connection_type == 'ssh' and project.ssh_host

        if is_ssh:
            self._launch_creation_script_ssh(rec, project)
        else:
            self._launch_creation_script_local(rec, project, dbname)

    def _launch_creation_script_local(self, rec, project, dbname):
        """Launch creation script locally (original implementation)."""
        instance_id = rec.id

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

        inst_path = rec.instance_path
        addons_path = f"{inst_path}/odoo/odoo/addons,{inst_path}/odoo/addons"
        if project.enterprise_path:
            addons_path += f",{inst_path}/enterprise"
        addons_path += f",/opt/odooAL/custom_addons"
        addons_path += f",{inst_path}/cremara_addons"

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

        # Detect OS/DB user from production service
        db_user = 'odooal'  # default for local
        prod_svc = project.odoo_service_name or ''
        if prod_svc:
            try:
                import subprocess as _sp
                r = _sp.run(
                    ['systemctl', 'show', prod_svc, '-p', 'User', '--value'],
                    capture_output=True, text=True, timeout=5,
                )
                u = r.stdout.strip()
                if u:
                    db_user = u
            except Exception:
                pass

        script = f"""#!/bin/bash
set -euo pipefail
ID={instance_id}
DB="{dbname}"
STEP() {{ psql -q "$DB" -c "UPDATE devops_instance SET creation_step='$1', creation_pid=$$ WHERE id=$ID;" 2>/dev/null; }}
FAIL() {{ psql -q "$DB" -c "UPDATE devops_instance SET state='error', creation_step='Error: $1', creation_pid=0 WHERE id=$ID;" 2>/dev/null; exit 1; }}

psql -q "$DB" -c "UPDATE devops_instance SET creation_pid=$$ WHERE id=$ID;" 2>/dev/null

exec >> /var/log/odoo/pmb_creation.log 2>&1
echo "=== Creating instance {rec.name} (id=$ID) pid=$$ at $(date) ==="

STEP "Clonando base de datos ({source_db})..."
if [ -n "{source_db}" ]; then
    DB_READY=0
    if psql -q "{rec.database_name}" -c "SELECT 1 FROM ir_module_module LIMIT 1" 2>/dev/null | grep -q 1; then
        DB_READY=1
    fi
    if [ "$DB_READY" -eq 0 ]; then
        psql -q "{dbname}" -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{rec.database_name}' AND pid != pg_backend_pid();" 2>/dev/null
        sleep 1
        dropdb --if-exists "{rec.database_name}" 2>/dev/null
        createdb -O {db_user} "{rec.database_name}" || FAIL "createdb failed"
        set +e
        pg_dump "{source_db}" 2>/dev/null | psql -q "{rec.database_name}" 2>/dev/null
        PG_EXIT=${{PIPESTATUS[0]}}
        set -e
        if [ "$PG_EXIT" -ne 0 ]; then
            FAIL "pg_dump failed (exit $PG_EXIT)"
        fi
        # Grant all privileges to the instance DB user
        psql -q "{rec.database_name}" -c "
            GRANT ALL ON ALL TABLES IN SCHEMA public TO {db_user};
            GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO {db_user};
            GRANT ALL ON ALL FUNCTIONS IN SCHEMA public TO {db_user};
            ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {db_user};
            ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO {db_user};
        " 2>/dev/null || echo "WARNING: GRANT failed"
        # Post-clone: update system parameters for the new instance
        STEP "Configurando parametros de instancia..."
        psql -q "{rec.database_name}" -c "
            UPDATE ir_config_parameter SET value = 'https://{rec.full_domain}' WHERE key = 'web.base.url';
            UPDATE ir_config_parameter SET value = 'https://{rec.full_domain}' WHERE key = 'report.url';
            UPDATE ir_config_parameter SET value = '{rec.database_name}' WHERE key = 'database.name';
            DELETE FROM ir_config_parameter WHERE key = 'database.uuid';
            DELETE FROM ir_config_parameter WHERE key = 'database.enterprise_code';
            UPDATE ir_mail_server SET active = false;
            UPDATE fetchmail_server SET active = false WHERE active = true;
            UPDATE ir_cron SET active = false WHERE active = true AND id NOT IN (SELECT id FROM ir_cron WHERE name ILIKE '%session%' OR name ILIKE '%autovacuum%' OR name ILIKE '%clean%');
        " 2>/dev/null || echo "WARNING: post-clone SQL failed"
    else
        echo "DB {rec.database_name} already has data, skipping clone"
    fi
fi

if [ -n "{source_filestore}" ] && [ -d "{source_filestore}" ]; then
    FSDEST="{rec.instance_path}/.local/share/Odoo/filestore/{rec.database_name}"
    if [ ! -d "$FSDEST" ] || [ -z "$(ls -A "$FSDEST" 2>/dev/null)" ]; then
        STEP "Copiando filestore..."
        sudo mkdir -p "$FSDEST"
        sudo rsync -a "{source_filestore}/" "$FSDEST/" || echo "WARNING: filestore copy failed"
        sudo chown -R {db_user}:{db_user} "{rec.instance_path}/.local/share/Odoo"
    else
        echo "Filestore already exists, skipping"
    fi
fi

STEP "Preparando directorio de instancia..."
sudo mkdir -p "{inst_path}/.local/share/Odoo"
sudo chown -R {db_user}:{db_user} "{inst_path}"

if [ ! -f "{inst_path}/odoo/odoo-bin" ]; then
    STEP "Copiando Odoo source..."
    rsync -a --exclude='__pycache__' /opt/odooAL/odoo/ "{inst_path}/odoo/"
else
    echo "Odoo source already exists, skipping"
fi

if [ -n "{enterprise_path}" ] && [ -d "{enterprise_path}" ]; then
    if [ ! -d "{inst_path}/enterprise/account" ]; then
        STEP "Copiando enterprise addons..."
        sudo rsync -a --exclude='__pycache__' "{enterprise_path}/" "{inst_path}/enterprise/"
        sudo chown -R {db_user}:{db_user} "{inst_path}/enterprise"
    else
        echo "Enterprise addons already exist, skipping"
    fi
fi

ln -sfn /opt/odooAL/.venv "{inst_path}/.venv"

if [ -n "{repo_url}" ] && [ ! -d "{inst_path}/cremara_addons/.git" ]; then
    STEP "Clonando repositorio de addons..."
    rm -rf "{inst_path}/cremara_addons" 2>/dev/null
    git clone "{repo_url}" "{inst_path}/cremara_addons" 2>/dev/null || \
    echo "WARNING: repo clone failed"
    # Create instance branch
    if [ -d "{inst_path}/cremara_addons/.git" ]; then
        cd "{inst_path}/cremara_addons"
        git checkout -b "{rec.git_branch}" 2>/dev/null || git checkout "{rec.git_branch}" 2>/dev/null || true
        git push -u origin "{rec.git_branch}" 2>/dev/null || true
        # Install pre-push hook to block push to protected branches
        mkdir -p .git/hooks
        cat > .git/hooks/pre-push << 'HOOKEOF'
#!/bin/bash
PROTECTED="{('main|master' if rec.instance_type == 'staging' else 'main|master|staging')}"
while read local_ref local_sha remote_ref remote_sha; do
    branch=$(echo "$remote_ref" | sed 's|refs/heads/||')
    if echo "$branch" | grep -qE "^($PROTECTED)$"; then
        echo ""; echo "  ERROR: Push a ramas protegidas bloqueado desde {rec.instance_type}."; echo "  Rama: $branch"; echo ""; exit 1
    fi
done
exit 0
HOOKEOF
        chmod +x .git/hooks/pre-push
        cd /
    fi
fi

STEP "Generando configuración Odoo..."
sudo tee "{rec.odoo_config_path}" > /dev/null << 'CONF'
[options]
addons_path = {addons_path}
admin_passwd = False
data_dir = {rec.instance_path}/.local/share/Odoo
db_name = {rec.database_name}
db_user = {db_user}
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

STEP "Creando servicio systemd..."
sudo tee "/etc/systemd/system/{rec.service_name}.service" > /dev/null << 'SVC'
[Unit]
Description=Odoo {rec.service_name}
After=network.target postgresql.service
[Service]
Type=simple
User={db_user}
Group={db_user}
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

STEP "Iniciando servicio Odoo..."
sudo systemctl start "{rec.service_name}"
sleep 5

STEP "Obteniendo certificado SSL..."
sudo certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --email admin@patchmybyte.com --redirect 2>&1 || echo "SSL cert failed (HTTP still works)"

STEP "Verificando..."
HTTP=$(curl -sk -o /dev/null -w '%{{http_code}}' "https://$DOMAIN/web/login" --max-time 15 2>/dev/null || echo "000")
if [ "$HTTP" = "200" ] || [ "$HTTP" = "303" ] || [ "$HTTP" = "302" ]; then
    psql -q "$DB" -c "UPDATE devops_instance SET state='running', creation_step='', creation_pid=0 WHERE id=$ID;"
    echo "=== Instance {rec.name} created successfully (HTTP $HTTP) ==="
else
    HTTP2=$(curl -sk -o /dev/null -w '%{{http_code}}' "http://$DOMAIN/web/login" --max-time 15 2>/dev/null || echo "000")
    if [ "$HTTP2" = "200" ] || [ "$HTTP2" = "303" ] || [ "$HTTP2" = "302" ]; then
        psql -q "$DB" -c "UPDATE devops_instance SET state='running', creation_step='', creation_pid=0 WHERE id=$ID;"
        echo "=== Instance {rec.name} created (HTTP $HTTP2, no SSL) ==="
    else
        FAIL "HTTP verify failed ($HTTP / $HTTP2)"
    fi
fi
"""
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

    def _launch_creation_script_ssh(self, rec, project):
        """Launch creation script on a remote SSH server.

        Script writes progress to files on the remote server.
        The SPA polls status via SSH from instance_poll_status.
        """
        instance_id = rec.id

        # Discover remote Odoo paths from production instance
        prod = project.production_instance_id
        prod_path = prod.instance_path if prod else project.repo_path or '/opt'
        prod_db = prod.database_name if prod else project.database_name or ''

        # Source DB for cloning
        source_db = ''
        if rec.cloned_from_id and rec.cloned_from_id.database_name:
            source_db = rec.cloned_from_id.database_name
        elif prod_db:
            source_db = prod_db

        # Source filestore
        source_filestore = ''
        source_inst = rec.cloned_from_id or prod
        if source_inst and source_inst.database_name and source_inst.instance_path:
            source_filestore = f"{source_inst.instance_path}/.local/share/Odoo/filestore/{source_inst.database_name}"

        inst_path = rec.instance_path
        enterprise_path = project.enterprise_path or ''

        # Discover Odoo source path and venv from production's systemd service
        prod_svc = project.odoo_service_name or (prod.service_name if prod else '')
        # Detect DB user from production
        db_user = 'odoo'  # default for remote
        if prod_svc:
            try:
                from ..utils import ssh_utils
                r = ssh_utils.execute_command_shell(
                    project,
                    f"systemctl show {prod_svc} -p User --value 2>/dev/null || echo odoo",
                )
                u = r.stdout.strip() if r.returncode == 0 else ''
                if u:
                    db_user = u
            except Exception:
                pass

        log_file = f"/tmp/pmb_create_{instance_id}.log"
        status_file = f"/tmp/pmb_create_{instance_id}.status"
        domain = rec.full_domain or f"{rec.subdomain}.{project.subdomain_base or project.domain}"
        prod_svc_name = prod_svc or 'odoo'

        script = f"""#!/bin/bash
set -euo pipefail
LOG="{log_file}"
STATUS="{status_file}"
PROD_PATH="{prod_path}"
INST_PATH="{inst_path}"

# Auto-detect Python and venv from production service
PROD_EXEC=$(systemctl show {prod_svc_name} -p ExecStart --value 2>/dev/null | grep -oP '\\S+python\\S*' | head -1 | sed 's/^path=//')
if [ -z "$PROD_EXEC" ]; then
    PROD_EXEC=$(grep -oP 'ExecStart=\\K\\S+' /etc/systemd/system/{prod_svc_name}.service 2>/dev/null | sed 's/^path=//' || echo "python3")
fi
PROD_VENV=$(dirname "$(dirname "$PROD_EXEC")" 2>/dev/null)
if [ ! -d "$PROD_VENV/bin" ]; then
    for V in "$PROD_PATH/venv" "$PROD_PATH/.venv" /opt/venv; do
        if [ -d "$V/bin" ]; then PROD_VENV="$V"; break; fi
    done
fi
PYTHON_BIN="$PROD_VENV/bin/python3"
if [ ! -f "$PYTHON_BIN" ]; then PYTHON_BIN="$PROD_VENV/bin/python"; fi
if [ ! -f "$PYTHON_BIN" ]; then PYTHON_BIN=$(which python3 2>/dev/null || echo "/usr/bin/python3"); fi
# Validate PYTHON_BIN is an absolute path
case "$PYTHON_BIN" in /*) ;; *) PYTHON_BIN="/usr/bin/python3";; esac

# Auto-detect addons_path from production config
PROD_ADDONS=$(grep -oP 'addons_path\\s*=\\s*\\K.*' /etc/{prod_svc_name}.conf 2>/dev/null || echo "")

STEP() {{ echo "$1" > "$STATUS"; echo "$(date +%H:%M:%S) $1" >> "$LOG"; }}
FAIL() {{ echo "error: $1" > "$STATUS"; echo "$(date +%H:%M:%S) ERROR: $1" >> "$LOG"; exit 1; }}

echo "running" > "$STATUS"
echo "=== Creating instance {rec.name} at $(date) ===" > "$LOG"

# Step 1: Clone database
STEP "Clonando base de datos ({source_db})..."
if [ -n "{source_db}" ]; then
    DB_READY=0
    if sudo -u postgres psql -q "{rec.database_name}" -c "SELECT 1 FROM ir_module_module LIMIT 1" 2>/dev/null | grep -q 1; then
        DB_READY=1
    fi
    if [ "$DB_READY" -eq 0 ]; then
        sudo -u postgres psql -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{rec.database_name}' AND pid != pg_backend_pid();" 2>/dev/null
        sleep 1
        sudo -u postgres dropdb --if-exists "{rec.database_name}" 2>/dev/null
        sudo -u postgres createdb -O {db_user} "{rec.database_name}" || FAIL "createdb failed"
        set +e
        sudo -u postgres pg_dump "{source_db}" 2>/dev/null | sudo -u postgres psql -q "{rec.database_name}" 2>/dev/null
        PG_EXIT=${{PIPESTATUS[0]}}
        set -e
        if [ "$PG_EXIT" -ne 0 ]; then
            FAIL "pg_dump failed (exit $PG_EXIT)"
        fi
        # Grant all privileges to the instance DB user
        sudo -u postgres psql -q "{rec.database_name}" -c "
            GRANT ALL ON ALL TABLES IN SCHEMA public TO {db_user};
            GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO {db_user};
            GRANT ALL ON ALL FUNCTIONS IN SCHEMA public TO {db_user};
            ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {db_user};
            ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO {db_user};
        " 2>/dev/null || echo "WARNING: GRANT failed" >> "$LOG"
        # Post-clone: update system parameters for the new instance
        STEP "Configurando parametros de instancia..."
        sudo -u postgres psql -q "{rec.database_name}" -c "
            UPDATE ir_config_parameter SET value = 'https://{domain}' WHERE key = 'web.base.url';
            UPDATE ir_config_parameter SET value = 'https://{domain}' WHERE key = 'report.url';
            UPDATE ir_config_parameter SET value = '{rec.database_name}' WHERE key = 'database.name';
            DELETE FROM ir_config_parameter WHERE key = 'database.uuid';
            DELETE FROM ir_config_parameter WHERE key = 'database.enterprise_code';
            UPDATE ir_mail_server SET active = false;
            UPDATE fetchmail_server SET active = false WHERE active = true;
            UPDATE ir_cron SET active = false WHERE active = true AND id NOT IN (SELECT id FROM ir_cron WHERE name ILIKE '%session%' OR name ILIKE '%autovacuum%' OR name ILIKE '%clean%');
        " 2>/dev/null || echo "WARNING: post-clone SQL failed" >> "$LOG"
        echo "DB cloned successfully" >> "$LOG"
    else
        echo "DB {rec.database_name} already has data, skipping" >> "$LOG"
    fi
fi

# Step 1b: Copy filestore
if [ -n "{source_filestore}" ] && [ -d "{source_filestore}" ]; then
    FSDEST="$INST_PATH/.local/share/Odoo/filestore/{rec.database_name}"
    if [ ! -d "$FSDEST" ] || [ -z "$(ls -A "$FSDEST" 2>/dev/null)" ]; then
        STEP "Copiando filestore..."
        mkdir -p "$FSDEST"
        rsync -a "{source_filestore}/" "$FSDEST/" || echo "WARNING: filestore copy failed" >> "$LOG"
        chown -R {db_user}:{db_user} "$INST_PATH/.local/share/Odoo"
    fi
fi

# Step 2: Replicate production directory structure
STEP "Preparando directorio de instancia..."
mkdir -p "$INST_PATH/.local/share/Odoo"

# Replicate each dir from production: symlinks stay as symlinks, git repos get cloned, rest is copied
INST_ADDONS=""
for ENTRY in "$PROD_PATH"/*/; do
    DIRNAME=$(basename "$ENTRY")
    [ "$DIRNAME" = ".local" ] && continue
    DEST="$INST_PATH/$DIRNAME"

    if [ -L "$PROD_PATH/$DIRNAME" ]; then
        # Symlink: replicate the symlink (shared resource like odoo, enterprise, venv)
        LINK_TARGET=$(readlink -f "$PROD_PATH/$DIRNAME")
        if [ ! -e "$DEST" ]; then
            STEP "Enlazando $DIRNAME..."
            ln -sfn "$LINK_TARGET" "$DEST"
            echo "Symlinked $DIRNAME -> $LINK_TARGET" >> "$LOG"
        fi
    elif [ -d "$PROD_PATH/$DIRNAME/.git" ]; then
        # Git repo: clone and create instance branch
        if [ ! -d "$DEST/.git" ]; then
            STEP "Clonando repositorio $DIRNAME..."
            PROD_BRANCH=$(cd "$PROD_PATH/$DIRNAME" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")
            REPO_URL=$(cd "$PROD_PATH/$DIRNAME" && git remote get-url origin 2>/dev/null || echo "")
            if [ -n "$REPO_URL" ]; then
                git clone "$REPO_URL" "$DEST" 2>/dev/null || \
                {{ echo "WARNING: clone $DIRNAME failed, copying instead" >> "$LOG"; rsync -a --exclude='__pycache__' "$PROD_PATH/$DIRNAME/" "$DEST/"; }}
            else
                rsync -a --exclude='__pycache__' "$PROD_PATH/$DIRNAME/" "$DEST/"
            fi
            # Create instance branch from production branch
            git config --global --add safe.directory "$DEST" 2>/dev/null
            if [ -d "$DEST/.git" ]; then
                cd "$DEST"
                git checkout "$PROD_BRANCH" 2>/dev/null || true
                git checkout -b "{rec.git_branch}" 2>/dev/null || git checkout "{rec.git_branch}" 2>/dev/null || true
                # Push branch to remote so it exists for merge/sync later
                if [ -n "$REPO_URL" ]; then
                    git push -u origin "{rec.git_branch}" 2>/dev/null || true
                fi
                cd /
                echo "Cloned $DIRNAME -> branch {rec.git_branch} (from $PROD_BRANCH)" >> "$LOG"
            fi
        fi
    elif [ -d "$PROD_PATH/$DIRNAME" ]; then
        # Regular directory: copy if it doesn't exist
        if [ ! -d "$DEST" ]; then
            STEP "Copiando $DIRNAME..."
            rsync -a --exclude='__pycache__' "$PROD_PATH/$DIRNAME/" "$DEST/"
            echo "Copied dir $DIRNAME" >> "$LOG"
        fi
    fi
done

# Symlink venv from production (if not already linked)
if [ ! -e "$INST_PATH/venv" ] && [ -d "$PROD_VENV" ]; then
    ln -sfn "$PROD_VENV" "$INST_PATH/venv"
fi

# Install git pre-push hooks to block push to protected branches
STEP "Instalando proteccion de ramas..."
for D in "$INST_PATH"/*/; do
    if [ -d "$D/.git/hooks" ]; then
        cat > "$D/.git/hooks/pre-push" << 'HOOKEOF'
#!/bin/bash
# PMB DevOps: branch protection for {rec.instance_type}
PROTECTED="{('main|master' if rec.instance_type == 'staging' else 'main|master|staging')}"
while read local_ref local_sha remote_ref remote_sha; do
    branch=$(echo "$remote_ref" | sed 's|refs/heads/||')
    if echo "$branch" | grep -qE "^($PROTECTED)$"; then
        echo ""
        echo "  ERROR: Push a ramas protegidas bloqueado desde {rec.instance_type}."
        echo "  Rama bloqueada: $branch"
        echo "  Solo puedes hacer push a tu propia rama."
        echo ""
        exit 1
    fi
done
exit 0
HOOKEOF
        chmod +x "$D/.git/hooks/pre-push"
    fi
done

# Set ownership
chown -R {db_user}:{db_user} "$INST_PATH"

# Step 3: Build addons_path by rewriting production paths to staging paths
STEP "Generando configuración Odoo..."
INST_ADDONS=$(echo "$PROD_ADDONS" | tr ',' '\\n' | while read -r AP; do
    AP=$(echo "$AP" | xargs)  # trim
    [ -z "$AP" ] && continue
    # Replace production base path with instance path
    echo "$AP" | sed "s|$PROD_PATH|$INST_PATH|g"
done | paste -sd ',' -)

# Fallback if detection failed
if [ -z "$INST_ADDONS" ]; then
    INST_ADDONS="$INST_PATH/odoo/odoo/addons,$INST_PATH/odoo/addons,$INST_PATH/enterprise"
fi

cat > "{rec.odoo_config_path}" << CONFEOF
[options]
addons_path = $INST_ADDONS
admin_passwd = False
data_dir = $INST_PATH/.local/share/Odoo
db_name = {rec.database_name}
db_user = {db_user}
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
CONFEOF

# Step 4: Systemd service (uses detected python path)
STEP "Creando servicio systemd..."
cat > "/etc/systemd/system/{rec.service_name}.service" << ENDSVC
[Unit]
Description=Odoo {rec.service_name}
After=network.target postgresql.service
[Service]
Type=simple
User={db_user}
Group={db_user}
ExecStart=$PYTHON_BIN {inst_path}/odoo/odoo-bin -c {rec.odoo_config_path}
WorkingDirectory={inst_path}
Restart=on-failure
RestartSec=5s
LimitNOFILE=65535
Environment=PYTHONUNBUFFERED=1
[Install]
WantedBy=multi-user.target
ENDSVC
systemctl daemon-reload
systemctl enable "{rec.service_name}"

# Step 5: Nginx
STEP "Configurando Nginx..."
DOMAIN="{domain}"
tee "/etc/nginx/sites-enabled/$DOMAIN" > /dev/null << NGINX
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
nginx -t || FAIL "nginx config invalid"
systemctl reload nginx

# Step 6: Start service
STEP "Iniciando servicio Odoo..."
systemctl start "{rec.service_name}"
sleep 5

# Step 7: SSL
STEP "Obteniendo certificado SSL..."
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --email admin@patchmybyte.com --redirect 2>&1 >> "$LOG" || echo "SSL cert failed (HTTP still works)" >> "$LOG"

# Step 8: Verify (try domain, then localhost:port as fallback)
STEP "Verificando..."
HTTP=$(curl -sk -o /dev/null -w '%{{http_code}}' "https://$DOMAIN/web/login" --max-time 15 2>/dev/null || echo "000")
if [ "$HTTP" = "200" ] || [ "$HTTP" = "303" ] || [ "$HTTP" = "302" ]; then
    echo "done" > "$STATUS"
    echo "=== Instance {rec.name} created successfully (HTTPS $HTTP) ===" >> "$LOG"
else
    HTTP2=$(curl -sk -o /dev/null -w '%{{http_code}}' "http://$DOMAIN/web/login" --max-time 15 2>/dev/null || echo "000")
    if [ "$HTTP2" = "200" ] || [ "$HTTP2" = "303" ] || [ "$HTTP2" = "302" ]; then
        echo "done" > "$STATUS"
        echo "=== Instance {rec.name} created (HTTP $HTTP2, no SSL) ===" >> "$LOG"
    else
        # Fallback: verify via localhost:port (DNS may not be ready yet)
        HTTP3=$(curl -s -o /dev/null -w '%{{http_code}}' "http://127.0.0.1:{rec.port}/web/login" --max-time 10 2>/dev/null || echo "000")
        if [ "$HTTP3" = "200" ] || [ "$HTTP3" = "303" ] || [ "$HTTP3" = "302" ]; then
            echo "done" > "$STATUS"
            echo "=== Instance {rec.name} created (port {rec.port}, DNS pending) ===" >> "$LOG"
        else
            FAIL "HTTP verify failed (https=$HTTP http=$HTTP2 local=$HTTP3)"
        fi
    fi
fi
"""
        # SCP script to remote and execute via nohup
        script_path = f"/tmp/pmb_create_{instance_id}.sh"
        with open(script_path, 'w') as f:
            f.write(script)

        ssh_user = project.ssh_user or 'root'
        ssh_host = project.ssh_host

        # Build SCP command
        scp_cmd = ['scp', '-o', 'StrictHostKeyChecking=no']
        if project.ssh_key_path and os.path.isfile(project.ssh_key_path):
            scp_cmd += ['-i', project.ssh_key_path]
        if project.ssh_port and project.ssh_port != 22:
            scp_cmd += ['-P', str(project.ssh_port)]
        scp_cmd += [script_path, f'{ssh_user}@{ssh_host}:{script_path}']

        # Transfer script
        subprocess.run(scp_cmd, capture_output=True, timeout=30)

        # Build SSH command to execute
        ssh_cmd = ['ssh', '-o', 'StrictHostKeyChecking=no']
        if project.ssh_key_path and os.path.isfile(project.ssh_key_path):
            ssh_cmd += ['-i', project.ssh_key_path]
        if project.ssh_port and project.ssh_port != 22:
            ssh_cmd += ['-p', str(project.ssh_port)]
        ssh_cmd += [f'{ssh_user}@{ssh_host}']
        ssh_cmd += [f'nohup bash {script_path} > /dev/null 2>&1 &']

        # Execute on remote
        subprocess.Popen(
            ssh_cmd,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        _logger.info(
            "SSH creation script launched for instance %s on %s",
            rec.name, ssh_host,
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
            infra_utils.start_service(self.service_name, project=self.project_id)
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
            infra_utils.stop_service(self.service_name, project=self.project_id)
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
            infra_utils.restart_service(self.service_name, project=self.project_id)
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

        self.sudo().write({'state': 'destroying'})
        self.env.cr.commit()

        errors = []

        project = self.project_id

        # 1. Stop & remove systemd service
        if self.service_name:
            try:
                infra_utils.remove_systemd_service(
                    self.service_name, project=project,
                )
            except Exception as e:
                errors.append(f"Systemd: {e}")
                _logger.warning("Error removing service %s: %s", self.service_name, e)

        # 2. Drop database
        if self.database_name:
            try:
                infra_utils.drop_database(
                    self.database_name, project=project,
                )
            except Exception as e:
                errors.append(f"Database: {e}")
                _logger.warning("Error dropping database %s: %s", self.database_name, e)

        # 3. Remove Odoo config
        if self.odoo_config_path:
            try:
                infra_utils._run(
                    f"rm -f {self.odoo_config_path}", project=project,
                )
            except Exception as e:
                errors.append(f"Config: {e}")

        # 4. Remove nginx vhost
        if self.nginx_config_path:
            try:
                infra_utils.remove_nginx_vhost(
                    self.nginx_config_path, project=project,
                )
            except Exception as e:
                errors.append(f"Nginx: {e}")
                _logger.warning(
                    "Error removing nginx config %s: %s",
                    self.nginx_config_path, e,
                )

        # 5. Remove instance directory
        if self.instance_path:
            try:
                infra_utils.remove_instance_directory(
                    self.instance_path, project=self.project_id,
                )
            except Exception as e:
                errors.append(f"Directory: {e}")
                _logger.warning(
                    "Error removing instance directory %s: %s",
                    self.instance_path, e,
                )

        # 5b. Delete git branch from remote repos
        if self.git_branch and self.git_branch not in ('main', 'master'):
            project = self.project_id
            try:
                from ..utils import ssh_utils
                # Find git repos in production and delete the instance branch
                prod_path = ''
                if project.production_instance_id:
                    prod_path = project.production_instance_id.instance_path
                elif project.repo_path:
                    prod_path = project.repo_path
                if prod_path:
                    # Delete branch from all git repos in production path
                    if project.connection_type == 'ssh' and project.ssh_host:
                        cmd = (
                            f'for D in {prod_path}/*/; do '
                            f'  if [ -d "$D/.git" ]; then '
                            f'    cd "$D" && git push origin --delete {self.git_branch} 2>/dev/null; '
                            f'    git branch -d {self.git_branch} 2>/dev/null; '
                            f'  fi; '
                            f'done'
                        )
                        ssh_utils.execute_command_shell(project, cmd)
                    else:
                        import glob
                        for git_dir in glob.glob(f'{prod_path}/*/.git'):
                            repo = os.path.dirname(git_dir)
                            subprocess.run(
                                ['git', 'push', 'origin', '--delete', self.git_branch],
                                cwd=repo, capture_output=True, timeout=30,
                            )
                            subprocess.run(
                                ['git', 'branch', '-d', self.git_branch],
                                cwd=repo, capture_output=True, timeout=10,
                            )
                _logger.info("Deleted git branch %s", self.git_branch)
            except Exception as e:
                errors.append(f"Git branch: {e}")
                _logger.warning("Error deleting git branch %s: %s", self.git_branch, e)

        # 6. Delete associated branch record
        if self.branch_id:
            try:
                self.branch_id.sudo().unlink()
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
        self.sudo().unlink()

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
    # SSL certificate (issue / verify)
    # ------------------------------------------------------------------

    def _dns_check(self):
        """Compare DNS A record of full_domain vs the server IP.

        Returns (ok: bool, detail: str). ok=True when DNS resolves to the
        same public IP as the server that would serve the vhost. For SSH
        projects, the reference IP is fetched with `curl ifconfig.me` on
        the remote; for local, it's the hub's own public IP.
        """
        self.ensure_one()
        if not self.full_domain:
            return False, _("La instancia no tiene dominio configurado.")

        import socket
        try:
            resolved = sorted(set(
                ai[4][0] for ai in socket.getaddrinfo(
                    self.full_domain, None, socket.AF_INET,
                )
            ))
        except Exception as e:
            return False, _("No se pudo resolver %s: %s") % (self.full_domain, e)
        if not resolved:
            return False, _("El dominio %s no tiene registro A.") % self.full_domain

        project = self.project_id
        try:
            r = infra_utils._run("curl -s --max-time 8 https://ifconfig.me",
                                 project=project, timeout=15)
            server_ip = (r.stdout or '').strip()
        except Exception:
            server_ip = ''

        if not server_ip:
            return False, _("No se pudo obtener la IP del servidor "
                            "(dominio resuelve a %s).") % ', '.join(resolved)

        if server_ip in resolved:
            return True, _("DNS OK (%s -> %s)") % (self.full_domain, server_ip)
        return False, _(
            "DNS mismatch: %(domain)s resuelve a %(resolved)s, "
            "pero el servidor es %(server)s. Actualiza el registro DNS para "
            "que %(domain)s apunte a %(server)s antes de reintentar el "
            "certificado SSL."
        ) % {
            'domain': self.full_domain,
            'resolved': ', '.join(resolved),
            'server': server_ip,
        }

    def action_obtain_ssl_cert(self):
        """(Re)issue the Let's Encrypt certificate for this instance.

        Workflow:
        1. Pre-check DNS -> server IP (catches the common "DNS not pointing
           to this server" failure mode).
        2. Run `certbot --nginx -d <domain>` via the project's transport.
        3. Reload nginx.
        4. Post the result (success/error) to the instance chatter and
           update ssl_status + ssl_last_error.
        """
        self.ensure_one()
        if not self.full_domain:
            raise UserError(_("La instancia no tiene dominio configurado."))

        ok, detail = self._dns_check()
        self.write({'ssl_last_checked': fields.Datetime.now()})
        if not ok:
            self.write({
                'ssl_status': 'dns_mismatch',
                'ssl_last_error': detail,
            })
            self.message_post(body=_(
                "<b>SSL: DNS no válido para %(d)s</b><br/>%(msg)s"
            ) % {'d': self.full_domain, 'msg': detail})
            # Commit before raising so the badge persists for the UI
            self.env.cr.commit()
            raise UserError(detail)

        project = self.project_id
        cmd = (
            "certbot --nginx -d {domain} --non-interactive --agree-tos "
            "--email admin@patchmybyte.com --redirect"
        ).format(domain=self.full_domain)
        result = infra_utils.sudo_run(cmd, timeout=180, project=project)
        stdout = (result.stdout or '')
        stderr = (result.stderr or '')
        output = (stdout + "\n" + stderr).strip()

        if result.returncode != 0:
            self.write({
                'ssl_status': 'error',
                'ssl_last_error': output[:4000],
            })
            self.message_post(body=_(
                "<b>SSL: certbot falló para %(d)s</b><pre>%(out)s</pre>"
            ) % {'d': self.full_domain, 'out': output[:2000]})
            self.env.cr.commit()
            raise UserError(_(
                "Certbot falló para %(d)s. Revisa el historial de la "
                "instancia para ver el detalle.\n\n%(out)s"
            ) % {'d': self.full_domain, 'out': output[-500:]})

        # Success: reload nginx and mark ok
        try:
            infra_utils.reload_nginx(project=project)
        except Exception as e:
            _logger.warning("nginx reload after cert issuance: %s", e)

        self.write({
            'ssl_status': 'ok',
            'ssl_last_error': '',
        })
        self.message_post(body=_(
            "<b>SSL: certificado emitido/renovado para %s</b>"
        ) % self.full_domain)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("SSL emitido"),
                'message': _("Certificado Let's Encrypt activo para %s.")
                           % self.full_domain,
                'type': 'success',
            },
        }

    def action_check_ssl_status(self):
        """Probe https://full_domain and update ssl_status from cert validity."""
        self.ensure_one()
        if not self.full_domain:
            return
        project = self.project_id
        cmd = (
            "echo | timeout 10 openssl s_client -servername {d} "
            "-connect {d}:443 2>/dev/null | openssl x509 -noout -enddate "
            "2>/dev/null || true"
        ).format(d=self.full_domain)
        result = infra_utils._run(cmd, project=project, timeout=20)
        out = (result.stdout or '').strip()
        vals = {'ssl_last_checked': fields.Datetime.now()}
        if 'notAfter=' in out:
            vals['ssl_status'] = 'ok'
            vals['ssl_last_error'] = ''
        else:
            # If we were 'ok' and now not, flip to missing; otherwise keep
            if self.ssl_status == 'ok':
                vals['ssl_status'] = 'missing'
        self.write(vals)

    # ------------------------------------------------------------------
    # Status check
    # ------------------------------------------------------------------

    def _check_service_status(self):
        """Check service status and update state field. Dispatches by
        runtime: docker-runtime uses `docker compose ps` via SSH; systemd
        uses `systemctl is-active`."""
        for rec in self:
            if rec.project_id.runtime == 'docker':
                if not rec.docker_compose_path:
                    continue
                try:
                    s = rec.get_docker_status()
                    containers = s.get('containers') or []
                    # Only the odoo container drives the instance state.
                    # (postgres + code-server can be down without affecting
                    # what the user considers "the instance is up".)
                    odoo_c = next((c for c in containers if c.get('service') == 'odoo'), None)
                    if not containers:
                        rec.state = 'stopped'
                    elif odoo_c and odoo_c.get('state') == 'running':
                        rec.state = 'running'
                    elif odoo_c and odoo_c.get('state') in ('exited', 'dead'):
                        rec.state = 'stopped'
                    else:
                        rec.state = 'error'
                except Exception as e:
                    _logger.warning("docker health check failed for %s: %s", rec.name, e)
                continue
            if not rec.service_name:
                continue
            status = infra_utils.is_service_active(rec.service_name, project=rec.project_id)
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

    @api.model
    def _cron_cleanup_failed(self):
        """Clean up instances stuck in 'creating' or 'error' state.

        - creating > 1 hour with dead PID → mark as error
        - error > 7 days for dev instances → destroy
        """
        from datetime import timedelta
        # Stuck creating
        stuck = self.search([
            ('state', '=', 'creating'),
            ('create_date', '<', fields.Datetime.now() - timedelta(hours=1)),
        ])
        for inst in stuck:
            pid = inst.creation_pid
            if pid and pid > 0:
                try:
                    os.kill(pid, 0)
                    continue  # still alive
                except (ProcessLookupError, PermissionError):
                    pass
            inst.sudo().write({
                'state': 'error',
                'creation_step': 'Timeout: creacion excedió 1 hora',
            })
            _logger.info("Cleanup: marked stuck instance %s as error", inst.name)

        # Old error dev instances
        old_errors = self.search([
            ('state', '=', 'error'),
            ('instance_type', '=', 'development'),
            ('create_date', '<', fields.Datetime.now() - timedelta(days=7)),
        ])
        for inst in old_errors:
            try:
                inst.action_destroy()
                _logger.info("Cleanup: destroyed old error dev instance %s", inst.name)
            except Exception as e:
                _logger.warning("Cleanup: failed to destroy %s: %s", inst.name, e)
