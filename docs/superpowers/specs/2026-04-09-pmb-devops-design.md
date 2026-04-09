# PMB DevOps — Design Specification

**Date:** 2026-04-09
**Author:** PatchMyByte
**Module:** pmb_devops
**Target:** asistentelisto.com (odooAL, port 8075)
**Repo:** cesarrm23/pmb_devops (dedicated, separate from cremara_addons)

---

## 1. Overview

`pmb_devops` is an Odoo 19 module that replicates Odoo.sh functionality for managing multiple Odoo instances (businesses) from a single platform. It runs on asistentelisto.com and administers projects like cremara.com remotely.

### Core Concept

- **Platform host:** asistentelisto.com (odooAL instance)
- **Managed projects:** cremara (first), future businesses
- **Architecture:** Monolith module with multi-project support
- **Execution layer:** Local subprocess or SSH for remote servers

---

## 2. Module Identity

- **Technical name:** `pmb_devops`
- **Display name:** PatchMyByte DevOps
- **Author:** PatchMyByte
- **License:** LGPL-3
- **Category:** Services/DevOps
- **Version:** 19.0.1.0.0
- **Dependencies:** `base`, `mail`
- **XML ID prefix:** `pmb_devops.`
- **Config param prefix:** `pmb_devops.`
- **No references to "cremara" anywhere in the module**

---

## 3. File Structure

```
pmb_devops/
├── __init__.py
├── __manifest__.py
├── controllers/
│   ├── __init__.py
│   ├── terminal_controller.py
│   └── devops_controller.py
├── data/
│   ├── devops_cron.xml
│   └── devops_data.xml
├── models/
│   ├── __init__.py
│   ├── devops_project.py
│   ├── devops_project_member.py
│   ├── devops_branch.py
│   ├── devops_build.py
│   ├── devops_log.py
│   ├── devops_backup.py
│   ├── devops_ai_assistant.py
│   ├── devops_deploy_ai.py
│   ├── devops_plugin.py
│   └── res_config_settings.py
├── security/
│   ├── devops_security.xml
│   └── ir.model.access.csv
├── static/
│   ├── description/
│   │   ├── icon.svg
│   │   └── manual/
│   │       ├── img/           # Annotated Playwright screenshots
│   │       └── index.html     # Built-in user manual
│   └── src/
│       ├── terminal/
│       │   ├── devops_terminal.js
│       │   └── devops_terminal.xml
│       └── git_graph/
│           ├── git_graph.js
│           └── git_graph.xml
├── utils/
│   ├── __init__.py
│   ├── git_utils.py
│   └── ssh_utils.py
├── views/
│   ├── devops_project_views.xml
│   ├── devops_branch_views.xml
│   ├── devops_build_views.xml
│   ├── devops_log_views.xml
│   ├── devops_backup_views.xml
│   ├── devops_deploy_ai_views.xml
│   ├── devops_plugin_views.xml
│   ├── res_config_settings_views.xml
│   └── devops_menus.xml
└── wizard/
    ├── __init__.py
    ├── ai_assistant_wizard.py
    ├── ai_assistant_wizard_views.xml
    ├── claude_login_wizard.py
    ├── claude_login_wizard_views.xml
    ├── deploy_wizard.py
    └── deploy_wizard_views.xml
```

---

## 4. Security — Hybrid Multi-User Roles

### 4.1 Groups (Odoo 19 pattern: res.groups.privilege)

```
Superadmin (group_devops_admin)
  └── implies → Developer (group_devops_developer)
       └── implies → Viewer (group_devops_viewer)
```

- **Superadmin:** Full access to all projects. Can deploy to production anywhere. Manages users and roles. `base.user_admin` gets this by default.
- **Developer:** Can work in staging/development on assigned projects. Can use terminal, AI, view logs. CANNOT deploy to production.
- **Viewer:** Read-only access to assigned projects. Can view branches, builds, logs, backups. Cannot execute any action.

### 4.2 Per-Project Roles (devops.project.member)

