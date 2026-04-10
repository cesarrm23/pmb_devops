from . import controllers
from . import models
from . import wizard


def _post_init_hook(env):
    """Recompute stored fields for instances created outside the ORM (e.g. SQL)."""
    instances = env['devops.instance'].search([])
    instances._compute_full_domain()
    instances._compute_url()
