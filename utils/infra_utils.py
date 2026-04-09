"""Infrastructure utilities for automated Odoo instance management.

Handles nginx, systemd, database, and filesystem operations
for creating and destroying Odoo instances.
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

   access_log /var/log/nginx/{instance_name}.access.log;
   error_log /var/log/nginx/{instance_name}.error.log;

   proxy_buffers 16 64k;
   proxy_buffer_size 128k;
   client_max_body_size 4000M;
   proxy_connect_timeout 1800;
   proxy_send_timeout 1800;
   proxy_read_timeout 1800;
   send_timeout 1800;

   ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;
   ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;
   include /etc/letsencrypt/options-ssl-nginx.conf;
   ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

   add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;

   location / {{
       proxy_pass http://odoo_{instance_id};
       proxy_next_upstream error timeout invalid_header http_500 http_502 http_503 http_504;
       proxy_redirect off;
       proxy_set_header Host $host;
       proxy_set_header X-Real-IP $remote_addr;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       proxy_set_header X-Forwarded-Proto https;
   }}

   location /websocket {{
       proxy_pass http://odoochat_{instance_id};
       proxy_set_header Upgrade $http_upgrade;
       proxy_set_header Connection $connection_upgrade;
       proxy_set_header X-Forwarded-Host $http_host;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       proxy_set_header X-Forwarded-Proto $scheme;
       proxy_set_header X-Real-IP $remote_addr;
       proxy_buffering off;
       proxy_cache_bypass $http_upgrade;
       proxy_read_timeout 3600s;
   }}
}}
"""

