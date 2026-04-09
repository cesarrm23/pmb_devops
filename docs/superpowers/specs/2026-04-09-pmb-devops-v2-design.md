# PMB DevOps v2 — Odoo.sh Replica Design Specification

**Date:** 2026-04-09
**Author:** PatchMyByte
**Module:** pmb_devops (refactor of existing v1)
**Target:** asistentelisto.com (odooAL, port 8075)
**Repo:** cesarrm23/pmb_devops

---

## 1. Overview

Refactor pmb_devops from a traditional Odoo menu-based module into a full Odoo.sh replica. The key change: **one project = one business** with multiple instances (production, staging, development) that are created/destroyed automatically from the UI. The entire interface is a single OWL SPA component matching the Odoo.sh layout.

### What changes from v1:
- **Model restructure**: New `devops.instance` model. `devops.project` becomes the business, not the instance.
- **UI replacement**: All Odoo views replaced by a single OWL SPA with sidebar + tabbed content area.
- **Infrastructure automation**: Automatic creation/destruction of systemd services, PostgreSQL DBs, nginx vhosts, SSL certs, Odoo configs.
- **Auto-lifecycle**: Auto-stop after 1h inactivity, auto-destroy development after configurable hours.

### What stays from v1:
- Backend models: branch, build, backup, log, ai_assistant, deploy_ai, plugin
- Utils layer: ssh_utils.py, git_utils.py
- Terminal controller + bridge.py (xterm.js PTY)
- Security groups structure (privilege + 3 tiers)
- Settings, wizards, cron jobs

---

## 2. Data Model

### 2.1 devops.project (= business/client)

```python
class DevopsProject(models.Model):
    _name = 'devops.project'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(required=True)                    # "Cremara"
    domain = fields.Char(required=True)                  # "cremara.com"
    repo_url = fields.Char()                             # git remote URL
    repo_path = fields.Char(required=True)               # /opt/odoo19Test/cremara_addons
    odoo_version = fields.Char(default='19.0')

    # Instances
    instance_ids = fields.One2many('devops.instance', 'project_id')
    production_instance_id = fields.Many2one('devops.instance')

    # Limits (configurable by admin)
    max_staging = fields.Integer(default=3)
    max_development = fields.Integer(default=5)
    auto_destroy_hours = fields.Integer(default=24)

    # Members
    member_ids = fields.One2many('devops.project.member', 'project_id')

    # Branches
    branch_ids = fields.One2many('devops.branch', 'project_id')

    # Connection (for production — SSH or local)
    connection_type = fields.Selection([
        ('local', 'Local'),
        ('ssh', 'SSH'),
    ], default='local')
    ssh_host = fields.Char()
    ssh_user = fields.Char()
    ssh_port = fields.Integer(default=22)
    ssh_key_path = fields.Char()

    # AI
    ai_api_key = fields.Char(groups='pmb_devops.group_devops_admin')
```

### 2.2 devops.instance (NEW — Odoo instance per branch)

