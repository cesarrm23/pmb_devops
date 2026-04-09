/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { rpc } from "@web/core/network/rpc";

class PmbDevopsApp extends Component {
    static template = "pmb_devops.PmbDevopsApp";
    static props = { action: { type: Object, optional: true }, "*": true };

    setup() {
        this.state = useState({
            // Projects
            projects: [],
            currentProjectId: false,
            currentProject: null,

            // Branches & Instances
            branches: [],
            instances: [],
            selectedBranchId: false,
            selectedBranch: null,
            selectedInstance: null,

            // Navigation
            activeNavTab: "branches", // branches, builds, status, logs, settings
            activeContentTab: "history", // history, ai, shell, logs, backups, upgrade, tools

            // Sidebar
            sidebarFilter: "",

            // UI
            loading: false,
            showCreateDialog: false,
            createDialogType: "", // 'staging' or 'development'
            createName: "",
            createBranchFrom: "main",
            createCloneFrom: "",

            // Terminal
            terminalConnected: false,
        });

        this.terminalAIRef = useRef("terminalAI");
        this.terminalShellRef = useRef("terminalShell");
        this.terminalLogsRef = useRef("terminalLogs");

        onMounted(async () => {
            await this._loadProjects();
            if (this.state.projects.length > 0) {
                this.state.currentProjectId = this.state.projects[0].id;
                this.state.currentProject = this.state.projects[0];
                await this._loadProjectData();
            }
        });
    }

    // ------------------------------------------------------------------
    // Data loading
    // ------------------------------------------------------------------

    async _loadProjects() {
        try {
            const projects = await rpc("/web/dataset/call_kw", {
                model: "devops.project",
                method: "search_read",
                args: [[["active", "=", true]]],
                kwargs: {
                    fields: [
                        "id",
                        "name",
                        "domain",
                        "repo_path",
                        "max_staging",
                        "max_development",
                    ],
                    limit: 100,
                },
            });
            this.state.projects = projects;
        } catch (err) {
            console.error("PmbDevopsApp: error loading projects", err);
        }
    }

    async _loadProjectData() {
        if (!this.state.currentProjectId) {
            return;
        }

        try {
            // Load instances for the current project
            const instances = await rpc("/web/dataset/call_kw", {
                model: "devops.instance",
                method: "search_read",
                args: [[["project_id", "=", this.state.currentProjectId]]],
                kwargs: {
                    fields: [
                        "id",
                        "name",
                        "instance_type",
                        "state",
                        "full_domain",
                        "port",
                        "database_name",
                        "service_name",
                        "url",
                        "branch_id",
                        "subdomain",
                        "last_activity",
                    ],
                    limit: 200,
                },
            });

            // Load branches for the current project
            const branches = await rpc("/web/dataset/call_kw", {
                model: "devops.branch",
                method: "search_read",
                args: [[["project_id", "=", this.state.currentProjectId]]],
                kwargs: {
                    fields: [
                        "id",
                        "name",
                        "branch_type",
                        "is_current",
                        "last_commit_hash",
                        "last_commit_message",
                        "last_commit_author",
                        "commit_history",
                        "instance_id",
                    ],
                    limit: 200,
                },
            });

            this.state.instances = instances;
            this.state.branches = branches;

            // Auto-select first instance: prefer production, then first available
            this.state.selectedInstance = null;
            this.state.selectedBranch = null;

            if (instances.length > 0) {
                const production = instances.find(
                    (i) => i.instance_type === "production"
                );
                this._selectInstance(production || instances[0]);
            }
        } catch (err) {
            console.error("PmbDevopsApp: error loading project data", err);
        }
    }

    // ------------------------------------------------------------------
    // Selection
    // ------------------------------------------------------------------

    _selectInstance(instance) {
        if (!instance) {
            return;
        }
        this.state.selectedInstance = instance;
        this.state.activeContentTab = "history";

        // Find matching branch
        if (instance.branch_id) {
            const branchId =
                Array.isArray(instance.branch_id)
                    ? instance.branch_id[0]
                    : instance.branch_id;
            const branch = this.state.branches.find((b) => b.id === branchId);
            this.state.selectedBranch = branch || null;
        } else {
            this.state.selectedBranch = null;
        }
    }

    _selectBranch(branch) {
        if (!branch) {
            return;
        }
        this.state.selectedBranch = branch;
        this.state.activeContentTab = "history";

        // Find matching instance
        if (branch.instance_id) {
            const instanceId =
                Array.isArray(branch.instance_id)
                    ? branch.instance_id[0]
                    : branch.instance_id;
            const instance = this.state.instances.find(
                (i) => i.id === instanceId
            );
            this.state.selectedInstance = instance || null;
        } else {
            this.state.selectedInstance = null;
        }
    }

    // ------------------------------------------------------------------
    // Project change
    // ------------------------------------------------------------------

