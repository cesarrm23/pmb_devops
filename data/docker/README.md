# pmb_devops Docker runtime

This folder holds the on-disk artifacts the hub ships to a remote host
when `devops.project.runtime == 'docker'`. No code in this folder is
executed on the hub itself — these are templates rendered and pushed
over SSH at project/instance create time.

## Files

| File | Purpose |
|---|---|
| `Dockerfile.odoo19` | Base image for Odoo 19 runtimes. Built **once per client host** (or pulled from a registry), shared by every instance on that host. |
| `requirements.odoo19.txt` | Python deps baked into the base image. Keeps code-layer rebuilds trivial. |
| `docker-compose.yml.j2` | Per-instance stack: `odoo` + `code-server` + `postgres`. Rendered to `/opt/pmb-docker/<project>/<instance>/docker-compose.yml` on the client host. |
| `odoo.conf.j2` | Odoo config file rendered next to the compose file, mounted read-only into the `odoo` container. |
| `nginx.location.j2` | Server-block snippet wired into the client's existing `nginx` so the Odoo, longpolling and `/code/<instance>/` URLs are exposed on the project's domain. |

## Why this design

- **Container per instance, not per branch.** Branches stay as `git checkout`
  inside the bind-mounted addons volume — no N-images explosion.
- **One base image per Odoo major version.** Cold `docker compose up`
  runs in ~3–5s because the image is prebuilt and the code is
  bind-mounted, not `COPY`ed.
- **Shared Odoo core, per-instance addons.** `pmb-odoo-core-<ver>` is an
  external named volume populated once from the host's `/opt/odoo<ver>`
  checkout.
- **code-server ships in every stack.** Same addons volume is mounted
  into code-server at `/home/coder/project`, so edits in VS Code Web
  are visible to Odoo immediately. `-u <module>` through
  `docker compose exec odoo odoo-bin -u <mod>` applies the upgrade
  without restarting the container.
- **Per-instance postgres container.** Simpler than sharing a host
  postgres — every teardown wipes cleanly via `docker compose down -v`.

## Provisioning contract (pending orchestrator)

When a `devops.instance` with `project.runtime='docker'` is created, the
orchestrator (not yet wired — this commit only ships the templates) will:

1. Ensure `docker` + `docker-compose-plugin` installed on the client host.
2. Build or pull `pmb/odoo:<version>` on the host (once).
3. Create `pmb-odoo-core-<version>` volume and populate it from the host's
   Odoo checkout.
4. Render `docker-compose.yml`, `odoo.conf`, `nginx.location` into
   `/opt/pmb-docker/<project>/<instance>/` with per-instance ports and
   secrets stored on the `devops.instance` record.
5. `docker compose up -d` the stack.
6. Add the nginx snippet to the project's server block, `nginx -s reload`.

On instance `unlink`:

1. `docker compose down -v` in the instance directory.
2. Remove the compose dir and the nginx snippet.
3. `nginx -s reload`.

Clones (staging from prod, dev from staging) are a standard `pg_dump |
pg_restore` into the new stack's postgres container plus a git checkout
of the chosen branch in the bind-mounted addons path. No systemd, no
ad-hoc rsync.
