"""Infrastructure pipeline implementation for devops.instance.

Extends devops.instance with actual create/start/stop/restart/destroy
logic using system commands through infra_utils.
"""
import logging
import subprocess
import threading
import time

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..utils import infra_utils

_logger = logging.getLogger(__name__)


class DevopsInstanceInfra(models.Model):
    _inherit = 'devops.instance'

    # ------------------------------------------------------------------
    # Action: Create Instance (background pipeline)
    # ------------------------------------------------------------------

    def action_create_instance(self):
        """Start instance creation in background.

        Validates limits, assigns port/names, writes initial infra fields,
        then spawns a background thread for the heavy work (DB clone, etc.)
        and returns immediately so the HTTP request is not blocked.
        """
        self.ensure_one()
        project = self.project_id

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
            'creation_step': 'Iniciando...',
        })
        self.env.cr.commit()  # persist so SPA can see the record immediately

        # Spawn background thread
        instance_id = self.id
        dbname = self.env.cr.dbname
        thread = threading.Thread(
            target=self._run_creation_pipeline_thread,
            args=(instance_id, dbname),
            daemon=True,
        )
        thread.start()
        # Return immediately -- SPA will poll status

    @api.model
    def _run_creation_pipeline_thread(self, instance_id, dbname):
        """Run creation pipeline in background thread with its own cursor."""
        import odoo
        with odoo.registry(dbname).cursor() as cr:
            env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
            instance = env['devops.instance'].browse(instance_id)
            try:
                instance._run_creation_pipeline()
            except Exception as e:
                _logger.error(
                    "Background creation failed for %s: %s", instance_id, e,
                )
                try:
                    instance.write({
                        'state': 'error',
                        'creation_step': f'Error: {str(e)[:200]}',
                    })
                    cr.commit()
                except Exception:
                    _logger.exception(
                        "Failed to write error state for instance %s",
                        instance_id,
                    )

    def _update_step(self, step_text):
        """Update creation_step using commit so it's visible to the SPA immediately."""
        self.env.cr.commit()  # commit current work
        self.write({'creation_step': step_text})
        self.env.cr.commit()  # commit the status update

    def _run_creation_pipeline(self):
        """Execute all creation steps sequentially.

        This runs inside a background thread with its own cursor.
        Each step commits progress so the SPA can poll and display it.
        """
        self.ensure_one()
        project = self.project_id
        errors = []

        try:
            # ---- Step 1: Create git branch if needed ----
            if self.branch_id and project.repo_path:
                self._update_step('Configurando rama git...')
                try:
                    from ..utils import git_utils
                    git_utils.git_fetch(project)
                    _logger.info(
                        "Branch %s assigned to instance %s",
                        self.branch_id.name, self.name,
                    )
                except Exception as e:
                    _logger.warning("Git branch setup skipped: %s", e)

            # ---- Step 2: Clone database ----
            source_db = None
            if self.cloned_from_id and self.cloned_from_id.database_name:
                source_db = self.cloned_from_id.database_name
            elif project.database_name:
                source_db = project.database_name

            if source_db:
                self._update_step(
                    f'Clonando base de datos ({source_db})...'
                )
                _logger.info(
                    "Cloning database %s -> %s",
                    source_db, self.database_name,
                )
                infra_utils.clone_database(
                    source_db, self.database_name, timeout=1800,
                )
            else:
                self._update_step('Creando base de datos vacía...')
                _logger.warning(
                    "No source database for cloning; "
                    "instance %s will start with empty database", self.name,
                )

            # ---- Step 3: Create instance directory + symlink ----
            self._update_step('Creando directorio de instancia...')
            infra_utils.create_instance_directory(self.instance_path)
            if project.repo_path:
                infra_utils.sudo_run(
                    f"ln -sfn {project.repo_path} {self.instance_path}/addons"
                )

            # ---- Step 4: Generate Odoo config ----
            self._update_step('Generando configuración Odoo...')
            addons_path = f"{self.instance_path}/addons"
            infra_utils.create_odoo_config(
                service_name=self.service_name,
                db_name=self.database_name,
                port=self.port,
                gevent_port=self.gevent_port,
                instance_path=self.instance_path,
                addons_path=addons_path,
            )

            # ---- Step 5: Create systemd service ----
            self._update_step('Creando servicio systemd...')
            infra_utils.create_systemd_service(
                service_name=self.service_name,
                config_path=self.odoo_config_path,
                instance_path=self.instance_path,
            )

            # ---- Step 6: Create nginx vhost ----
            self._update_step('Configurando Nginx...')
            infra_utils.create_nginx_vhost(
                domain=self.full_domain,
                port=self.port,
                gevent_port=self.gevent_port,
                instance_id=self.id,
                instance_name=self.service_name,
            )

            # ---- Step 7: Reload nginx ----
            infra_utils.reload_nginx()

            # ---- Step 8: Start service ----
            self._update_step('Iniciando servicio Odoo...')
            infra_utils.start_service(self.service_name, timeout=30)
            time.sleep(5)

            # ---- Step 9: SSL certificate ----
            self._update_step('Obteniendo certificado SSL...')
            try:
                infra_utils.obtain_ssl_cert(self.full_domain)
            except Exception as e:
                errors.append(f"SSL certificate: {e}")
                _logger.warning(
                    "SSL cert failed for %s: %s", self.full_domain, e,
                )

            # ---- Step 10: Verify HTTP ----
            self._update_step('Verificando...')
            http_ok = False
            try:
                verify = subprocess.run(
                    [
                        'curl', '-s', '-o', '/dev/null', '-w',
                        '%{http_code}', '-k',
                        f'https://{self.full_domain}/web/login',
                        '--max-time', '15',
                    ],
                    capture_output=True, text=True, timeout=20,
                )
                if verify.stdout.strip() in ('200', '303', '302'):
                    http_ok = True
                else:
                    errors.append(
                        f"HTTP check returned {verify.stdout.strip()}"
                    )
            except Exception as e:
                errors.append(f"HTTP verify: {e}")

            # ---- Step 11: Update state ----
            if http_ok:
                self.write({'state': 'running', 'creation_step': ''})
            else:
                self.write({
                    'state': 'error',
                    'creation_step': (
                        f'HTTP {verify.stdout.strip()}'
                        if not errors
                        else errors[-1]
                    ),
                })

            self._update_activity()

            # ---- Step 12: Post chatter log ----
            body = _(
                "<b>Instancia creada</b><br/>"
                "Puerto: %(port)s | Gevent: %(gevent)s<br/>"
                "BD: %(db)s<br/>"
                "Dominio: %(domain)s<br/>"
                "Servicio: %(service)s<br/>",
                port=self.port,
                gevent=self.gevent_port,
                db=self.database_name,
                domain=self.full_domain,
                service=self.service_name,
            )
            if errors:
                body += _(
                    "<br/><b>Advertencias:</b><br/>%s"
                ) % '<br/>'.join(errors)
            self.message_post(body=body)
            self.env.cr.commit()
            _logger.info("Instance %s created successfully", self.name)

        except Exception as e:
            _logger.exception("Creation pipeline failed for %s", self.name)
            self.write({
                'state': 'error',
                'creation_step': f'Error: {str(e)[:200]}',
            })
            self.message_post(
                body=_("<b>Error creando instancia:</b><br/>%s") % str(e),
            )
            self.env.cr.commit()

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
