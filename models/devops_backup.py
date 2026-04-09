import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

from ..utils import ssh_utils

_logger = logging.getLogger(__name__)

DEFAULT_BACKUP_PATH = '/var/backups/odoo'


class DevopsBackup(models.Model):
    _name = 'devops.backup'
    _description = 'Backup de Base de Datos DevOps'
    _order = 'started_at desc, id desc'

    name = fields.Char(string='Nombre', required=True)
    project_id = fields.Many2one(
        'devops.project', string='Proyecto',
        required=True, ondelete='cascade', index=True,
    )
    instance_id = fields.Many2one('devops.instance', string='Instancia', ondelete='cascade')
    state = fields.Selection([
        ('pending', 'Pendiente'),
        ('running', 'En Progreso'),
        ('done', 'Completado'),
        ('failed', 'Fallido'),
    ], string='Estado', default='pending', required=True)
    backup_type = fields.Selection([
        ('full', 'Completo'),
        ('sql', 'Solo SQL'),
        ('filestore', 'Solo Filestore'),
    ], string='Tipo', default='sql', required=True)
    trigger = fields.Selection([
        ('manual', 'Manual'),
        ('cron', 'Cron'),
        ('pre_deploy', 'Pre-Deploy'),
    ], string='Disparador', default='manual', required=True)
    database_name = fields.Char(string='Base de Datos')
    file_path = fields.Char(string='Ruta del Archivo')
    file_size = fields.Float(string='Tamaño (MB)')
    started_at = fields.Datetime(string='Inicio')
    finished_at = fields.Datetime(string='Fin')
    duration = fields.Float(
        string='Duración (s)', compute='_compute_duration', store=True,
    )
    error_message = fields.Text(string='Mensaje de Error')
    notes = fields.Text(string='Notas')
    created_by = fields.Many2one(
        'res.users', string='Creado por',
        default=lambda self: self.env.user,
    )

    @api.depends('started_at', 'finished_at')
    def _compute_duration(self):
        for rec in self:
            if rec.started_at and rec.finished_at:
                delta = rec.finished_at - rec.started_at
                rec.duration = delta.total_seconds()
            else:
                rec.duration = 0.0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_backup_path(self):
        """Return configured backup path or default."""
        return self.env['ir.config_parameter'].sudo().get_param(
            'pmb_devops.backup_path', DEFAULT_BACKUP_PATH,
        )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_create_backup(self, project=None, trigger='manual'):
        """Create a new backup record and execute it.

        Can be called on an empty recordset with a project argument.
        """
        if project is None:
            project = self.project_id
        if not project:
            raise UserError("Se requiere un proyecto para crear el backup.")

        db_name = project.database_name or 'odoo'
        timestamp = fields.Datetime.now()
        ts_str = timestamp.strftime('%Y%m%d_%H%M%S')
        backup_path = self._get_backup_path() if self else \
            self.env['ir.config_parameter'].sudo().get_param(
                'pmb_devops.backup_path', DEFAULT_BACKUP_PATH,
            )
        file_name = f"{db_name}_{ts_str}.sql.gz"
        file_path = f"{backup_path}/{file_name}"

        backup = self.env['devops.backup'].create({
            'name': file_name,
            'project_id': project.id,
            'state': 'pending',
            'backup_type': 'sql',
            'trigger': trigger,
            'database_name': db_name,
            'file_path': file_path,
            'started_at': timestamp,
            'created_by': self.env.user.id,
        })
        backup.action_run_backup()
        return backup

    def action_run_backup(self):
        """Execute the backup via SSH/local command."""
        self.ensure_one()
        self.write({'state': 'running', 'started_at': fields.Datetime.now()})

        backup_dir = '/'.join(self.file_path.split('/')[:-1])
        try:
            # Ensure backup directory exists
            ssh_utils.execute_command(
                self.project_id, ['mkdir', '-p', backup_dir], timeout=10,
            )

            # Run pg_dump
            cmd = f"pg_dump {self.database_name} | gzip > {self.file_path}"
            result = ssh_utils.execute_command_shell(
                self.project_id, cmd, timeout=600,
            )

            if result.returncode != 0:
                self.write({
                    'state': 'failed',
                    'finished_at': fields.Datetime.now(),
                    'error_message': result.stderr or 'pg_dump falló sin mensaje.',
                })
                return

            # Get file size
            size_result = ssh_utils.execute_command(
                self.project_id,
                ['stat', '-c', '%s', self.file_path],
                timeout=10,
            )
            file_size_bytes = 0
            if size_result.returncode == 0 and size_result.stdout.strip().isdigit():
                file_size_bytes = int(size_result.stdout.strip())

            self.write({
                'state': 'done',
                'finished_at': fields.Datetime.now(),
                'file_size': round(file_size_bytes / (1024 * 1024), 2),
            })
            _logger.info(
                "Backup completado: %s (%.2f MB)",
                self.file_path, self.file_size,
            )
        except Exception as e:
            _logger.exception("Error en backup %s", self.name)
            self.write({
                'state': 'failed',
                'finished_at': fields.Datetime.now(),
                'error_message': str(e),
            })

    def action_restore(self):
        """Show manual restore instructions (safety measure)."""
        self.ensure_one()
        instructions = (
            f"Para restaurar este backup manualmente:\n\n"
            f"1. Detener el servicio:\n"
            f"   sudo systemctl stop {self.project_id.service_name or 'odoo'}.service\n\n"
            f"2. Restaurar la base de datos:\n"
            f"   gunzip -c {self.file_path} | psql {self.database_name}\n\n"
            f"3. Reiniciar el servicio:\n"
            f"   sudo systemctl restart {self.project_id.service_name or 'odoo'}.service\n\n"
            f"NOTA: Se recomienda crear un backup de la BD actual antes de restaurar."
        )
        raise UserError(instructions)

    def action_download(self):
        """Create ir.attachment from backup file for download."""
        self.ensure_one()
        if self.state != 'done':
            raise UserError("Solo se pueden descargar backups completados.")

        try:
            result = ssh_utils.execute_command_shell(
                self.project_id,
                f"base64 {self.file_path}",
                timeout=120,
            )
            if result.returncode != 0:
                raise UserError(
                    f"No se pudo leer el archivo de backup:\n{result.stderr}"
                )

            attachment = self.env['ir.attachment'].create({
                'name': self.name,
                'type': 'binary',
                'datas': result.stdout.strip(),
                'mimetype': 'application/gzip',
            })
            return {
                'type': 'ir.actions.act_url',
                'url': f'/web/content/{attachment.id}?download=true',
                'target': 'new',
            }
        except UserError:
            raise
        except Exception as e:
            raise UserError(f"Error descargando backup: {e}") from e

    # ------------------------------------------------------------------
    # Cron
    # ------------------------------------------------------------------

    @api.model
    def _cron_auto_backup(self):
        """Backup all running projects and clean old backups (> 7 days)."""
        projects = self.env['devops.project'].search([
            ('state', '=', 'active'),
        ])
        for project in projects:
            try:
                self.action_create_backup(project=project, trigger='cron')
            except Exception:
                _logger.exception(
                    "Cron backup failed for project %s", project.name,
                )

        # Clean old backups (older than 7 days)
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=7)
        old_backups = self.search([
            ('state', '=', 'done'),
            ('trigger', '=', 'cron'),
            ('finished_at', '<', cutoff),
        ])
        for backup in old_backups:
            try:
                # Remove file on server
                ssh_utils.execute_command(
                    backup.project_id,
                    ['rm', '-f', backup.file_path],
                    timeout=10,
                )
                backup.write({'state': 'pending', 'notes': 'Limpiado por retención (7 días)'})
            except Exception:
                _logger.warning("Could not delete old backup file: %s", backup.file_path)
        if old_backups:
            old_backups.unlink()
            _logger.info("Cleaned %d old backups", len(old_backups))
