"""Infrastructure utilities for automated Odoo instance management.

Handles nginx, systemd, database, and filesystem operations.
All functions support both local and SSH execution via optional `project` param.
"""
import logging
import subprocess

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

NGINX_TEMPLATE = """\
upstream odoo_{instance_id} {{
   server 127.0.0.1:{port};
}}
upstream odoochat_{instance_id} {{
   server 127.0.0.1:{gevent_port};
}}
server {{
   listen 80;
   server_name {domain};
   location /.well-known/acme-challenge/ {{ root /var/www/html; }}
   location / {{ return 301 https://$host$request_uri; }}
}}
server {{
   listen 443 ssl;
   server_name {domain};

   ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;
   ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;

   proxy_read_timeout 720s;
   proxy_connect_timeout 720s;
   proxy_send_timeout 720s;

   proxy_set_header X-Forwarded-Host $host;
   proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
   proxy_set_header X-Forwarded-Proto $scheme;
   proxy_set_header X-Real-IP $remote_addr;

   location /websocket {{
       proxy_pass http://odoochat_{instance_id};
       proxy_http_version 1.1;
       proxy_set_header Upgrade $http_upgrade;
       proxy_set_header Connection "upgrade";
   }}

   location / {{
       proxy_redirect off;
       proxy_pass http://odoo_{instance_id};
   }}

   # Enable gzip
   gzip_types text/css text/plain text/xml application/xml application/javascript application/json;
   gzip on;
   client_max_body_size 200m;
}}
"""

SYSTEMD_TEMPLATE = """\
[Unit]
Description=Odoo {service_name}
After=postgresql.service network.target

[Service]
Type=simple
User=odooal
Group=odooal
WorkingDirectory={instance_path}
ExecStart=/opt/odooAL/.venv/bin/python /opt/odooAL/odoo/odoo-bin -c {config_path}
Restart=on-failure
RestartSec=5
StandardOutput=journal+console

[Install]
WantedBy=multi-user.target
"""

ODOO_CONFIG_TEMPLATE = """\
[options]
addons_path = /opt/odooAL/odoo/addons,/opt/odooAL/custom_addons,{addons_path}
db_name = {db_name}
db_user = odooal
http_port = {port}
gevent_port = {gevent_port}
workers = 2
proxy_mode = True
list_db = False
logfile = /var/log/odoo/{service_name}.log
"""

# ---------------------------------------------------------------------------
# Core: run command locally or via SSH
# ---------------------------------------------------------------------------


def _run(cmd_str, project=None, timeout=60):
    """Run a shell command locally or via SSH based on project type.

    This is the single entry point — ALL infra operations go through here.
    """
    if project and project.connection_type == 'ssh' and project.ssh_host:
        from . import ssh_utils
        return ssh_utils.execute_command_shell(project, cmd_str, timeout=timeout)
    # Local: use sudo
    return subprocess.run(
        f"sudo {cmd_str}", shell=True,
        capture_output=True, text=True, timeout=timeout,
    )


def sudo_run(command, timeout=60, project=None):
    """Run a command with sudo (local) or via SSH (remote).

    Backward compatible: existing calls without project= still work locally.
    """
    if isinstance(command, str):
        return _run(command, project, timeout)
    else:
        cmd_str = ' '.join(command)
        return _run(cmd_str, project, timeout)


# ---------------------------------------------------------------------------
# Nginx
# ---------------------------------------------------------------------------


def create_nginx_vhost(domain, port, gevent_port, instance_id, instance_name, project=None):
    """Write nginx vhost config and reload nginx."""
    config_content = NGINX_TEMPLATE.format(
        domain=domain, port=port, gevent_port=gevent_port,
        instance_id=instance_id, instance_name=instance_name,
    )
    path = f"/etc/nginx/sites-enabled/{domain}"
    result = sudo_run(f"tee {path} <<'NGINX_EOF'\n{config_content}\nNGINX_EOF", project=project)
    if result.returncode != 0:
        raise RuntimeError(f"Error writing nginx config: {result.stderr}")
    test = sudo_run("nginx -t", project=project)
    if test.returncode != 0:
        sudo_run(f"rm -f {path}", project=project)
        raise RuntimeError(f"Nginx config test failed: {test.stderr}")
    return path


def remove_nginx_vhost(path, project=None):
    """Remove nginx vhost config and reload nginx."""
    sudo_run(f"rm -f {path}", project=project)
    sudo_run("nginx -t", project=project)
    sudo_run("systemctl reload nginx", project=project)


def reload_nginx(project=None):
    """Test and reload nginx."""
    test = sudo_run("nginx -t", project=project)
    if test.returncode != 0:
        raise RuntimeError(f"Nginx config test failed: {test.stderr}")
    result = sudo_run("systemctl reload nginx", project=project)
    if result.returncode != 0:
        raise RuntimeError(f"Nginx reload failed: {result.stderr}")


# ---------------------------------------------------------------------------
# SSL
# ---------------------------------------------------------------------------


