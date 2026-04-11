/** @odoo-module **/
import { registry } from "@web/core/registry";
import { browser } from "@web/core/browser/browser";
import { rpc } from "@web/core/network/rpc";

registry.category("user_menuitems").add("pmb_hard_reset", (env) => ({
    type: "item",
    id: "pmb_hard_reset",
    description: env._t("🔄 Hard Reset (limpiar caché)"),
    callback: async () => {
        try {
            await rpc("/devops/assets/clear");
        } catch (e) {
            // endpoint might fail, still reload
        }
        browser.location.reload(true);
    },
    sequence: 25,
}));