```python
class DevopsProjectMember(models.Model):
    _name = 'devops.project.member'

    project_id = fields.Many2one('devops.project', required=True, ondelete='cascade')
    user_id = fields.Many2one('res.users', required=True)
    role = fields.Selection([
        ('admin', 'Admin'),
        ('developer', 'Developer'),
        ('viewer', 'Viewer'),
    ], required=True, default='developer')
```

### 4.3 Record Rules

- **Superadmin (group_devops_admin):** `[(1, '=', 1)]` — sees everything
- **Developer/Viewer:** `[('member_ids.user_id', '=', user.id)]` — only assigned projects
- **Production deploy:** Python validation — only project admin or superadmin
- **AI Assistant history:** Users see only their own queries (except superadmin)

---

## 5. Models

### 5.1 devops.project (enhanced)

Everything from the old module plus:

```python
# Connection config (NEW)
connection_type = fields.Selection([
    ('local', 'Local (mismo servidor)'),
    ('ssh', 'SSH Remoto'),
], default='local')
ssh_host = fields.Char('Host SSH')
ssh_user = fields.Char('Usuario SSH')
ssh_port = fields.Integer('Puerto SSH', default=22)
ssh_key_path = fields.Char('Ruta llave SSH')

# Project members (NEW)
member_ids = fields.One2many('devops.project.member', 'project_id')
```

Cremara config: `connection_type='local'`, repo_path='/opt/odoo19Test/cremara_addons', etc.

### 5.2 devops.branch

Same as old module. Fields: project_id, name, branch_type (production/staging/development), commit info, diff stats.

### 5.3 devops.build

Same as old module. Fields: project_id, branch_id, state, commit info, build_log, error_log, duration. All subprocess calls go through `ssh_utils.execute_command()`.

### 5.4 devops.log

Same as old module. Fetches journalctl logs from the service.

### 5.5 devops.backup

Same as old module. pg_dump with cron, retention cleanup. Uses `ssh_utils` for remote execution.

### 5.6 devops.ai.assistant

Same as old module. Wizard-based: prompt → response. Claude CLI primary, API fallback. Config param prefix changed to `pmb_devops.`.

### 5.7 devops.deploy.ai

Same as old module. Tests → backup → pull → restart → verify → auto-rollback. Permission check: only project admin or superadmin can deploy to production.

### 5.8 devops.plugin

Same as old module. Lists Claude Code plugins, install/uninstall.

---

## 6. Execution Layer — ssh_utils.py

All subprocess calls go through a unified abstraction:

```python
def execute_command(project, command, timeout=30, cwd=None):
    """Execute command locally or via SSH based on project config."""
    if project.connection_type == 'local':
        return subprocess.run(
            command, capture_output=True, text=True,
            timeout=timeout, cwd=cwd or project.repo_path,
        )
    else:
        ssh_args = ['ssh']
        if project.ssh_key_path:
            ssh_args += ['-i', project.ssh_key_path]
        if project.ssh_port != 22:
            ssh_args += ['-p', str(project.ssh_port)]
        ssh_args.append(f'{project.ssh_user}@{project.ssh_host}')

        cmd_str = ' '.join(command)
        if cwd:
            cmd_str = f'cd {cwd} && {cmd_str}'
        ssh_args.append(cmd_str)

        return subprocess.run(
            ssh_args, capture_output=True, text=True, timeout=timeout,
        )
```

This is used by: git_utils.py, devops_build.py, devops_backup.py, devops_log.py, terminal_controller.py.

---

## 7. Frontend (OWL Components)

### 7.1 Terminal (xterm.js)

Same architecture as old module:
- 3 tabs: AI (Claude CLI), Shell (bash -i), Logs (journalctl -f)
- bridge.py runs outside Odoo (avoids gevent conflicts)
- File-based I/O between Odoo controllers and bridge process
- xterm.js loaded from CDN with FitAddon
- Tokyo Night theme

### 7.2 Git Graph

Same as old module:
- Canvas-based visualization
- Sidebar with Production/Staging/Development sections
- Commit nodes, branch lines, merge indicators

