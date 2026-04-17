#!/usr/bin/env bash
# PMB DevOps — module install/upgrade/uninstall runner
#
# Invoked by /etc/systemd/system/pmb-module-op@.service.
# The systemd instance name (passed as $1 via %i) is the operation id,
# which doubles as the basename of the env file we read:
#
#     /run/pmb/module_op/<op_id>.env
#
# Env vars expected (written by devops_controller before starting the unit):
#   PMB_PY, PMB_ODOO_BIN, PMB_CONFIG, PMB_DB, PMB_MODULE,
#   PMB_ACTION (install|upgrade|uninstall), PMB_SERVICE,
#   PMB_LOG (log file path), PMB_DONE (marker file path)
#
# The unit runs as root with its own cgroup, so a `systemctl stop PMB_SERVICE`
# inside this script does NOT kill the script (unlike the old in-worker path).
set -o pipefail
op_id="${1:-$PMB_OP_ID}"
env_file="/run/pmb/module_op/${op_id}.env"
if [ -f "$env_file" ]; then
    set -a; . "$env_file"; set +a
fi

log="${PMB_LOG:-/var/log/pmb/module_op-${op_id}.log}"
done_file="${PMB_DONE:-/run/pmb/module_op/${op_id}.done}"
mkdir -p "$(dirname "$log")" "$(dirname "$done_file")"

echo "=== pmb-module-op ${op_id} $(date -Is) ===" >>"$log"
echo "svc=${PMB_SERVICE} action=${PMB_ACTION} module=${PMB_MODULE} db=${PMB_DB}" >>"$log"

if [ -z "$PMB_PY" ] || [ -z "$PMB_ODOO_BIN" ] || [ -z "$PMB_CONFIG" ] \
    || [ -z "$PMB_DB" ] || [ -z "$PMB_MODULE" ] || [ -z "$PMB_SERVICE" ] \
    || [ -z "$PMB_ACTION" ]; then
    echo "missing required env vars" >>"$log"
    echo "rc=2" >"$done_file"
    exit 2
fi

case "$PMB_ACTION" in
    install)   flag="-i" ;;
    upgrade)   flag="-u" ;;
    uninstall) flag="" ;;
    *) echo "invalid action: $PMB_ACTION" >>"$log"; echo "rc=3" >"$done_file"; exit 3 ;;
esac

systemctl stop "$PMB_SERVICE" >>"$log" 2>&1 || true

# Run odoo-bin as the same user the target service runs under, so DB peer
# authentication and file permissions match. Fallback to odooal, then root.
run_user="${PMB_USER:-}"
if [ -z "$run_user" ]; then
    run_user=$(systemctl show -p User --value "$PMB_SERVICE" 2>/dev/null)
fi
if [ -z "$run_user" ] || [ "$run_user" = "root" ]; then
    run_user="odooal"
fi
echo "run_user=$run_user" >>"$log"

if [ "$PMB_ACTION" = "uninstall" ]; then
    runuser -u "$run_user" -- "$PMB_PY" "$PMB_ODOO_BIN" shell \
        -c "$PMB_CONFIG" -d "$PMB_DB" \
        --no-http --stop-after-init \
        >>"$log" 2>&1 <<PYEOF
env['ir.module.module'].search([('name','=','$PMB_MODULE')]).button_immediate_uninstall()
env.cr.commit()
PYEOF
    rc=$?
else
    runuser -u "$run_user" -- "$PMB_PY" "$PMB_ODOO_BIN" \
        -c "$PMB_CONFIG" -d "$PMB_DB" \
        $flag "$PMB_MODULE" \
        --no-http --stop-after-init \
        >>"$log" 2>&1
    rc=$?
fi

systemctl start "$PMB_SERVICE" >>"$log" 2>&1 || true

echo "rc=$rc" >"$done_file"
echo "=== done rc=$rc $(date -Is) ===" >>"$log"
exit $rc
