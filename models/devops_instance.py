from odoo import _, api, fields, models
from odoo.exceptions import UserError


class DevopsInstance(models.Model):
    _name = 'devops.instance'
    _description = 'Instancia Odoo'
    _inherit = ['mail.thread']
    _order = 'instance_type, name'

    project_id = fields.Many2one(
        'devops.project', string='Proyecto', required=True, ondelete='cascade',
    )
    branch_id = fields.Many2one(
        'devops.branch', string='Rama', ondelete='set null',
    )
    name = fields.Char(string='Nombre', required=True)  # "staging-1", "dev-cesar"

    instance_type = fields.Selection([
        ('production', 'Production'),
        ('staging', 'Staging'),
        ('development', 'Development'),
    ], string='Tipo', required=True)

    # ---- Git branch ----
    git_branch = fields.Char(
        string='Branch Git',
        help='Nombre del branch git real (ej: main, staging, dev-feature)',
    )

    # ---- Infrastructure (auto-generated) ----
    subdomain = fields.Char(string='Subdominio')
    full_domain = fields.Char(
        string='Dominio Completo', compute='_compute_full_domain', store=True,
    )
    port = fields.Integer(string='Puerto')
    gevent_port = fields.Integer(string='Puerto Gevent')
    service_name = fields.Char(string='Servicio Systemd')
    database_name = fields.Char(string='Base de Datos')
    odoo_config_path = fields.Char(string='Config Odoo')
    instance_path = fields.Char(string='Directorio')
    url = fields.Char(string='URL', compute='_compute_url', store=True)
    nginx_config_path = fields.Char(string='Config Nginx')

    # ---- State ----
    state = fields.Selection([
        ('creating', 'Creando'),
        ('running', 'Ejecutando'),
        ('stopped', 'Detenido'),
        ('error', 'Error'),
        ('destroying', 'Destruyendo'),
    ], string='Estado', default='creating', tracking=True)
    creation_step = fields.Char(
        string='Paso Actual', help='Current step during creation',
    )
    last_activity = fields.Datetime(
        string='Última Actividad', default=fields.Datetime.now,
    )

    # ---- Users ----
    user_ids = fields.Many2many('res.users', string='Usuarios Asignados')

    # ---- Relations ----
    build_ids = fields.One2many('devops.build', 'instance_id', string='Builds')
    backup_ids = fields.One2many(
        'devops.backup', 'instance_id', string='Backups',
    )
    log_ids = fields.One2many('devops.log', 'instance_id', string='Logs')

    # ---- Cloned from ----
    cloned_from_id = fields.Many2one('devops.instance', string='Clonado de')

    # ---- Counts ----
    build_count = fields.Integer(compute='_compute_counts')
    backup_count = fields.Integer(compute='_compute_counts')

    # ------------------------------------------------------------------
    # Constraints (Odoo 19 pattern)
    # ------------------------------------------------------------------

    _unique_name = models.Constraint(
        'UNIQUE(project_id, name)',
        'Ya existe una instancia con este nombre en el proyecto.',
    )
    _unique_port = models.Constraint(
        'UNIQUE(port)',
        'El puerto ya está en uso por otra instancia.',
    )

    # ------------------------------------------------------------------
    # Computed methods
    # ------------------------------------------------------------------

    @api.depends('subdomain', 'project_id.domain', 'instance_type')
    def _compute_full_domain(self):
        for rec in self:
            if rec.instance_type == 'production':
                rec.full_domain = rec.project_id.domain or ''
            elif rec.subdomain and rec.project_id.domain:
                rec.full_domain = f"{rec.subdomain}.{rec.project_id.domain}"
            else:
                rec.full_domain = ''

    @api.depends('full_domain')
    def _compute_url(self):
        for rec in self:
            rec.url = f"https://{rec.full_domain}" if rec.full_domain else ''

    def _compute_counts(self):
        for rec in self:
            rec.build_count = len(rec.build_ids)
            rec.backup_count = len(rec.backup_ids)

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------

    def _find_free_port(self):
        """Find next available port starting from 8080."""
        used_ports = set(
            self.search([('port', '!=', False)]).mapped('port')
        )
        for port in range(8080, 8200):
            if port not in used_ports:
                return port
        raise UserError(_("No hay puertos disponibles (8080-8199)."))

    def _update_activity(self):
        """Update last_activity timestamp."""
        self.write({'last_activity': fields.Datetime.now()})

    # ------------------------------------------------------------------
    # Action stubs (implemented in Task 3 — devops_instance_infra.py)
    # ------------------------------------------------------------------

    def action_create_instance(self):
        """Full automated creation pipeline. Implemented in devops_instance_infra.py"""
        pass

    def action_start(self):
        pass

    def action_stop(self):
        pass

    def action_restart(self):
        pass

    def action_destroy(self):
        pass