```python
class DevopsInstance(models.Model):
    _name = 'devops.instance'
    _inherit = ['mail.thread']
    _order = 'instance_type, name'

    project_id = fields.Many2one('devops.project', required=True, ondelete='cascade')
    branch_id = fields.Many2one('devops.branch', ondelete='set null')
    name = fields.Char(required=True)                    # "staging-1", "dev-cesar"

    instance_type = fields.Selection([
        ('production', 'Production'),
        ('staging', 'Staging'),
        ('development', 'Development'),
    ], required=True)

    # Infrastructure (auto-generated)
    subdomain = fields.Char()                            # "staging-1"
    full_domain = fields.Char(compute='_compute_full_domain')  # staging-1.cremara.com
    port = fields.Integer()                              # 8080
    gevent_port = fields.Integer()                       # 9080
    service_name = fields.Char()                         # odoo-cremara-stg-1
    database_name = fields.Char()                        # cremara_stg_1
    odoo_config_path = fields.Char()                     # /etc/odoo/cremara-stg-1.conf
    instance_path = fields.Char()                        # /opt/instances/cremara-stg-1/
    url = fields.Char(compute='_compute_url')            # https://staging-1.cremara.com
    nginx_config_path = fields.Char()                    # /etc/nginx/sites-enabled/cremara-stg-1.conf

    # State
    state = fields.Selection([
        ('creating', 'Creating'),
        ('running', 'Running'),
        ('stopped', 'Stopped'),
        ('error', 'Error'),
        ('destroying', 'Destroying'),
    ], default='creating')
    last_activity = fields.Datetime(default=fields.Datetime.now)

    # Users with access
    user_ids = fields.Many2many('res.users', string='Assigned Users')

    # Relations
    build_ids = fields.One2many('devops.build', 'instance_id')
    backup_ids = fields.One2many('devops.backup', 'instance_id')
    log_ids = fields.One2many('devops.log', 'instance_id')

    # Cloned from
    cloned_from_id = fields.Many2one('devops.instance')

    # Methods
    def action_create_instance(self):
        """Full automated instance creation pipeline."""
    def action_start(self):
        """Start the systemd service."""
    def action_stop(self):
        """Stop the systemd service."""
    def action_restart(self):
        """Restart the systemd service. NEVER stop production."""
    def action_destroy(self):
        """Full automated destruction pipeline."""
    def action_open_shell(self):
        """Open shell terminal for this instance."""
    def action_open_logs(self):
        """Open live logs for this instance."""
```

### 2.3 devops.branch (updated)

Add link to instance:
```python
instance_id = fields.Many2one('devops.instance', ondelete='set null')
```

### 2.4 devops.build, devops.backup, devops.log (updated)

Add link to instance (in addition to project):
```python
instance_id = fields.Many2one('devops.instance', ondelete='cascade')
```

### 2.5 devops.project.member (unchanged)

Same as v1: project_id, user_id, role (admin/developer/viewer).

---

## 3. Infrastructure Automation

### 3.1 Instance Creation Pipeline

`devops.instance.action_create_instance()` executes these steps sequentially:

1. **Validate limits** — check max_staging/max_development not exceeded
2. **Assign free port** — scan 8080-8199 for unused port, gevent = port + 1000
3. **Create git branch** — `git checkout -b {branch_name} {source_branch}` + push
4. **Clone database** — `createdb -T {source_db} {new_db}` (pg_dump|psql for cross-user)
5. **Create instance directory** — `/opt/instances/{project}-{name}/`
6. **Symlink Odoo source** — `ln -s /opt/odooAL/odoo` into instance dir
7. **Symlink/copy addons** — link repo path into instance
8. **Generate Odoo config** — write `/etc/odoo/{service_name}.conf` with correct db_name, port, addons_path
9. **Create systemd service** — write `/etc/systemd/system/{service_name}.service`, daemon-reload, enable
10. **Generate nginx vhost** — write `/etc/nginx/sites-enabled/{name}.conf` with proxy_pass to port
11. **Reload nginx** — `nginx -t && systemctl reload nginx`
12. **Start service** — `systemctl start {service_name}`
13. **SSL certificate** — `certbot --nginx -d {subdomain}.{domain} --non-interactive --agree-tos`
14. **Verify HTTP 200** — curl the URL
15. **Update state** — set to 'running' or 'error'

All commands run via `subprocess` as `odooal` user with sudo privileges.

### 3.2 Instance Destruction Pipeline

`devops.instance.action_destroy()`:

1. `systemctl stop {service_name}`
2. `systemctl disable {service_name}`
3. Remove service file from `/etc/systemd/system/`
4. `systemctl daemon-reload`
5. `dropdb {database_name}`
6. Remove config from `/etc/odoo/`
7. Remove nginx vhost + `systemctl reload nginx`
8. Remove instance directory from `/opt/instances/`
9. Delete git branch (optional, configurable)
10. Unlink Odoo record

