/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { Wysiwyg } from "@html_editor/wysiwyg";
import { MAIN_PLUGINS } from "@html_editor/plugin_sets";

/**
 * Thin wrapper around @html_editor's Wysiwyg that gives us a project.task
 * description editor with paste-image support. The record-info hook is what
 * makes image_save_plugin upload pasted <img> to the task's attachments via
 * /html_editor/attachment/add_data, so the saved HTML ends up referencing
 * /web/image/<id> — no data-URIs piling up in the column.
 */
export class DevopsDescEditor extends Component {
    static template = "pmb_devops.DevopsDescEditor";
    static components = { Wysiwyg };
    static props = {
        value: { type: String, optional: true },
        resModel: String,
        resId: Number,
        field: { type: String, optional: true },
        placeholder: { type: String, optional: true },
        onSave: Function,
        onCancel: Function,
    };
    static defaultProps = {
        value: "",
        field: "description",
        placeholder: "",
    };

    setup() {
        this.editor = null;
        this.internal = useState({ busy: false });
        this.config = {
            content: this.props.value || "",
            Plugins: MAIN_PLUGINS,
            placeholder: this.props.placeholder,
            getRecordInfo: () => ({
                resModel: this.props.resModel,
                resId: this.props.resId,
                field: this.props.field,
                type: "html",
            }),
        };
    }

    onWysiwygLoad(editor) {
        this.editor = editor;
    }

    async _save() {
        if (this.internal.busy || !this.editor) return;
        this.internal.busy = true;
        try {
            const html = this.editor.getContent();
            await this.props.onSave(html);
        } finally {
            this.internal.busy = false;
        }
    }

    _cancel() {
        this.props.onCancel();
    }
}
