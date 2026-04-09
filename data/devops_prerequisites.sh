#!/bin/bash
# PMB DevOps server prerequisites — run once as root

# 1. Sudoers for odooal
cat > /etc/sudoers.d/pmb_devops << 'EOF'
odooal ALL=(ALL) NOPASSWD: /usr/bin/systemctl, /usr/sbin/nginx, /usr/bin/certbot, /usr/bin/tee, /usr/bin/createdb, /usr/bin/dropdb, /usr/bin/pg_dump, /bin/rm, /bin/ln, /bin/mkdir, /bin/chown, /bin/chmod, /usr/bin/psql
EOF
chmod 440 /etc/sudoers.d/pmb_devops

# 2. Instances directory
mkdir -p /opt/instances
chown odooal:odooal /opt/instances

# 3. Grant odooal createdb in PostgreSQL
sudo -u postgres psql -c "ALTER USER odooal CREATEDB;" 2>/dev/null

# 4. Ensure log directory is writable
mkdir -p /var/log/odoo
chown odooal:odooal /var/log/odoo

echo "PMB DevOps prerequisites configured."