    _onProjectChange(ev) {
        const val = parseInt(ev.target.value, 10);
        this.state.currentProjectId = val || false;
        this.state.currentProject =
            this.state.projects.find((p) => p.id === val) || null;
        this.state.selectedInstance = null;
        this.state.selectedBranch = null;
        this._loadProjectData();
    }

    // ------------------------------------------------------------------
    // Filtering
    // ------------------------------------------------------------------

    _getFilteredInstances(type) {
        const filter = (this.state.sidebarFilter || "").toLowerCase();
        return this.state.instances.filter((inst) => {
            if (inst.instance_type !== type) {
                return false;
            }
            // Determine the display name (branch name or instance name)
            const branchId = Array.isArray(inst.branch_id)
                ? inst.branch_id[0]
                : inst.branch_id;
            const branch = branchId
                ? this.state.branches.find((b) => b.id === branchId)
                : null;
            const displayName = branch ? branch.name : inst.name;
            // Attach branch_name for the template
            inst.branch_name = branch ? branch.name : "";
            if (filter && !displayName.toLowerCase().includes(filter)) {
                return false;
            }
            return true;
        });
    }

    _getStatusColor(instance) {
        if (!instance) {
            return "#6c7086";
        }
        switch (instance.state) {
            case "running":
                return "#a6e3a1";
            case "error":
                return "#f38ba8";
            case "creating":
            case "destroying":
                return "#f9e2af";
            case "stopped":
            default:
                return "#6c7086";
        }
    }

    // ------------------------------------------------------------------
    // Navigation
    // ------------------------------------------------------------------

    _onNavTabChange(tab) {
        this.state.activeNavTab = tab;
    }

    _onContentTabChange(tab) {
        this.state.activeContentTab = tab;
    }

    // ------------------------------------------------------------------
    // Create dialog
    // ------------------------------------------------------------------

    _showCreateDialog(type) {
        this.state.showCreateDialog = true;
        this.state.createDialogType = type;
        this.state.createName = "";
        if (type === "staging") {
            this.state.createBranchFrom = "main";
        } else {
            this.state.createBranchFrom = "staging";
        }
    }

    async _createInstance() {
        if (!this.state.createName || !this.state.currentProjectId) {
            return;
        }

        this.state.loading = true;
        this.state.showCreateDialog = false;

        try {
            // Create the instance record
            const ids = await rpc("/web/dataset/call_kw", {
                model: "devops.instance",
                method: "create",
                args: [
                    {
                        name: this.state.createName,
                        instance_type: this.state.createDialogType,
                        project_id: this.state.currentProjectId,
                    },
                ],
                kwargs: {},
            });

            const instanceId = Array.isArray(ids) ? ids[0] : ids;

            // Run the creation pipeline
            await rpc("/web/dataset/call_kw", {
                model: "devops.instance",
                method: "action_create_instance",
                args: [[instanceId]],
                kwargs: {},
            });

            // Reload data
            await this._loadProjectData();
        } catch (err) {
            console.error("PmbDevopsApp: error creating instance", err);
            alert("Error creating instance: " + (err.message || err));
        } finally {
            this.state.loading = false;
        }
    }

    // ------------------------------------------------------------------
    // Destroy
    // ------------------------------------------------------------------

    async _destroyInstance() {
        const inst = this.state.selectedInstance;
        if (!inst) {
            return;
        }

        if (
            !confirm(
                `Are you sure you want to destroy instance "${inst.name}"?\nThis will delete the database, service, and all associated files.`
            )
        ) {
            return;
        }

        this.state.loading = true;

        try {
            await rpc("/web/dataset/call_kw", {
                model: "devops.instance",
                method: "action_destroy",
                args: [[inst.id]],
                kwargs: {},
            });

            this.state.selectedInstance = null;
            this.state.selectedBranch = null;
            await this._loadProjectData();
        } catch (err) {
            console.error("PmbDevopsApp: error destroying instance", err);
            alert("Error destroying instance: " + (err.message || err));
        } finally {
            this.state.loading = false;
        }
    }

    // ------------------------------------------------------------------
    // Actions
    // ------------------------------------------------------------------

    _onAction(action) {
        const inst = this.state.selectedInstance;
        if (!inst) {
            return;
        }

        const project = this.state.currentProject;
        const repoPath = project ? project.repo_path || "" : "";
        const domain = project ? project.domain || "" : "";

        switch (action) {
            case "Clone":
                alert(
                    `git clone ${domain ? "git@" + domain + ":" : ""}${repoPath}`
                );
                break;
            case "Fork":
                alert(
                    `Fork functionality: create new branch from ${inst.branch_name || inst.name}`
                );
                break;
            case "Merge":
                alert(
                    `Merge ${inst.branch_name || inst.name} into production`
                );
                break;
            case "SSH":
                alert(
                    `ssh ${project && project.domain ? project.domain : "server"} -p ${inst.port || 22}`
                );
                break;
            case "SQL":
                alert(`psql -d ${inst.database_name || "odoo"}`);
                break;
            default:
                break;
        }
    }
}

registry.category("actions").add("pmb_devops_main", PmbDevopsApp);
