# Base image for pmb_devops instances (version-agnostic).
# Build once per cluster per Odoo version, reuse across every docker-runtime
# instance of every project. The project's custom_addons checkout is
# bind-mounted at runtime via docker-compose — it is NEVER baked into the image.
#
# The deploy script SCPs the correct requirements.odoo<VER>.txt as
# `requirements.txt` before `docker build`, so the same Dockerfile works
# for Odoo 18, 19, etc.
#
#   docker build -t pmb/odoo:<ver> .
#
# The image expects the host to provide, via compose volumes:
#   /mnt/odoo          — Odoo core checkout (shared, read-only)
#   /mnt/addons        — project custom_addons (read-write)
#   /var/lib/odoo      — filestore (per-instance named volume)
#   /etc/odoo/odoo.conf — rendered config file
#
# Kept compatible with the legacy Docker builder (no BuildKit `--mount=`
# directives), because some client hosts still ship a pre-buildx daemon.

FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev libldap2-dev libsasl2-dev libxml2-dev \
        libxslt1-dev libjpeg-dev libtiff5-dev libffi-dev libssl-dev \
        zlib1g-dev libopenjp2-7-dev libfreetype6-dev liblcms2-dev \
        libwebp-dev libharfbuzz-dev libfribidi-dev \
        git curl ca-certificates \
        wkhtmltopdf ffmpeg \
        nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Odoo requirements. The deploy script writes the per-version file as
# requirements.txt inside the build context. Kept in a separate layer so
# code changes do not invalidate the pip cache.
COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

RUN useradd -m -d /home/odoo -s /bin/bash odoo \
    && mkdir -p /var/lib/odoo /var/log/odoo /etc/odoo \
    && chown -R odoo:odoo /var/lib/odoo /var/log/odoo /etc/odoo

USER odoo
WORKDIR /mnt/odoo

EXPOSE 8069 8072

# The compose file supplies the actual command; the default runs Odoo with
# the config file rendered at provision time.
CMD ["python", "/mnt/odoo/odoo-bin", "-c", "/etc/odoo/odoo.conf"]
