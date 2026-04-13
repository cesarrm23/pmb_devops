from . import controllers
from . import models
from . import wizard


def _post_init_hook(env):
    """Post-install/upgrade hook: cleanup, migrations, recompute."""
    import os
    import logging
    _logger = logging.getLogger(__name__)

    # 1. Recompute stored fields
    instances = env['devops.instance'].search([])
    instances._compute_full_domain()
    instances._compute_url()

    # 2. Clean orphaned groups (from old module versions)
    cr = env.cr
    # Remove access rights pointing to non-existent groups
    cr.execute("""
        DELETE FROM ir_model_access
        WHERE group_id IS NOT NULL
        AND group_id NOT IN (SELECT id FROM res_groups)
    """)
    orphaned_access = cr.rowcount
    if orphaned_access:
        _logger.info("Cleaned %d orphaned access rights", orphaned_access)

    # Remove old privilege records not matching current module
    cr.execute("""
        DELETE FROM res_groups_privilege
        WHERE id NOT IN (
            SELECT privilege_id FROM res_groups WHERE privilege_id IS NOT NULL
        )
        AND name::text NOT LIKE '%%PatchMyByte%%'
    """)

    # 3. Clean stuck instances (creating > 30min or error without creation_pid)
    cr.execute("""
        UPDATE devops_instance
        SET state = 'error', creation_step = 'Limpiado por post_init_hook'
        WHERE state = 'creating'
        AND (creation_pid IS NULL OR creation_pid = 0)
        AND create_date < NOW() - INTERVAL '30 minutes'
    """)
    stuck = cr.rowcount
    if stuck:
        _logger.info("Marked %d stuck creating instances as error", stuck)

    # 4. Ensure Claude credentials are readable by all Odoo service users
    home = os.path.expanduser('~')
    claude_dir = os.path.join(home, '.claude')
    creds_file = os.path.join(claude_dir, '.credentials.json')
    try:
        if os.path.isdir(claude_dir):
            os.chmod(claude_dir, 0o755)
        if os.path.isfile(creds_file):
            os.chmod(creds_file, 0o644)
        # Also make session dirs traversable
        for dirpath, dirnames, _ in os.walk(claude_dir):
            for d in dirnames:
                try:
                    os.chmod(os.path.join(dirpath, d), 0o755)
                except Exception:
                    pass
    except Exception as e:
        _logger.warning("Could not fix Claude permissions: %s", e)

    _logger.info("post_init_hook completed")