### 3.3 Production Instance (READONLY)

Production is registered manually when creating the project:
- service_name = existing service (e.g., "odoo19")
- database_name = existing DB (e.g., "odoo19")
- port = existing port (e.g., 8073)
- No creation/destruction pipeline
- Cannot be stopped or destroyed from UI
- Only action: git pull + restart (via merge from staging)

### 3.4 Sudoers Configuration

During module install (or documented as prerequisite):
```
odooal ALL=(ALL) NOPASSWD: /usr/bin/systemctl, /usr/sbin/nginx, /usr/bin/certbot, /usr/bin/tee, /usr/bin/createdb, /usr/bin/dropdb, /bin/rm, /bin/ln, /bin/mkdir, /bin/chown, /bin/chmod, /usr/bin/pg_dump
```

### 3.5 Port Management

```python
def _find_free_port(self):
    """Find next available port starting from 8080."""
    used_ports = set(self.search([]).mapped('port'))
    for port in range(8080, 8200):
        if port not in used_ports:
            return port
    raise UserError("No free ports available (8080-8199)")
```

### 3.6 Auto-lifecycle Crons

**Auto-stop** (every 15 min): Stop staging/development instances with >1h inactivity.

**Auto-destroy** (every 1h): Destroy development instances that have been stopped for longer than `project.auto_destroy_hours`.

**Health check** (every 5 min): Check all running instances, update state.

---

## 4. UI — OWL SPA Component

### 4.1 Architecture

One single OWL component `PmbDevopsApp` registered as `ir.actions.client` tag `pmb_devops_main`. This replaces ALL current Odoo views and menu items.

The module menu has a single entry: **DevOps** → opens the SPA.

### 4.2 Layout (matching Odoo.sh)

```
┌──────────────────────────────────────────────────────────────────────┐
│ NAVBAR                                                               │
│ PMB DevOps │ Branches │ Builds │ Status │ Logs │ Settings │ Docs │ [Project ▼] │
├─────────────────┬────────────────────────────────────────────────────┤
│ SIDEBAR          │ MAIN CONTENT                                      │
│                  │                                                    │
│ [Filter...]      │ {branch_name}                                     │
│                  │ ┌─────────────────────────────────────────────┐   │
│ PRODUCTION       │ │ Clone │ Fork │ Merge │ SSH │ SQL │ Delete  │   │
│  ● main    19.0  │ └─────────────────────────────────────────────┘   │
│                  │                                                    │
│ STAGING        + │ ┌─────────────────────────────────────────────┐   │
│  ● staging  19.0 │ │ HISTORY│ AI │ SHELL↗│ LOGS │ BACKUPS│TOOLS │   │
│                  │ └─────────────────────────────────────────────┘   │
│ DEVELOPMENT    + │                                                    │
│  ○ dev-1    19.0 │ [Tab content area]                                │
│  ● dev-2    19.0 │                                                    │
│                  │                                                    │
└─────────────────┴────────────────────────────────────────────────────┘
```

### 4.3 Navbar Tabs

| Tab | Content |
|-----|---------|
| **Branches** | Default view — sidebar + branch detail |
| **Builds** | Global build list across all instances |
| **Status** | Dashboard: all instances with status dots, resource usage |
| **Logs** | Aggregated logs across instances |
| **Settings** | Project settings, limits, AI config, members |
| **Docs** | Built-in manual (HTML page) |

### 4.4 Sidebar

- **Filter input** — filters branches by name
- **Production section** — always shows 1 branch, no "+" button
- **Staging section** — "+" button to create new staging instance
- **Development section** — "+" button to create new development instance
- Each branch entry shows:
  - Branch name (truncated if long)
  - Odoo version (19.0)
  - Status dot: ● green=running, ● red=error, ● yellow=creating, ○ gray=stopped
- Click on branch → loads its content in main area
- Selected branch highlighted

### 4.5 Branch Detail — Action Bar

When a branch/instance is selected, top bar shows:

