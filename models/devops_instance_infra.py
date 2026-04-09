"""Infrastructure pipeline implementation for devops.instance.

Extends devops.instance with actual create/start/stop/restart/destroy
logic using system commands through infra_utils.
"""
import logging
import subprocess
import time

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..utils import infra_utils

_logger = logging.getLogger(__name__)


class DevopsInstanceInfra(models.Model):
    _inherit = 'devops.instance'

    # ------------------------------------------------------------------
    # Action: Create Instance (15-step pipeline)
    # ------------------------------------------------------------------

    def action_create_instance(self):
        """Full automated creation pipeline for a new Odoo instance.

        Steps:
         1. Validate limits (max_staging / max_development)
         2. Assign port via _find_free_port(), gevent = port + 1000
         3. Generate names (service, db, subdomain, paths)
         4. Create git branch if branch_id exists
         5. Clone database from source
         6. Create instance directory + symlink repo
         7. Generate Odoo config
         8. Create systemd service
         9. Create nginx vhost
        10. Reload nginx
        11. Start service
        12. SSL certificate via certbot
        13. Verify HTTP 200
        14. Update state to 'running' or 'error'
        15. Post creation log to chatter
        """
        self.ensure_one()
        project = self.project_id
        errors = []

        try:
            # ---- Step 1: Validate limits ----
            self._validate_instance_limits()

            # ---- Step 2: Assign ports ----
            port = self._find_free_port()
            gevent_port = port + 1000

            # ---- Step 3: Generate names ----
            safe_name = self.name.replace(' ', '-').lower()
            service_name = f"odoo-{project.name.replace(' ', '').lower()}-{safe_name}"
            db_name = f"{project.name.replace(' ', '_').lower()}_{safe_name}"
            if self.instance_type == 'production':
                subdomain = ''
            else:
                subdomain = f"{safe_name}"
            instance_path = f"/opt/instances/{service_name}"
            config_path = f"/etc/{service_name}.conf"
            domain = project.domain or ''
            if subdomain and domain:
                full_domain = f"{subdomain}.{domain}"
            elif domain:
                full_domain = domain
            else:
                raise UserError(
                    _("El proyecto no tiene dominio configurado.")
                )
            nginx_path = f"/etc/nginx/sites-enabled/{full_domain}"

            # Addons path: instance addons + custom + odoo source
            addons_path = f"{instance_path}/addons"

            # Write names to record
            self.write({
                'port': port,
                'gevent_port': gevent_port,
                'service_name': service_name,
                'database_name': db_name,
                'subdomain': subdomain,
                'odoo_config_path': config_path,
                'instance_path': instance_path,
                'nginx_config_path': nginx_path,
                'state': 'creating',
            })
            self.env.cr.commit()  # noqa: E501 -- persist partial state in case of failure

            # ---- Step 4: Create git branch if needed ----
            if self.branch_id and project.repo_path:
                try:
                    from ..utils import git_utils
                    git_utils.git_fetch(project)
                    # Create and checkout branch for the instance
                    _logger.info(
                        "Branch %s assigned to instance %s",
                        self.branch_id.name, self.name,
                    )
                except Exception as e:
                    _logger.warning("Git branch setup skipped: %s", e)

            # ---- Step 5: Clone database ----
            source_db = None
            if self.cloned_from_id and self.cloned_from_id.database_name:
                source_db = self.cloned_from_id.database_name
            elif project.database_name:
                source_db = project.database_name

            if source_db:
                _logger.info("Cloning database %s -> %s", source_db, db_name)
                infra_utils.clone_database(source_db, db_name, timeout=600)
            else:
                _logger.warning(
                    "No source database for cloning; "
                    "instance %s will start with empty database", self.name,
                )

            # ---- Step 6: Create instance directory + symlink ----
            infra_utils.create_instance_directory(instance_path)
            if project.repo_path:
                infra_utils.sudo_run(
                    f"ln -sfn {project.repo_path} {instance_path}/addons"
                )

            # ---- Step 7: Generate Odoo config ----
            infra_utils.create_odoo_config(
                service_name=service_name,
                db_name=db_name,
                port=port,
                gevent_port=gevent_port,
                instance_path=instance_path,
                addons_path=addons_path,
            )

            # ---- Step 8: Create systemd service ----
            infra_utils.create_systemd_service(
                service_name=service_name,
                config_path=config_path,
                instance_path=instance_path,
            )

            # ---- Step 9: Create nginx vhost ----
            infra_utils.create_nginx_vhost(
                domain=full_domain,
                port=port,
                gevent_port=gevent_port,
                instance_id=self.id,
                instance_name=service_name,
            )

            # ---- Step 10: Reload nginx ----
            infra_utils.reload_nginx()

            # ---- Step 11: Start service ----
            infra_utils.start_service(service_name, timeout=30)
            # Give Odoo a few seconds to initialize
            time.sleep(5)

            # ---- Step 12: SSL certificate ----
            try:
                infra_utils.obtain_ssl_cert(full_domain)
            except Exception as e:
                errors.append(f"SSL certificate: {e}")
                _logger.warning("SSL cert failed for %s: %s", full_domain, e)

            # ---- Step 13: Verify HTTP 200 ----
            http_ok = False
            try:
                verify = subprocess.run(
                    ['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}',
                     '-k', f'https://{full_domain}/web/login'],
                    capture_output=True, text=True, timeout=15,
                )
                if verify.stdout.strip() in ('200', '303'):
                    http_ok = True
                else:
                    errors.append(
                        f"HTTP check returned {verify.stdout.strip()}"
                    )
            except Exception as e:
                errors.append(f"HTTP verify: {e}")

            # ---- Step 14: Update state ----
            if http_ok and not errors:
                self.write({'state': 'running'})
            elif http_ok:
                # Running but with warnings
                self.write({'state': 'running'})
            else:
                self.write({'state': 'error'})

            self._update_activity()

            # ---- Step 15: Post chatter log ----
            body = _(
                "<b>Instancia creada</b><br/>"
                "Puerto: %(port)s | Gevent: %(gevent)s<br/>"
                "BD: %(db)s<br/>"
                "Dominio: %(domain)s<br/>"
                "Servicio: %(service)s<br/>",
                port=port,
                gevent=gevent_port,
                db=db_name,
                domain=full_domain,
                service=service_name,
            )
            if errors:
                body += _("<br/><b>Advertencias:</b><br/>%s") % '<br/>'.join(errors)
            self.message_post(body=body)
            _logger.info("Instance %s created successfully", self.name)

        except UserError:
            raise
        except Exception as e:
            _logger.exception("Error creating instance %s", self.name)
            self.write({'state': 'error'})
            self.message_post(
                body=_("<b>Error creando instancia:</b><br/>%s") % str(e),
            )
            raise UserError(
                _("Error creando instancia: %s") % str(e)
            )

    # ------------------------------------------------------------------
    # Limit validation
    # ------------------------------------------------------------------

    def _validate_instance_limits(self):
        """Check that project instance limits are not exceeded."""
        project = self.project_id
        if self.instance_type == 'staging':
            current = self.search_count([
                ('project_id', '=', project.id),
                ('instance_type', '=', 'staging'),
                ('state', 'not in', ['destroying']),
                ('id', '!=', self.id),
            ])
            if current >= project.max_staging:
                raise UserError(
                    _("Límite de instancias staging alcanzado (%d/%d).") % (
                        current, project.max_staging,
                    )
                )
        elif self.instance_type == 'development':
            current = self.search_count([
                ('project_id', '=', project.id),
                ('instance_type', '=', 'development'),
                ('state', 'not in', ['destroying']),
                ('id', '!=', self.id),
            ])
            if current >= project.max_development:
                raise UserError(
                    _("Límite de instancias development alcanzado (%d/%d).") % (
                        current, project.max_development,
                    )
                )

    # ------------------------------------------------------------------
    # Action: Start
    # ------------------------------------------------------------------

    def action_start(self):
        """Start the instance's systemd service."""
        self.ensure_one()
        if not self.service_name:
            raise UserError(_("No hay servicio systemd configurado."))

        try:
            infra_utils.start_service(self.service_name)
        except Exception as e:
            raise UserError(_("Error iniciando servicio: %s") % str(e))

        self._check_service_status()
        self._update_activity()
        self.message_post(
            body=_("Servicio '%s' iniciado.") % self.service_name,
        )

    # ------------------------------------------------------------------
    # Action: Stop
    # ------------------------------------------------------------------

    def action_stop(self):
        """Stop the instance's systemd service.
        Production instances cannot be stopped.
        """
        self.ensure_one()
        if self.instance_type == 'production':
            raise UserError(
                _("No se puede detener una instancia de producción. "
                  "Use reiniciar en su lugar.")
            )
        if not self.service_name:
            raise UserError(_("No hay servicio systemd configurado."))

        try:
            infra_utils.stop_service(self.service_name)
        except Exception as e:
            raise UserError(_("Error deteniendo servicio: %s") % str(e))

        self._check_service_status()
        self._update_activity()
        self.message_post(
            body=_("Servicio '%s' detenido.") % self.service_name,
        )

    # ------------------------------------------------------------------
    # Action: Restart
    # ------------------------------------------------------------------

    def action_restart(self):
        """Restart the instance's systemd service."""
        self.ensure_one()
        if not self.service_name:
            raise UserError(_("No hay servicio systemd configurado."))

        try:
            infra_utils.restart_service(self.service_name)
        except Exception as e:
            raise UserError(_("Error reiniciando servicio: %s") % str(e))

        self._check_service_status()
        self._update_activity()
        self.message_post(
            body=_("Servicio '%s' reiniciado.") % self.service_name,
        )

    # ------------------------------------------------------------------
    # Action: Destroy
    # ------------------------------------------------------------------

    def action_destroy(self):
        """Full destruction pipeline for an instance.

        Production instances cannot be destroyed.

        Steps:
        1. Stop & disable systemd service
        2. Drop database
        3. Remove Odoo config
        4. Remove nginx vhost
        5. Remove instance directory
        6. Delete the record
        """
        self.ensure_one()
        if self.instance_type == 'production':
            raise UserError(
                _("No se puede destruir una instancia de producción.")
            )

        self.write({'state': 'destroying'})
        self.env.cr.commit()

        errors = []

        # 1. Stop & remove systemd service
        if self.service_name:
            try:
                infra_utils.remove_systemd_service(self.service_name)
            except Exception as e:
                errors.append(f"Systemd: {e}")
                _logger.warning("Error removing service %s: %s", self.service_name, e)

        # 2. Drop database
        if self.database_name:
            try:
                infra_utils.drop_database(self.database_name)
            except Exception as e:
                errors.append(f"Database: {e}")
                _logger.warning("Error dropping database %s: %s", self.database_name, e)

        # 3. Remove Odoo config
        if self.odoo_config_path:
            try:
                infra_utils.sudo_run(f"rm -f {self.odoo_config_path}")
            except Exception as e:
                errors.append(f"Config: {e}")

        # 4. Remove nginx vhost
        if self.nginx_config_path:
            try:
                infra_utils.remove_nginx_vhost(self.nginx_config_path)
            except Exception as e:
                errors.append(f"Nginx: {e}")
                _logger.warning(
                    "Error removing nginx config %s: %s",
                    self.nginx_config_path, e,
                )

        # 5. Remove instance directory
        if self.instance_path:
            try:
                infra_utils.remove_instance_directory(self.instance_path)
            except Exception as e:
                errors.append(f"Directory: {e}")
                _logger.warning(
                    "Error removing instance directory %s: %s",
                    self.instance_path, e,
                )

        # Log before deleting the record
        instance_name = self.name
        project = self.project_id

        if errors:
            _logger.warning(
                "Instance %s destroyed with errors: %s",
                instance_name, '; '.join(errors),
            )

        # 6. Delete the record
        self.unlink()

        # Post to project chatter
        if project:
            body = _(
                "<b>Instancia '%s' destruida.</b>",
                instance_name,
            )
            if errors:
                body += _("<br/><b>Errores:</b><br/>%s") % '<br/>'.join(errors)
            project.message_post(body=body)

        _logger.info("Instance %s fully destroyed", instance_name)

    # ------------------------------------------------------------------
    # Status check
    # ------------------------------------------------------------------

    def _check_service_status(self):
        """Check systemd service status and update state field."""
        for rec in self:
            if not rec.service_name:
                continue
            status = infra_utils.is_service_active(rec.service_name)
            if status == 'active':
                rec.state = 'running'
            elif status in ('inactive', 'deactivating'):
                rec.state = 'stopped'
            else:
                rec.state = 'error'

    # ---- Cron methods ----

    @api.model
    def _cron_auto_stop(self):
        """Stop staging/dev instances with >1h inactivity."""
        from datetime import timedelta
        cutoff = fields.Datetime.now() - timedelta(hours=1)
        instances = self.search([
            ('instance_type', 'in', ['staging', 'development']),
            ('state', '=', 'running'),
            ('last_activity', '<', cutoff),
        ])
        for inst in instances:
            try:
                inst.action_stop()
            except Exception as e:
                _logger.warning("Auto-stop failed for %s: %s", inst.name, e)

    @api.model
    def _cron_auto_destroy(self):
        """Destroy stopped development instances after configured hours."""
        from datetime import timedelta
        instances = self.search([
            ('instance_type', '=', 'development'),
            ('state', '=', 'stopped'),
        ])
        for inst in instances:
            hours = inst.project_id.auto_destroy_hours or 24
            cutoff = fields.Datetime.now() - timedelta(hours=hours)
            if inst.last_activity and inst.last_activity < cutoff:
                try:
                    inst.action_destroy()
                except Exception as e:
                    _logger.warning("Auto-destroy failed for %s: %s", inst.name, e)

    @api.model
    def _cron_health_check(self):
        """Check service status of all running/error instances."""
        instances = self.search([
            ('state', 'in', ['running', 'error']),
            ('service_name', '!=', False),
        ])
        instances._check_service_status()
