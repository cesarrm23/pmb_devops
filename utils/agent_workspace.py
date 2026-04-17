"""Inject a project-scoped CLAUDE.md into the agent's workspace.

Claude Code reads CLAUDE.md from the cwd it was launched in. This is the
canonical way to give AI agents durable, project-specific rules. We write
one at session start so every agent receives the same workspace discipline
(never drift to /tmp), manifest-versioning rules, and project context.
"""
import logging
import os
import shlex

from . import ssh_utils

_logger = logging.getLogger(__name__)

# Marker so we only overwrite files we authored — never clobber user-written
# CLAUDE.md or append duplicates.
MARKER_BEGIN = '<!-- pmb-devops:begin -->'
MARKER_END = '<!-- pmb-devops:end -->'


def _rules_body(project, instance, workspace):
    project_name = (project.name if project else 'unknown').strip()
    instance_label = (
        f"{instance.instance_type} ({instance.name})"
        if instance else 'project-level (no instance)'
    )
    repo_path = workspace or (project.repo_path if project else '') or ''
    prod_branch = (project.production_branch or 'main') if project else 'main'
    staging_branch = 'staging'
    if project:
        try:
            from odoo.http import request
            inst = request.env['devops.instance'].sudo().search([
                ('project_id', '=', project.id),
                ('instance_type', '=', 'staging'),
            ], limit=1)
            if inst and inst.branch_id:
                staging_branch = inst.branch_id.name
            elif project.staging_branch:
                staging_branch = project.staging_branch
        except Exception:
            staging_branch = project.staging_branch or 'staging'
    return f"""{MARKER_BEGIN}
# PMB DevOps Agent Rules — {project_name}

You are operating inside the `{project_name}` project, instance
**{instance_label}**. The workspace root for this session is:

    {repo_path}

## Workspace discipline (non-negotiable)

- **Never** `cd` to `/tmp/*`, `/var/tmp/*`, or any path outside the
  workspace root shown above. If you need a scratch file, place it in
  `{repo_path}/.pmb_scratch/` (create the directory if absent).
- If you notice the cwd has drifted outside the workspace, `cd` back to
  the workspace root before continuing.
- Do not create sibling repo clones elsewhere. Edit the checkout that
  lives at the workspace root.

## Git flow

- Branches in this project: production = `{prod_branch}`,
  staging = `{staging_branch}`. Dev work goes on any other branch.
- After pushing a dev branch, promote via the DevOps UI
  (Ramas → merge). Do not push directly to `{prod_branch}` or
  `{staging_branch}`.
- Commit to the branch your instance already tracks. Do not switch
  branches unless the user asks.

## Module versioning (Odoo addons)

When you modify files under any Odoo addon directory (i.e. any folder
containing `__manifest__.py`):

1. Open that addon's `__manifest__.py`.
2. Bump the `version` string using Odoo's `major.minor.x.y.z` scheme —
   increment the last segment by 1 (e.g. `18.0.1.0.3` → `18.0.1.0.4`).
   If the version is missing or non-standard, set it to
   `18.0.1.0.1`.
3. Include the manifest bump in the same commit as the module change,
   and mention the module name + new version in the commit message.

This discipline is what lets the DevOps "Módulos" panel detect that
an instance needs an upgrade.

## Before you finish

- Leave the working tree in the branch the session started on.
- If you edited addon code, confirm the manifest bump is in `git diff`
  before handing back to the user.
{MARKER_END}
"""


def _read_remote(project, path):
    r = ssh_utils.execute_command(
        project, ['cat', path], timeout=10,
    )
    return r.stdout if r.returncode == 0 else ''


def _write_remote(project, path, content):
    # Use a heredoc via shell exec to preserve content verbatim.
    tmp_path = f'{path}.pmb_tmp'
    safe = content.replace("'", "'\\''")
    cmd = (
        f"cat > {shlex.quote(tmp_path)} <<'PMB_EOF'\n{content}\nPMB_EOF\n"
        f"mv {shlex.quote(tmp_path)} {shlex.quote(path)}"
    )
    ssh_utils.execute_command_shell(project, cmd, timeout=15)


def _merge(existing, block):
    """Replace the pmb-devops section if present; otherwise append."""
    if MARKER_BEGIN in existing and MARKER_END in existing:
        before, _, rest = existing.partition(MARKER_BEGIN)
        _, _, after = rest.partition(MARKER_END)
        return f"{before.rstrip()}\n\n{block.strip()}\n{after.lstrip()}".rstrip() + '\n'
    if existing.strip():
        return f"{existing.rstrip()}\n\n{block.strip()}\n"
    return block


def ensure_claude_md(project, instance, workspace):
    """Write/refresh CLAUDE.md in `workspace` with pmb-devops rules.

    Safe to call on every AI session start: existing user content is
    preserved; only the marked block is refreshed.
    """
    if not workspace:
        return False
    try:
        block = _rules_body(project, instance, workspace)
        is_ssh = project and project.connection_type == 'ssh' and project.ssh_host
        path = os.path.join(workspace, 'CLAUDE.md')
        if is_ssh:
            existing = _read_remote(project, path)
            merged = _merge(existing, block)
            _write_remote(project, path, merged)
        else:
            existing = ''
            if os.path.isfile(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        existing = f.read()
                except OSError:
                    existing = ''
            merged = _merge(existing, block)
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(merged)
            except OSError as e:
                _logger.warning("Could not write %s: %s", path, e)
                return False
        return True
    except Exception as e:
        _logger.warning("ensure_claude_md failed: %s", e)
        return False