| Action | Function |
|--------|---------|
| **Clone** | Shows `git clone` command in a copyable box |
| **Fork** | Creates new staging/dev instance from this branch |
| **Merge** | Merge this branch into production (or staging). Wizard with confirmation. |
| **SSH** | Shows SSH command: `ssh odooal@server -p 22` |
| **SQL** | Shows psql command: `psql -h localhost -U odooal {db_name}` |
| **Delete** | Destroys the instance (disabled for production) |

### 4.6 Branch Detail — Content Tabs

| Tab | Content |
|-----|---------|
| **HISTORY** | Git commit log for this branch. Each commit: hash, message, author, date. |
| **AI** | Claude Code terminal (xterm.js) — reuses existing terminal controller + bridge.py |
| **SHELL** | Bash terminal (xterm.js) — opens in context of instance directory |
| **LOGS** | Live journalctl for this instance's service — xterm.js readonly |
| **BACKUPS** | List of backups for this instance + Create Backup button |
| **UPGRADE** | Deploy wizard: AI tests → backup → git pull → restart → verify → rollback |
| **TOOLS** | Plugins management, instance config, restart/stop buttons |

### 4.7 "+" Create Instance Dialog

When user clicks "+" next to Staging or Development:

```
┌──────────────────────────────────┐
│ Create Staging Instance          │
│                                  │
│ Name: [staging-hotfix       ]    │
│ Branch from: [main          ▼]   │
│ Clone DB from: [production  ▼]   │
│                                  │
│        [Cancel]  [Create]        │
└──────────────────────────────────┘
```

For development:
- Branch from defaults to staging
- Clone DB from defaults to staging
- Name auto-suggested from branch name

### 4.8 Project Selector (navbar dropdown)

Top-right dropdown showing all projects the user has access to. Switching projects reloads the sidebar with that project's branches/instances.

---

## 5. File Structure (changes from v1)

```
pmb_devops/
├── models/
│   ├── devops_instance.py          # NEW — core instance model + infrastructure
│   ├── devops_instance_infra.py    # NEW — create/destroy pipelines
│   ├── devops_project.py           # MODIFIED — now represents business
│   ├── devops_branch.py            # MODIFIED — add instance_id
│   ├── devops_build.py             # MODIFIED — add instance_id
│   ├── devops_backup.py            # MODIFIED — add instance_id
│   ├── devops_log.py               # MODIFIED — add instance_id
│   └── (rest unchanged)
├── static/src/
│   ├── pmb_app/                    # NEW — main SPA component
│   │   ├── pmb_app.js              # Main OWL component
│   │   ├── pmb_app.xml             # Main template
│   │   ├── pmb_app.scss            # Odoo.sh-like dark theme styles
│   │   ├── sidebar.js              # Sidebar sub-component
│   │   ├── sidebar.xml
│   │   ├── branch_detail.js        # Branch detail sub-component
│   │   ├── branch_detail.xml
│   │   ├── tab_history.js          # History tab
│   │   ├── tab_terminal.js         # Terminal tab (reuses xterm.js)
│   │   ├── tab_logs.js             # Logs tab
│   │   ├── tab_backups.js          # Backups tab
│   │   ├── tab_upgrade.js          # Deploy/upgrade tab
│   │   ├── tab_tools.js            # Tools tab
│   │   └── create_dialog.js        # Create instance dialog
│   ├── terminal/                   # KEEP — xterm.js + bridge
│   └── git_graph/                  # REMOVE — replaced by sidebar
├── views/
│   ├── devops_menus.xml            # SIMPLIFIED — single menu entry
│   ├── devops_instance_views.xml   # NEW — fallback views
│   └── res_config_settings_views.xml  # KEEP
├── data/
│   ├── devops_cron.xml             # MODIFIED — add auto-stop, auto-destroy crons
│   └── devops_sudoers.xml          # NEW — sudoers setup
└── (rest unchanged)
```

---

## 6. Security

### 6.1 Groups (unchanged from v1)