---

## 8. Git Repo Organization — cremara_addons

As part of this project, organize cremara's existing repo:

### Current state:
- Branches: 18.0, 19.0, main
- HEAD → main
- Working branch: 19.0

### Target state:
1. Merge 19.0 → main (sync)
2. Create `staging` branch from main
3. Create `development` branch from staging
4. Protect `main` on GitHub (no direct push)

### Branch-to-instance mapping:
| Branch | Instance | Service |
|--------|----------|---------|
| main | cremara.com | odoo19.service |
| staging | test.cremara.com | odoo19Test.service |
| development | (local dev) | — |

### Workflow:
```
development → staging → main (production)
```

---

## 9. pmb_devops Repo Setup

- **GitHub repo:** cesarrm23/pmb_devops
- **Clone to:** /opt/odooAL/custom_addons/pmb_devops
- **Initial branches:** main, staging, development
- **Protect main** from direct push

---

## 10. Built-in User Manual

### Generation process:
1. Playwright tests capture screenshots at key UI points
2. Screenshots are annotated with arrows, highlights, and numbered steps using Pillow
3. Saved to `static/description/manual/img/`
4. `index.html` assembles all sections into a navigable manual

### Access from Odoo:
- Menu: DevOps > Configuración > Documentación
- Opens as ir.actions.act_url pointing to the static HTML

### Manual sections:
1. Crear proyecto (form fields annotated)
2. Configurar conexión SSH
3. Sincronizar branches
4. Usar terminal (AI/Shell/Logs tabs)
5. Crear backup
6. Ejecutar build
7. Deploy con IA (wizard walkthrough)
8. Gestionar miembros y roles
9. Git Graph navigation

---

## 11. Cron Jobs

- **Auto backup:** Daily, all running projects with database configured
- **Backup retention:** Clean backups older than 7 days
- **Health check:** Every 15 min, check service status of all projects

---

## 12. Technical Constraints (Odoo 19)

These MUST be followed during implementation:

1. Security groups use `res.groups.privilege`, NOT `ir.module.category`
2. Search views: NO `<group expand="0">`, use `<separator/>` instead
3. Actions: `target='main'` NOT `target='inline'`
4. Computed fields without `store=True` cannot be used in search view domains
5. PTY/select() doesn't work inside Odoo gevent — must use external bridge process
6. `claude auth login` uses HTTP callback, not stdin — use ANTHROPIC_API_KEY for headless
7. Text fields cannot be used as search fields

---

## 13. Data Files Load Order

```python
'data': [
    'security/devops_security.xml',
    'security/ir.model.access.csv',
    'data/devops_cron.xml',
    'data/devops_data.xml',
    'wizard/ai_assistant_wizard_views.xml',
    'wizard/deploy_wizard_views.xml',
    'wizard/claude_login_wizard_views.xml',
    'views/devops_project_views.xml',
    'views/devops_branch_views.xml',
    'views/devops_build_views.xml',
    'views/devops_log_views.xml',
    'views/devops_backup_views.xml',
    'views/devops_deploy_ai_views.xml',
    'views/devops_plugin_views.xml',
    'views/res_config_settings_views.xml',
    'views/devops_menus.xml',
],
'assets': {
    'web.assets_backend': [
        'pmb_devops/static/src/terminal/devops_terminal.js',
        'pmb_devops/static/src/terminal/devops_terminal.xml',
        'pmb_devops/static/src/git_graph/git_graph.js',
        'pmb_devops/static/src/git_graph/git_graph.xml',
    ],
},
```

---

## 14. Testing Strategy

- **Playwright:** Full E2E tests on asistentelisto.com
  - Install module
  - Create project (cremara)
  - Sync branches
  - Open terminal (each tab)
  - Create backup
  - Run build
  - Test AI assistant wizard
  - Test deploy wizard
  - Verify Git Graph renders
  - Capture annotated screenshots for manual at each step
- **Verification:** After each major step, check Odoo logs for errors