SYSTEMD_TEMPLATE = """\
[Unit]
Description=Odoo {service_name}
After=network.target postgresql.service

[Service]
Type=simple
SyslogIdentifier={service_name}
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
# Utility
# ---------------------------------------------------------------------------


def sudo_run(command, timeout=60):
    """Run a command with sudo, capturing output.

    Args:
        command: string or list -- the command to execute.
        timeout: seconds before TimeoutExpired.

    Returns:
        subprocess.CompletedProcess
    """
    if isinstance(command, str):
        full_cmd = f"sudo {command}"
        return subprocess.run(
            full_cmd, shell=True,
            capture_output=True, text=True, timeout=timeout,
        )
    else:
        return subprocess.run(
            ['sudo'] + list(command),
            capture_output=True, text=True, timeout=timeout,
        )


# ---------------------------------------------------------------------------
# Nginx
# ---------------------------------------------------------------------------


def create_nginx_vhost(domain, port, gevent_port, instance_id, instance_name):
    """Write nginx vhost config and reload nginx.

    Returns:
        str: path to the created config file.
    """
    config_content = NGINX_TEMPLATE.format(
        domain=domain,
        port=port,
        gevent_port=gevent_port,
        instance_id=instance_id,
        instance_name=instance_name,
    )
    path = f"/etc/nginx/sites-enabled/{domain}"
    result = sudo_run(f"tee {path} <<'NGINX_EOF'\n{config_content}\nNGINX_EOF")
    if result.returncode != 0:
        raise RuntimeError(f"Error writing nginx config: {result.stderr}")

    # Validate nginx config
    test = sudo_run("nginx -t")
    if test.returncode != 0:
        # Rollback: remove bad config
        sudo_run(f"rm -f {path}")
        raise RuntimeError(f"Nginx config test failed: {test.stderr}")

    return path


def remove_nginx_vhost(path):
    """Remove nginx vhost config and reload nginx."""
    sudo_run(f"rm -f {path}")
    test = sudo_run("nginx -t")
    if test.returncode != 0:
        _logger.warning("Nginx config test failed after removing %s: %s", path, test.stderr)
    sudo_run("systemctl reload nginx")


def reload_nginx():
    """Test and reload nginx."""
    test = sudo_run("nginx -t")
    if test.returncode != 0:
        raise RuntimeError(f"Nginx config test failed: {test.stderr}")
    result = sudo_run("systemctl reload nginx")
    if result.returncode != 0:
        raise RuntimeError(f"Nginx reload failed: {result.stderr}")


# ---------------------------------------------------------------------------
# SSL
# ---------------------------------------------------------------------------


def obtain_ssl_cert(domain):
    """Obtain SSL certificate via certbot for the given domain."""
    result = sudo_run(
        f"certbot --nginx -d {domain} "
        f"--non-interactive --agree-tos "
        f"--email admin@patchmybyte.com --redirect",
        timeout=120,
    )
    if result.returncode != 0:
        _logger.warning("Certbot failed for %s: %s", domain, result.stderr)
        raise RuntimeError(f"Certbot failed: {result.stderr}")
    return result


# ---------------------------------------------------------------------------
# Systemd
# ---------------------------------------------------------------------------


def create_systemd_service(service_name, config_path, instance_path):
    """Create and enable a systemd service unit.

    Returns:
        str: path to the created service file.
    """
    content = SYSTEMD_TEMPLATE.format(
        service_name=service_name,
        config_path=config_path,
        instance_path=instance_path,
    )
    path = f"/etc/systemd/system/{service_name}.service"
    result = sudo_run(f"tee {path} <<'SYSTEMD_EOF'\n{content}\nSYSTEMD_EOF")
    if result.returncode != 0:
        raise RuntimeError(f"Error writing systemd service: {result.stderr}")

    sudo_run("systemctl daemon-reload")
    sudo_run(f"systemctl enable {service_name}")
    return path


def remove_systemd_service(service_name):
    """Stop, disable, and remove a systemd service."""
    sudo_run(f"systemctl stop {service_name}", timeout=30)
    sudo_run(f"systemctl disable {service_name}")
    path = f"/etc/systemd/system/{service_name}.service"
    sudo_run(f"rm -f {path}")
    sudo_run("systemctl daemon-reload")


def start_service(service_name, timeout=30):
    """Start a systemd service."""
    result = sudo_run(f"systemctl start {service_name}", timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"Error starting {service_name}: {result.stderr}")


def stop_service(service_name, timeout=30):
    """Stop a systemd service."""
    result = sudo_run(f"systemctl stop {service_name}", timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"Error stopping {service_name}: {result.stderr}")


def restart_service(service_name, timeout=30):
    """Restart a systemd service."""
    result = sudo_run(f"systemctl restart {service_name}", timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"Error restarting {service_name}: {result.stderr}")


def is_service_active(service_name):
    """Check if a systemd service is active.

    Returns:
        str: 'active', 'inactive', 'failed', etc.
    """
    result = sudo_run(f"systemctl is-active {service_name}", timeout=10)
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Odoo config
# ---------------------------------------------------------------------------


def create_odoo_config(service_name, db_name, port, gevent_port,
                       instance_path, addons_path):
    """Write Odoo configuration file.

    Returns:
        str: path to the created config file.
    """
    content = ODOO_CONFIG_TEMPLATE.format(
        service_name=service_name,
        db_name=db_name,
        port=port,
        gevent_port=gevent_port,
        addons_path=addons_path,
    )
    path = f"/etc/{service_name}.conf"
    result = sudo_run(f"tee {path} <<'CONF_EOF'\n{content}\nCONF_EOF")
    if result.returncode != 0:
        raise RuntimeError(f"Error writing Odoo config: {result.stderr}")

    # Restrict permissions
    sudo_run(f"chmod 640 {path}")
    sudo_run(f"chown odooal:odooal {path}")
    return path


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def clone_database(source_db, target_db, timeout=600):
    """Clone a PostgreSQL database.

    Tries createdb -T (fast template clone) first; falls back to
    pg_dump | psql if the template approach fails (e.g. active connections).
    """
    # Attempt 1: createdb -T (requires no active connections on source)
    result = sudo_run(
        f'su - postgres -c "createdb -T {source_db} {target_db}"',
        timeout=timeout,
    )
    if result.returncode == 0:
        _logger.info("Database %s cloned from %s via createdb -T", target_db, source_db)
        return

    _logger.info(
        "createdb -T failed (%s), falling back to pg_dump|psql",
        result.stderr.strip(),
    )

    # Attempt 2: pg_dump | psql
    result = sudo_run(
        f'su - postgres -c "createdb {target_db}"',
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Error creating target database: {result.stderr}")

    result = sudo_run(
        f'su - postgres -c "pg_dump {source_db} | psql {target_db}"',
        timeout=timeout,
    )
    if result.returncode != 0:
        # Cleanup failed target
        sudo_run(f'su - postgres -c "dropdb --if-exists {target_db}"')
        raise RuntimeError(f"Error cloning database via pg_dump: {result.stderr}")

    _logger.info("Database %s cloned from %s via pg_dump|psql", target_db, source_db)


def drop_database(db_name):
    """Terminate all connections and drop a PostgreSQL database."""
    # Terminate active connections
    sudo_run(
        f"su - postgres -c \"psql -c "
        f"\\\"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid();\\\"\"",
        timeout=30,
    )
    result = sudo_run(
        f'su - postgres -c "dropdb --if-exists {db_name}"',
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Error dropping database {db_name}: {result.stderr}")
    _logger.info("Database %s dropped", db_name)


# ---------------------------------------------------------------------------
# Filesystem
# ---------------------------------------------------------------------------


def create_instance_directory(instance_path):
    """Create the instance directory structure with proper ownership."""
    sudo_run(f"mkdir -p {instance_path}/.local/share/Odoo")
    sudo_run(f"chown -R odooal:odooal {instance_path}")


def remove_instance_directory(instance_path):
    """Remove an instance directory (with safety check)."""
    if not instance_path or not instance_path.startswith('/opt/instances/'):
        raise RuntimeError(
            f"Safety check failed: refusing to remove '{instance_path}'. "
            f"Path must start with /opt/instances/"
        )
    sudo_run(f"rm -rf {instance_path}")
    _logger.info("Instance directory %s removed", instance_path)