```
Superadmin (group_devops_admin)
  └── Developer (group_devops_developer)
       └── Viewer (group_devops_viewer)
```

### 6.2 Instance Access (new)

- **Superadmin**: sees all instances in all projects
- **Admin (project member role=admin)**: sees all instances in their project, can create staging/dev, can merge to production
- **Developer (project member role=developer)**: sees assigned instances only + production (readonly). Can create development (auto-assigned). Cannot create staging. Cannot merge to production.
- **Viewer (project member role=viewer)**: sees assigned instances only, read-only

### 6.3 Record Rules for devops.instance

```xml
<!-- Admin sees all -->
<record id="rule_instance_admin" model="ir.rule">
    <field name="model_id" ref="model_devops_instance"/>
    <field name="domain_force">[(1, '=', 1)]</field>
    <field name="groups" eval="[(4, ref('group_devops_admin'))]"/>
</record>

<!-- Developer/Viewer sees assigned instances + production -->
<record id="rule_instance_member" model="ir.rule">
    <field name="model_id" ref="model_devops_instance"/>
    <field name="domain_force">['|', ('user_ids', 'in', user.id), ('instance_type', '=', 'production')]</field>
    <field name="groups" eval="[(4, ref('group_devops_viewer'))]"/>
</record>
```

---

## 7. API Endpoints (controllers)

### New/modified endpoints:

```python
# Instance management
POST /devops/instance/create    # Create new instance (staging/dev)
POST /devops/instance/destroy   # Destroy instance
POST /devops/instance/start     # Start instance
POST /devops/instance/stop      # Stop instance
POST /devops/instance/status    # Get instance status

# Branch operations
POST /devops/branch/merge       # Merge branch into target
POST /devops/branch/fork        # Fork branch

# Sidebar data
POST /devops/project/branches   # Get all branches+instances for project

# Terminal (unchanged)
POST /devops/terminal/start     # Start terminal session
POST /devops/terminal/read      # Read terminal output
POST /devops/terminal/write     # Write terminal input
POST /devops/terminal/stop      # Stop terminal session
```

---

## 8. Odoo 19 Constraints (unchanged)

1. Security groups use `res.groups.privilege`
2. Search views: NO `<group expand="0">`, use `<separator/>`
3. Actions: `target='main'` NOT `target='inline'`
4. Constraints use `models.Constraint()` NOT `_sql_constraints`
5. PTY bridge runs outside Odoo's gevent workers
6. Claude CLI runs locally, not via SSH

---

## 9. Production Safety Rules (unchanged)

1. **NEVER** `systemctl stop` production — always `restart`
2. **ALWAYS** backup before deploying to production
3. **ALWAYS** verify HTTP 200 after production restart
4. **AUTO-ROLLBACK** if production deploy fails
5. Production instance **CANNOT** be destroyed from UI
6. Production instance **CANNOT** be stopped from UI

---

## 10. Infrastructure Prerequisites

These must be in place on the server:

1. **Wildcard DNS**: `*.cremara.com` → 70.35.200.175 (DONE)
2. **Nginx installed** with sites-enabled pattern
3. **Certbot installed** with nginx plugin
4. **Sudoers for odooal**: systemctl, nginx, certbot, createdb, dropdb, pg_dump, tee, rm, ln, mkdir, chown, chmod
5. **PostgreSQL**: odooal user with createdb privilege
6. **Directory**: `/opt/instances/` owned by odooal
7. **Odoo source**: shared at `/opt/odooAL/odoo/` (symlinked into instances)

---

## 11. Testing Strategy

- **Playwright E2E**: Login → Create project → Register production → Create staging → Verify staging URL responds → Open Shell → Open Logs → Create development → Merge dev→staging → Destroy development → Screenshots for manual
- **Infrastructure tests**: Verify systemd service created, nginx vhost works, SSL cert obtained, DB cloned correctly
- **Permission tests**: Developer cannot merge to production, viewer cannot create instances