def obtain_ssl_cert(domain, project=None):
    """Obtain SSL certificate via certbot."""
    result = sudo_run(
        f"certbot --nginx -d {domain} --non-interactive --agree-tos "
        f"--email admin@patchmybyte.com --redirect",
        timeout=120, project=project,
    )
    if result.returncode != 0:
        _logger.warning("Certbot failed for %s: %s", domain, result.stderr)
        raise RuntimeError(f"Certbot failed: {result.stderr}")
    return result


# ---------------------------------------------------------------------------
# Systemd
# ---------------------------------------------------------------------------


def create_systemd_service(service_name, config_path, instance_path, project=None):
    """Create and enable a systemd service unit."""
    content = SYSTEMD_TEMPLATE.format(
        service_name=service_name, config_path=config_path,
        instance_path=instance_path,
    )
    path = f"/etc/systemd/system/{service_name}.service"
    result = sudo_run(f"tee {path} <<'SYSTEMD_EOF'\n{content}\nSYSTEMD_EOF", project=project)
    if result.returncode != 0:
        raise RuntimeError(f"Error writing systemd service: {result.stderr}")
    sudo_run("systemctl daemon-reload", project=project)
    sudo_run(f"systemctl enable {service_name}", project=project)
    return path


def remove_systemd_service(service_name, project=None):
    """Stop, disable, and remove a systemd service."""
    sudo_run(f"systemctl stop {service_name}", timeout=30, project=project)
    sudo_run(f"systemctl disable {service_name}", project=project)
    path = f"/etc/systemd/system/{service_name}.service"
    sudo_run(f"rm -f {path}", project=project)
    sudo_run("systemctl daemon-reload", project=project)


def start_service(service_name, timeout=30, project=None):
    """Start a systemd service."""
    result = sudo_run(f"systemctl start {service_name}", timeout=timeout, project=project)
    if result.returncode != 0:
        raise RuntimeError(f"Error starting {service_name}: {result.stderr}")


def stop_service(service_name, timeout=30, project=None):
    """Stop a systemd service."""
    result = sudo_run(f"systemctl stop {service_name}", timeout=timeout, project=project)
    if result.returncode != 0:
        raise RuntimeError(f"Error stopping {service_name}: {result.stderr}")


def restart_service(service_name, timeout=30, project=None):
    """Restart a systemd service."""
    result = sudo_run(f"systemctl restart {service_name}", timeout=timeout, project=project)
    if result.returncode != 0:
        raise RuntimeError(f"Error restarting {service_name}: {result.stderr}")


def is_service_active(service_name, project=None):
    """Check if a systemd service is active."""
    result = sudo_run(f"systemctl is-active {service_name}", timeout=10, project=project)
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Odoo config
# ---------------------------------------------------------------------------


def create_odoo_config(service_name, db_name, port, gevent_port,
                       instance_path, addons_path, project=None):
    """Write Odoo configuration file."""
    content = ODOO_CONFIG_TEMPLATE.format(
        service_name=service_name, db_name=db_name,
        port=port, gevent_port=gevent_port, addons_path=addons_path,
    )
    path = f"/etc/{service_name}.conf"
    result = sudo_run(f"tee {path} <<'CONF_EOF'\n{content}\nCONF_EOF", project=project)
    if result.returncode != 0:
        raise RuntimeError(f"Error writing Odoo config: {result.stderr}")
    sudo_run(f"chmod 640 {path}", project=project)
    sudo_run(f"chown odooal:odooal {path}", project=project)
    return path


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def clone_database(source_db, target_db, timeout=1800, project=None):
    """Clone a PostgreSQL database using pg_dump | psql."""
    _logger.info("Cloning database %s -> %s", source_db, target_db)
    result = _run(f"createdb -O odooal {target_db}", project, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"Error creating database {target_db}: {result.stderr}")
    result = _run(f"pg_dump {source_db} | psql -q {target_db}", project, timeout=timeout)
    if result.returncode != 0:
        _run(f"dropdb --if-exists {target_db}", project, timeout=60)
        raise RuntimeError(f"Error cloning database: {result.stderr}")
    _logger.info("Database %s cloned from %s", target_db, source_db)


def drop_database(db_name, project=None):
    """Terminate connections and drop a database."""
    _run(f"psql -c \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
         f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid();\" odooal",
         project, timeout=30)
    result = _run(f"dropdb --if-exists {db_name}", project, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"Error dropping database {db_name}: {result.stderr}")
    _logger.info("Database %s dropped", db_name)


# ---------------------------------------------------------------------------
# Filesystem
# ---------------------------------------------------------------------------


def create_instance_directory(instance_path, project=None):
    """Create the instance directory structure with proper ownership."""
    sudo_run(f"mkdir -p {instance_path}/.local/share/Odoo", project=project)
    sudo_run(f"chown -R odooal:odooal {instance_path}", project=project)


def remove_instance_directory(instance_path, project=None):
    """Remove an instance directory (with safety check)."""
    if not instance_path or not instance_path.startswith('/opt/instances/'):
        raise RuntimeError(
            f"Safety check failed: refusing to remove '{instance_path}'. "
            f"Path must start with /opt/instances/"
        )
    sudo_run(f"rm -rf {instance_path}", project=project)
    _logger.info("Instance directory %s removed", instance_path)
