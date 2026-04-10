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
            sidebarCollapsed: false,  // collapsed on mobile after selecting instance

            // UI
            loading: false,
            showCreateDialog: false,
            createDialogType: "", // 'staging' or 'development'
            createName: "",
            createBranchFrom: "main",
            createCloneFrom: "",

            // Terminal
            terminalConnected: false,

            // Loading
            loadingMessage: '',

            // History
            commits: [],

            // Backups
            backups: [],

            // Logs
            logType: 'service',  // 'service' (journalctl) or 'odoo' (logfile)

            // Editor / file browser
            editorPath: '',
            editorFiles: [],
            editorLoading: false,
            editorSelectedFile: '',
            editorFileContent: '',
            editorFileName: '',
        });

        this.terminalAIRef = useRef("terminalAI");
        this.terminalShellRef = useRef("terminalShell");
        this.terminalLogsRef = useRef("terminalLogs");
        this._pollTimer = null;

        onMounted(async () => {
            await this._loadProjects();
            if (this.state.projects.length > 0) {
                this.state.currentProjectId = this.state.projects[0].id;
                this.state.currentProject = this.state.projects[0];
                await this._loadProjectData();
            }
        });

        onWillUnmount(() => {
            if (this._pollTimer) {
                clearInterval(this._pollTimer);
                this._pollTimer = null;
            }
            this._cleanupTerminal();
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
                        "creation_step",
                        "full_domain",
                        "port",
                        "database_name",
                        "service_name",
                        "url",
                        "branch_id",
                        "subdomain",
                        "last_activity",
                        "git_branch",
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

            // Preserve current selection if it still exists, otherwise auto-select
            const currentId = this.state.selectedInstance ? this.state.selectedInstance.id : null;
            const stillExists = currentId ? instances.find(i => i.id === currentId) : null;

            if (stillExists) {
                // Update the selected instance data without changing selection
                this.state.selectedInstance = stillExists;
            } else if (instances.length > 0) {
                const production = instances.find(
                    (i) => i.instance_type === "production"
                );
                this._selectInstance(production || instances[0]);
            } else {
                this.state.selectedInstance = null;
                this.state.selectedBranch = null;
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
        // Cleanup terminal when switching instances
        if (this._termConnected || this._term) {
            this._cleanupTerminal();
        }
        this.state.selectedInstance = instance;
        this.state.activeContentTab = "history";
        // Collapse sidebar on mobile
        if (window.innerWidth <= 768) {
            this.state.sidebarCollapsed = true;
        }

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

        // Load history for the newly selected instance
        this._loadHistory();
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

    async _onContentTabChange(tab) {
        if (tab === this.state.activeContentTab) return;

        const termTabs = ['ai', 'shell', 'logs'];
        const wasTermTab = termTabs.includes(this.state.activeContentTab);
        const isTermTab = termTabs.includes(tab);

        // When leaving a terminal tab for a NON-terminal tab, just pause polling
        // (don't destroy the session — user may come back)
        if (wasTermTab && !isTermTab) {
            this._pauseTerminalPolling();
        }

        // When switching between different terminal types (shell→ai), destroy old
        if (wasTermTab && isTermTab && tab !== this.state.activeContentTab) {
            this._cleanupTerminal();
        }

        this.state.activeContentTab = tab;

        if (tab === 'history') {
            await this._loadHistory();
        } else if (tab === 'editor') {
            await this._browseDir('');
        } else if (tab === 'backups') {
            await this._loadBackups();
        } else if (isTermTab) {
            const sessionType = tab === 'ai' ? 'claude' : tab === 'shell' ? 'shell' : (this.state.logType === 'odoo' ? 'odoo_log' : 'logs');
            // If same session type is still alive, just resume polling
            if (this._termConnected && this._terminalType === sessionType && this._term) {
                this._resumeTerminalPolling();
                // Re-fit terminal after DOM re-renders
                setTimeout(() => { if (this._fitAddon) this._fitAddon.fit(); }, 100);
            } else {
                // New session needed
                setTimeout(async () => {
                    if (this.state.activeContentTab === tab && !this._termInitializing) {
                        await this._initTerminal(sessionType);
                    }
                }, 200);
            }
        }
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
        const name = this.state.createName.trim();
        if (!name || !this.state.currentProjectId) {
            return;
        }

        this.state.showCreateDialog = false;
        this.state.loading = true;
        this.state.loadingMessage = 'Creando instancia...';

        try {
            const result = await rpc('/devops/instance/create', {
                project_id: this.state.currentProjectId,
                name: name,
                instance_type: this.state.createDialogType,
                branch_from: this.state.createBranchFrom,
            });

            if (result.error) {
                this.state.loading = false;
                alert('Error: ' + result.error);
                return;
            }

            // Instance created, background pipeline started
            const instanceId = result.instance_id;
            this.state.loading = false;
            await this._loadProjectData();  // reload to show the new instance in sidebar

            // Start polling for creation progress
            this._pollCreation(instanceId);

        } catch (e) {
            this.state.loading = false;
            alert('Error: ' + (e.message || e));
        }
    }

    async _pollCreation(instanceId) {
        if (this._pollTimer) {
            clearInterval(this._pollTimer);
        }
        this._pollTimer = setInterval(async () => {
            try {
                const status = await rpc('/devops/instance/poll_status', {
                    instance_id: instanceId,
                });

                // Update the instance in our local state
                const inst = this.state.instances.find(i => i.id === instanceId);
                if (inst) {
                    inst.state = status.state;
                    inst.creation_step = status.creation_step;
                }

                if (status.state === 'running' || status.state === 'error') {
                    clearInterval(this._pollTimer);
                    this._pollTimer = null;
                    await this._loadProjectData();
                }
            } catch (e) {
                // Ignore polling errors silently
            }
        }, 3000);
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

    // ------------------------------------------------------------------
    // History
    // ------------------------------------------------------------------

    async _loadHistory() {
        if (!this.state.selectedInstance || !this.state.currentProjectId) return;
        const branchName = this.state.selectedInstance.git_branch || this.state.selectedInstance.branch_name || 'HEAD';
        try {
            const result = await rpc('/devops/branch/history', {
                project_id: this.state.currentProjectId,
                branch_name: branchName,
                limit: 30,
            });
            this.state.commits = (result.commits || []).map(c => ({
                ...c, _expanded: false, _loading: false, _body: '', _files: [], _stat: '',
            }));
        } catch (e) {
            this.state.commits = [];
        }
    }

    async _toggleFileDiff(commit, file) {
        if (file._expanded) {
            file._expanded = false;
            return;
        }
        file._expanded = true;
        file._loading = true;
        try {
            const result = await rpc('/devops/commit/file_diff', {
                project_id: this.state.currentProjectId,
                commit_hash: commit.full_hash,
                file_path: file.path,
            });
            file._diff = result.diff || 'No diff available';
        } catch (e) {
            file._diff = 'Error loading diff';
        }
        file._loading = false;
    }

    async _toggleCommitDetail(commit) {
        if (commit._expanded) {
            commit._expanded = false;
            return;
        }
        commit._expanded = true;
        commit._loading = true;
        try {
            const result = await rpc('/devops/commit/detail', {
                project_id: this.state.currentProjectId,
                commit_hash: commit.full_hash,
            });
            commit._body = result.body || '';
            commit._files = (result.files || []).map(f => ({
                ...f, _expanded: false, _loading: false, _diff: '',
            }));
            commit._stat = result.stat || '';
        } catch (e) {
            commit._body = 'Error loading detail';
        }
        commit._loading = false;
    }

    // ------------------------------------------------------------------
    // Terminal (xterm.js)
    // ------------------------------------------------------------------

    async _initTerminal(sessionType) {
        // Guard against re-entry
        if (this._termInitializing) return;
        this._termInitializing = true;

        try {
            // Clean previous terminal without touching reactive state
            this._doCleanupTerminal();

            // Load xterm.js from CDN if not loaded
            if (!window.Terminal) {
                await this._loadScript('https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js');
                await this._loadCSS('https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css');
                await this._loadScript('https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js');
            }

            const container = this._getTerminalContainer();
            if (!container) return;

            this._term = new window.Terminal({
                cursorBlink: true,
                fontSize: 14,
                fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
                theme: {
                    background: '#1e1e2e',
                    foreground: '#cdd6f4',
                    cursor: '#f5e0dc',
                    selectionBackground: '#45475a',
                    black: '#45475a', red: '#f38ba8', green: '#a6e3a1',
                    yellow: '#f9e2af', blue: '#89b4fa', magenta: '#cba6f7',
                    cyan: '#94e2d5', white: '#bac2de',
                },
                allowProposedApi: true,
            });

            this._fitAddon = new window.FitAddon.FitAddon();
            this._term.loadAddon(this._fitAddon);
            this._term.open(container);
            this._fitAddon.fit();

            // Start session
            const instanceId = this.state.selectedInstance ? this.state.selectedInstance.id : null;
            const projectId = this.state.currentProjectId;
            const result = await rpc('/devops/terminal/start', {
                session_type: sessionType,
                project_id: projectId,
                instance_id: instanceId,
            });

            if (result && result.error) {
                this._term.writeln('\x1b[31mError: ' + result.error + '\x1b[0m');
                return;
            }

            // Wait for bridge to initialize (write alive=1, create output file)
            await new Promise(r => setTimeout(r, 1000));

            this._termConnected = true;  // non-reactive flag

            // Handle input
            this._term.onData((data) => {
                if (!this._termConnected) return;
                rpc('/devops/terminal/write', {
                    session_type: sessionType,
                    data: data,
                    instance_id: instanceId,
                });
            });

            // Start polling output
            this._terminalType = sessionType;
            this._termIdleCount = 0;
            this._termReadPos = 0;  // track read position
            this._startTerminalPolling();

        } catch (e) {
            if (this._term) {
                this._term.writeln('\x1b[31mError: ' + (e.message || e) + '\x1b[0m');
            }
        } finally {
            this._termInitializing = false;
        }
    }

    _startTerminalPolling() {
        // Adaptive polling: fast when active, slow when idle
        if (this._termPollTimeout) clearTimeout(this._termPollTimeout);
        this._pollTerminalLoop();
    }

    async _pollTerminalLoop() {
        if (!this._termConnected) return;
        try {
            const result = await rpc('/devops/terminal/read', {
                session_type: this._terminalType,
                instance_id: this.state.selectedInstance ? this.state.selectedInstance.id : null,
                pos: this._termReadPos || 0,
            });
            if (result.pos !== undefined) {
                this._termReadPos = result.pos;
            }
            if (result.output && this._term) {
                this._term.write(result.output);
                // Only count as "active" if substantial output (>20 bytes)
                if (result.output.length > 20) {
                    this._termIdleCount = 0;
                } else {
                    this._termIdleCount++;
                }
            } else {
                this._termIdleCount++;
            }
            if (!result.alive) {
                // Give a few chances before declaring dead (bridge may be starting)
                this._termDeadCount = (this._termDeadCount || 0) + 1;
                if (this._termDeadCount > 5) {
                    this._termConnected = false;
                    if (this._term) this._term.writeln('\r\n\x1b[31m[Session ended]\x1b[0m');
                    return;  // stop polling
                }
            } else {
                this._termDeadCount = 0;
            }
        } catch (e) { /* ignore */ }

        // Schedule next poll: 250ms if active, 2000ms if idle (>3 empty reads)
        if (this._termConnected) {
            const delay = this._termIdleCount > 3 ? 2000 : 250;
            this._termPollTimeout = setTimeout(() => this._pollTerminalLoop(), delay);
        }
    }

    _pauseTerminalPolling() {
        if (this._termPollTimeout) {
            clearTimeout(this._termPollTimeout);
            this._termPollTimeout = null;
        }
    }

    _resumeTerminalPolling() {
        if (this._termConnected && !this._termPollTimeout) {
            this._termIdleCount = 0;
            this._pollTerminalLoop();
        }
    }

    _cleanupTerminal() {
        this._doCleanupTerminal();
    }

    _doCleanupTerminal() {
        // Internal cleanup — does NOT touch reactive state to avoid re-render loops
        if (this._termPollInterval) {
            clearInterval(this._termPollInterval);
            this._termPollInterval = null;
        }
        if (this._termPollTimeout) {
            clearTimeout(this._termPollTimeout);
            this._termPollTimeout = null;
        }
        if (this._term) {
            this._term.dispose();
            this._term = null;
        }
        this._fitAddon = null;
        this._termConnected = false;
        // Stop server session
        if (this._terminalType) {
            rpc('/devops/terminal/stop', { session_type: this._terminalType }).catch(() => {});
            this._terminalType = null;
        }
    }

    _getTerminalContainer() {
        // Return the DOM element for the current terminal tab
        const ref = this.state.activeContentTab === 'ai' ? 'terminalAI'
                  : this.state.activeContentTab === 'shell' ? 'terminalShell'
                  : 'terminalLogs';
        return this.__owl__.refs[ref] || null;
    }

    _loadScript(url) {
        return new Promise((resolve, reject) => {
            const script = document.createElement('script');
            script.src = url;
            script.onload = resolve;
            script.onerror = reject;
            document.head.appendChild(script);
        });
    }

    _loadCSS(url) {
        return new Promise((resolve) => {
            const link = document.createElement('link');
            link.rel = 'stylesheet';
            link.href = url;
            link.onload = resolve;
            document.head.appendChild(link);
        });
    }

    // ------------------------------------------------------------------
    // Backups
    // ------------------------------------------------------------------

    async _loadBackups() {
        if (!this.state.selectedInstance) { this.state.backups = []; return; }
        try {
            const result = await rpc('/web/dataset/call_kw', {
                model: 'devops.backup', method: 'search_read',
                args: [[['instance_id', '=', this.state.selectedInstance.id]]],
                kwargs: { fields: ['name', 'state', 'backup_type', 'file_size', 'create_date'], limit: 20, order: 'create_date desc' },
            });
            this.state.backups = result;
        } catch (e) { this.state.backups = []; }
    }

    async _createBackup() {
        if (!this.state.selectedInstance) return;
        try {
            await rpc('/web/dataset/call_kw', {
                model: 'devops.backup', method: 'action_create_backup',
                args: [this.state.selectedInstance.id], kwargs: {},
            });
            await this._loadBackups();
        } catch (e) {
            alert('Error creating backup: ' + e.message);
        }
    }

    // ------------------------------------------------------------------
    // Instance management (Upgrade tab)
    // ------------------------------------------------------------------

    // ------------------------------------------------------------------
    // Editor / File browser
    // ------------------------------------------------------------------

    async _browseDir(path) {
        this.state.editorPath = path;
        this.state.editorLoading = true;
        this.state.editorFileContent = '';
        this.state.editorSelectedFile = '';
        this.state.editorFileName = '';
        try {
            const result = await rpc('/devops/files/list', {
                project_id: this.state.currentProjectId,
                instance_id: this.state.selectedInstance ? this.state.selectedInstance.id : null,
                path: path,
            });
            if (result.error) {
                this.state.editorFiles = [];
                alert(result.error);
            } else {
                this.state.editorFiles = result.items || [];
            }
        } catch (e) {
            this.state.editorFiles = [];
        }
        this.state.editorLoading = false;
    }

    async _openFile(item) {
        this.state.editorSelectedFile = item.path;
        this.state.editorFileName = item.name;
        this.state.editorFileContent = '';
        try {
            const result = await rpc('/devops/files/read', {
                project_id: this.state.currentProjectId,
                instance_id: this.state.selectedInstance ? this.state.selectedInstance.id : null,
                path: item.path,
            });
            if (result.error) {
                this.state.editorFileContent = '// Error: ' + result.error;
            } else {
                this.state.editorFileContent = result.content || '';
            }
        } catch (e) {
            this.state.editorFileContent = '// Error loading file';
        }
    }

    // ------------------------------------------------------------------
    // Log type switching
    // ------------------------------------------------------------------

    async _switchLogType(type) {
        if (type === this.state.logType) return;
        this.state.logType = type;
        // Destroy current terminal and start new one with the right session type
        this._doCleanupTerminal();
        await new Promise(r => setTimeout(r, 200));
        const sessionType = type === 'odoo' ? 'odoo_log' : 'logs';
        await this._initTerminal(sessionType);
    }

    async _startInstance() {
        if (!this.state.selectedInstance) return;
        await rpc('/devops/instance/start', { instance_id: this.state.selectedInstance.id });
        await this._loadProjectData();
    }

    async _stopInstance() {
        if (!this.state.selectedInstance) return;
        if (!confirm('Stop this instance?')) return;
        await rpc('/devops/instance/stop', { instance_id: this.state.selectedInstance.id });
        await this._loadProjectData();
    }

    async _restartInstance() {
        if (!this.state.selectedInstance) return;
        await rpc('/devops/instance/restart', { instance_id: this.state.selectedInstance.id });
        await this._loadProjectData();
    }
}

registry.category("actions").add("pmb_devops_main", PmbDevopsApp);
