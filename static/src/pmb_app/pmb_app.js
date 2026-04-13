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
            sidebarMinimized: false,  // icons-only mode (desktop)

            // UI
            loading: false,
            showCreateDialog: false,
            createDialogType: "", // 'staging' or 'development'
            createName: "",
            createBranchFrom: "main",
            createCloneFrom: "",
            isAdmin: false,

            // Terminal
            terminalConnected: false,

            // Loading
            loadingMessage: '',

            // History
            commits: [],
            historySearch: '',
            historyRepos: [],
            historyRepoPath: '',

            // All builds (for Builds nav tab)
            allBuilds: [],

            // Backups
            backups: [],

            // Logs
            logType: 'service',  // 'service' (journalctl) or 'odoo' (logfile)

            // Git changes (AI tab sidebar)
            gitStaged: [],
            gitUnstaged: [],
            gitUntracked: [],
            gitOutgoing: [],
            gitPanelCollapsed: window.innerWidth <= 768, // collapsed by default on mobile
            gitSelectedRepo: '',        // selected repo path for git panel
            gitCommitMessage: '',       // commit message input
            gitCommitting: false,       // commit in progress
            gitPushing: false,          // push in progress
            gitDiffFile: '',            // file currently showing diff
            gitDiffContent: '',         // diff content
            gitDiffStaged: false,       // is the diff for a staged file
            dashboard: null,             // reports dashboard data
            diagnoseResult: null,       // diagnostics output
            fixResult: null,            // fix result
            meetings: [],               // meetings list
            meetCreating: false,
            meetNewName: '',
            meetNewUrl: '',
            meetTranscriptionId: null,
            meetTranscription: '',
            meetActiveId: null,         // active Jitsi call meeting ID
            meetActiveName: '',
            meetType: 'jitsi',          // create form: jitsi or external
            meetRecordingId: null,      // meeting currently recording
            meetRecordingTime: '',      // recording duration display
            meetAnalyzedId: null,       // meeting with analyzed tasks pending
            meetAnalyzedTasks: [],      // tasks extracted by AI
            meetTasksId: null,          // meeting showing created tasks
            meetTasks: [],              // created Odoo tasks
            groqApiKey: '',
            projectMembers: [],
            availableUsers: [],
            memberNewLogin: '',
            memberNewRole: 'developer',
            memberError: '',
            autodetectService: '',
            autodetectResult: null,
            gitPanelWidth: 280,         // resizable panel width (px)
            gitResizing: false,         // drag in progress
            claudeSessions: [],         // list of claude sessions
            claudeSessionSearch: '',    // search filter
            claudeSessionsVisible: false, // toggle sessions panel
            gitAuthenticated: false,    // git auth confirmed this session
            gitAuthIsAdmin: false,      // current user is admin (no auth needed)
            gitAuthLogin: '',
            gitAuthPassword: '',
            gitAuthError: '',
            gitAuthLoading: false,

            // Editor / file browser
            editorRepo: 'addons',  // 'addons', 'odoo', 'enterprise'
            editorPath: '',
            editorFiles: [],
            editorLoading: false,
            editorSelectedFile: '',
            editorFileContent: '',
            editorFileName: '',
            ctxMenu: null,

            // AI terminal (WebSocket)
            aiConnected: false,
            aiError: '',

            // Creation log
            creationLog: '',
            creationPid: 0,

            // Production setup
            prodSetup: { service: '', db: '', port: 8069, path: '' },

            // Deploy / Upgrade
            deploying: false,
            deployLog: '',
            deployResult: null,
            upgradeRepos: [],

            // Server metrics
            serverMetrics: null,
            metricsUpdated: '',

            // Settings
            settingsProject: null,
            sshPublicKey: '',
            sshTestResult: null,
            settingsSaved: false,
        });

        this.terminalAIRef = useRef("terminalAI");
        this.jitsiContainerRef = useRef("jitsiContainer");
        this.terminalShellRef = useRef("terminalShell");
        this.terminalLogsRef = useRef("terminalLogs");
        this._pollTimer = null;
        this._termPollTimeout = null;

        onMounted(async () => {
            // Load persisted UI preferences
            try {
                const prefs = await rpc('/devops/user/prefs');
                if (prefs.git_panel_width) this.state.gitPanelWidth = prefs.git_panel_width;
                if (prefs.sidebar_minimized) this.state.sidebarMinimized = true;
                if (prefs.git_collapsed) this.state.gitPanelCollapsed = true;
            } catch (e) { /* ignore */ }
            // Check admin status early
            try {
                const authCheck = await rpc('/devops/git/auth/check');
                this.state.isAdmin = authCheck.is_admin || false;
            } catch (e) {}
            await this._loadProjects();
            if (this.state.projects.length > 0) {
                this.state.currentProjectId = this.state.projects[0].id;
                this.state.currentProject = this.state.projects[0];
                await this._loadProjectData();
            }
            // Mobile keyboard: re-fit terminals and collapse header
            if (window.visualViewport) {
                this._onViewportResize = () => {
                    this._refitTerminals();
                    // Detect keyboard open: viewport height much smaller than window height
                    const keyboardOpen = window.visualViewport.height < window.innerHeight * 0.75;
                    document.body.classList.toggle('pmb-keyboard-open', keyboardOpen);
                };
                window.visualViewport.addEventListener('resize', this._onViewportResize);
            }
        });

        onWillUnmount(() => {
            if (this._pollTimer) {
                clearInterval(this._pollTimer);
                this._pollTimer = null;
            }
            this._cleanupTerminal();
            this._cleanupAiTerminal();
            if (this._onViewportResize && window.visualViewport) {
                window.visualViewport.removeEventListener('resize', this._onViewportResize);
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
                        "repo_url",
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
        // Cleanup terminals when switching instances
        if (this._termConnected || this._term) {
            this._cleanupTerminal();
        }
        this._cleanupAiTerminal();
        // Stop any existing creation polling
        if (this._pollTimer) {
            clearInterval(this._pollTimer);
            this._pollTimer = null;
        }
        this.state.creationLog = '';
        this.state.creationPid = 0;
        this.state.claudeSessions = [];
        this.state.selectedInstance = instance;

        // If instance is creating/error, go to DEPLOY tab
        if (instance.state === 'creating' || instance.state === 'error') {
            this.state.activeContentTab = "deploy";
            if (instance.state === 'creating') {
                this._pollCreation(instance.id);
            } else {
                // Load log for error state (one-time, no polling)
                this._loadCreationLog(instance.id);
            }
        } else {
            this.state.activeContentTab = "history";
        }
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

        // Load repos and history for the newly selected instance
        this._loadHistoryRepos().then(() => this._loadHistory());
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

    _onProjectSelectorChange(ev) {
        this._onProjectChange(ev);
    }

    _onAutodetectInput(ev) { this.state.autodetectService = ev.target.value; }

    async _autodetectProject() {
        if (!this.state.autodetectService) return;
        this.state.autodetectResult = null;
        try {
            const result = await rpc('/devops/project/autodetect', {
                service_name: this.state.autodetectService,
            });
            this.state.autodetectResult = result;
            // Auto-fill settings form
            if (!result.error && this.state.settingsProject) {
                const p = this.state.settingsProject;
                if (result.database_name) p.database_name = result.database_name;
                if (result.domain) p.domain = result.domain;
                if (result.repo_path) p.repo_path = result.repo_path;
                if (result.enterprise_path) p.enterprise_path = result.enterprise_path;
                if (result.instance_path && !p.name) p.name = result.database_name || this.state.autodetectService;
                p.odoo_service_name = this.state.autodetectService;
            }
        } catch (e) {
            this.state.autodetectResult = { error: e.message };
        }
    }

    async _newProject() {
        // Switch to settings tab with empty project form
        this.state.settingsProject = {
            id: null, name: '', domain: '', repo_path: '', enterprise_path: '',
            database_name: '', connection_type: 'local', ssh_host: '', ssh_user: '',
            ssh_port: 22, max_staging: 3, max_development: 5, auto_destroy_hours: 24,
            production_branch: 'main',
        };
        this.state.settingsSaved = false;
        await this._onNavTabChange('settings');
    }

    async _onProjectChange(ev) {
        const val = parseInt(ev.target.value, 10);
        this.state.currentProjectId = val || false;
        this.state.currentProject =
            this.state.projects.find((p) => p.id === val) || null;
        this.state.selectedInstance = null;
        this.state.selectedBranch = null;
        this.state.claudeSessions = [];
        this.state.claudeSessionsVisible = false;
        this.state.settingsProject = null;
        this.state.dashboard = null;
        await this._loadProjectData();
        // Reload current nav tab data
        const tab = this.state.activeNavTab;
        if (tab === 'settings') {
            await this._loadSettings();
            await this._loadMembers();
            await this._loadAvailableUsers();
        } else if (tab === 'status') {
            await this._loadMetrics();
            await this._loadDashboard();
        } else if (tab === 'builds') {
            await this._loadAllBuilds();
        } else if (tab !== 'branches') {
            // Go to branches for other tabs
            this.state.activeNavTab = 'branches';
        }
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

    async _onNavTabChange(tab) {
        // Toggle sidebar on mobile when clicking Branches while already on branches
        if (tab === 'branches' && this.state.activeNavTab === 'branches' && window.innerWidth <= 768) {
            this.state.sidebarCollapsed = !this.state.sidebarCollapsed;
            return;
        }
        this.state.activeNavTab = tab;
        // Show sidebar when switching to branches on mobile
        if (tab === 'branches' && window.innerWidth <= 768) {
            this.state.sidebarCollapsed = false;
        }
        // Re-init terminal when returning to branches with AI tab active
        if (tab === 'branches' && this.state.activeContentTab === 'ai') {
            const canTerminal = this.state.isAdmin ||
                (this.state.selectedInstance && this.state.selectedInstance.instance_type !== 'production');
            if (canTerminal && this.state.selectedInstance && this.state.selectedInstance.state === 'running') {
                setTimeout(() => this._initAiTerminal(), 300);
            }
        }
        if (tab === 'branches' && this.state.activeContentTab === 'shell') {
            setTimeout(() => {
                if (this._term) {
                    const container = this._getTerminalContainer();
                    if (container && !container.querySelector('.xterm')) {
                        this._term.open(container);
                        if (this._fitAddon) this._fitAddon.fit();
                    }
                }
            }, 300);
        }
        if (tab === 'settings') {
            await this._loadSettings();
        } else if (tab === 'status') {
            await this._loadMetrics();
            await this._loadDashboard();
        } else if (tab === 'builds') {
            await this._loadAllBuilds();
        }
    }

    async _loadAllBuilds() {
        if (!this.state.currentProjectId) return;
        try {
            const result = await rpc('/web/dataset/call_kw', {
                model: 'devops.build', method: 'search_read',
                args: [[['project_id', '=', this.state.currentProjectId]]],
                kwargs: { fields: ['name', 'state', 'branch_id', 'build_type', 'commit_hash', 'triggered_by', 'duration', 'create_date'], limit: 50, order: 'create_date desc' },
            });
            this.state.allBuilds = result;
        } catch (e) { this.state.allBuilds = []; }
    }

    async _onContentTabChange(tab) {
        if (tab === this.state.activeContentTab) return;

        const prevTab = this.state.activeContentTab;
        const httpTermTabs = ['shell', 'logs'];

        // Pause shell/logs polling when leaving those tabs (keep session alive)
        if (httpTermTabs.includes(prevTab)) {
            if (this._termPollTimeout) {
                clearTimeout(this._termPollTimeout);
                this._termPollTimeout = null;
            }
        }

        // When switching between shell↔logs, destroy old HTTP terminal
        if (httpTermTabs.includes(prevTab) && httpTermTabs.includes(tab) && prevTab !== tab) {
            this._cleanupTerminal();
        }

        this.state.activeContentTab = tab;

        if (tab === 'history') {
            await this._loadHistoryRepos();
            await this._loadHistory();
        } else if (tab === 'ai') {
            await this._checkGitAuth();
            await this._refreshGitStatus();
            this._loadClaudeSessions();
            // Only start terminal if instance is running AND user has write access
            const canTerminal = this.state.isAdmin ||
                (this.state.selectedInstance && this.state.selectedInstance.instance_type !== 'production');
            if (canTerminal && this.state.selectedInstance && this.state.selectedInstance.state === 'running') {
                setTimeout(() => this._initAiTerminal(), 200);
            }
        } else if (tab === 'editor') {
            await this._browseDir('');
        } else if (tab === 'upgrade') {
            await this._loadUpgradeRepos();
        } else if (tab === 'backups') {
            await this._loadBackups();
        } else if (tab === 'meet') {
            await this._loadMeetings();
        } else if (httpTermTabs.includes(tab)) {
            const sessionType = tab === 'shell' ? 'shell' : (this.state.logType === 'odoo' ? 'odoo_log' : 'logs');
            if (this._termConnected && this._terminalType === sessionType && this._term) {
                if (!this._termPollTimeout) this._pollTerminal();
                setTimeout(() => { if (this._fitAddon) this._fitAddon.fit(); }, 100);
            } else {
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

    async _loadCreationLog(instanceId) {
        this.state.creationLog = '';
        try {
            const status = await rpc('/devops/instance/poll_status', {
                instance_id: instanceId, log_pos: 0,
            });
            this.state.creationLog = status.log || '';
            this.state.creationPid = status.creation_pid || 0;
            setTimeout(() => {
                const el = this.__owl__.refs.creationLog;
                if (el) el.parentElement.scrollTop = el.parentElement.scrollHeight;
            }, 100);
        } catch (e) {}
    }

    async _pollCreation(instanceId) {
        if (this._pollTimer) {
            clearInterval(this._pollTimer);
        }
        this._creationLogPos = 0;
        this.state.creationLog = '';
        this.state.creationPid = 0;
        this._pollTimer = setInterval(async () => {
            try {
                const status = await rpc('/devops/instance/poll_status', {
                    instance_id: instanceId,
                    log_pos: this._creationLogPos || 0,
                });

                // Update the instance in our local state
                const inst = this.state.instances.find(i => i.id === instanceId);
                if (inst) {
                    inst.state = status.state;
                    inst.creation_step = status.creation_step;
                }

                // Update creation log
                if (status.log) {
                    this.state.creationLog += status.log;
                    // Auto-scroll log
                    setTimeout(() => {
                        const el = this.__owl__.refs.creationLog;
                        if (el) el.parentElement.scrollTop = el.parentElement.scrollHeight;
                    }, 50);
                }
                if (status.log_pos) {
                    this._creationLogPos = status.log_pos;
                }
                if (status.creation_pid) {
                    this.state.creationPid = status.creation_pid;
                }

                if (status.state === 'running' || status.state === 'error') {
                    clearInterval(this._pollTimer);
                    this._pollTimer = null;
                    await this._loadProjectData();
                    // Switch to HISTORY when done
                    if (this.state.activeContentTab === 'deploy') {
                        this.state.activeContentTab = 'history';
                    }
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
        if (!inst) return;
        const project = this.state.currentProject;

        switch (action) {
            case 'Clone': {
                const url = project ? project.repo_url || project.repo_path : '';
                alert(`git clone ${url}`);
                break;
            }
            case 'SSH': {
                const host = project ? project.domain : 'server';
                alert(`ssh odooal@${host}`);
                break;
            }
            case 'SQL': {
                alert(`psql -h localhost -U odooal ${inst.database_name || ''}`);
                break;
            }
            case 'Fork': {
                this._showCreateDialog(inst.instance_type === 'production' ? 'staging' : 'development');
                break;
            }
            case 'Merge': {
                if (confirm(`Merge ${inst.git_branch || inst.name} into production?`)) {
                    this._mergeBranch();
                }
                break;
            }
        }
    }

    async _mergeBranch() {
        if (!this.state.selectedInstance || !this.state.currentProjectId) return;
        const source = this.state.selectedInstance.git_branch || this.state.selectedInstance.name;
        try {
            const result = await rpc('/devops/branch/merge', {
                project_id: this.state.currentProjectId,
                source_branch: source,
                target_branch: 'main',
            });
            if (result.error) {
                alert('Merge error: ' + result.error);
            } else {
                alert('Merge successful');
                await this._loadProjectData();
            }
        } catch (e) {
            alert('Error: ' + e.message);
        }
    }

    // ------------------------------------------------------------------
    // History
    // ------------------------------------------------------------------

    async _loadHistoryRepos() {
        if (!this.state.selectedInstance || !this.state.currentProjectId) return;
        try {
            const result = await rpc('/devops/instance/repos', {
                project_id: this.state.currentProjectId,
                instance_id: this.state.selectedInstance.id,
            });
            this.state.historyRepos = result.repos || [];
            this.state.isAdmin = result.is_admin || false;
            // Select first repo if current selection is invalid
            if (this.state.historyRepos.length > 0) {
                const paths = this.state.historyRepos.map(r => r.path);
                if (!this.state.historyRepoPath || !paths.includes(this.state.historyRepoPath)) {
                    this.state.historyRepoPath = this.state.historyRepos[0].path;
                }
            }
        } catch (e) {
            this.state.historyRepos = [];
        }
    }

    _isSelectedRepoShallow() {
        const repo = this.state.historyRepos.find(r => r.path === this.state.historyRepoPath);
        return repo && repo.shallow;
    }

    async _fetchFullHistory() {
        if (!this.state.historyRepoPath) return;
        try {
            const result = await rpc('/devops/repo/fetch_deeper', {
                repo_path: this.state.historyRepoPath,
                count: 50,
            });
            if (result.error) {
                alert('Error: ' + result.error);
            } else {
                // Update shallow flag on current repo
                const repo = this.state.historyRepos.find(r => r.path === this.state.historyRepoPath);
                if (repo) repo.shallow = result.still_shallow;
                await this._loadHistory();
            }
        } catch (e) {
            alert('Error: ' + (e.message || e));
        }
    }

    _switchHistoryRepo(ev) {
        const btn = ev.target.closest('button[data-path]');
        const path = btn ? btn.dataset.path : '';
        if (path && path !== this.state.historyRepoPath) {
            this.state.historyRepoPath = path;
            this._loadHistory();
        }
    }

    async _loadHistory(search = '', offset = 0) {
        if (!this.state.selectedInstance || !this.state.currentProjectId) return;
        // Use the selected repo's branch, not the instance's git_branch
        const selectedRepo = this.state.historyRepos.find(r => r.path === this.state.historyRepoPath);
        const branchName = (selectedRepo && selectedRepo.branch) || 'HEAD';
        try {
            const result = await rpc('/devops/branch/history', {
                project_id: this.state.currentProjectId,
                branch_name: branchName,
                limit: 30,
                search: search,
                offset: offset,
                repo_path: this.state.historyRepoPath || '',
            });
            const newCommits = (result.commits || []).map(c => ({
                ...c, _expanded: false, _loading: false, _body: '', _files: [], _stat: '',
            }));
            if (offset > 0 && newCommits.length > 0) {
                this.state.commits = this.state.commits.concat(newCommits);
            } else {
                this.state.commits = newCommits;
            }
            this._hasMoreCommits = newCommits.length >= 30;
        } catch (e) {
            if (offset === 0) this.state.commits = [];
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
                repo_path: this.state.historyRepoPath || '',
            });
            file._diff = result.diff || 'No diff available';
        } catch (e) {
            file._diff = 'Error loading diff';
        }
        file._loading = false;
    }

    _onHistorySearchInput(ev) {
        this.state.historySearch = ev.target.value;
    }

    _onHistorySearchKey(ev) {
        if (ev.key === 'Enter') {
            this._loadHistory(this.state.historySearch);
        }
    }

    _clearHistorySearch() {
        this.state.historySearch = '';
        this._loadHistory();
    }

    async _loadMoreHistory() {
        const repo = this.state.historyRepos.find(r => r.path === this.state.historyRepoPath);
        if (repo && repo.shallow) {
            await this._fetchFullHistory();
            return;
        }
        await this._loadHistory('', this.state.commits.length);
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
                repo_path: this.state.historyRepoPath || '',
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
            this._terminalType = sessionType;

            // Handle input — write then immediately read for fast echo
            this._term.onData(async (data) => {
                if (!this._termConnected) return;
                await rpc('/devops/terminal/write', {
                    session_type: sessionType,
                    data: data,
                    instance_id: instanceId,
                });
                // Immediate read after write for instant echo
                this._termHasInput = true;
                if (this._termPollTimeout) {
                    clearTimeout(this._termPollTimeout);
                    this._termPollTimeout = null;
                }
                this._pollTerminal();
            });

            // Start output polling (lightweight, adaptive)
            this._termReadPos = 0;
            this._termHasInput = false;
            this._terminalType = sessionType;
            this._termInstanceId = instanceId;
            this._pollTerminal();

        } catch (e) {
            if (this._term) {
                this._term.writeln('\x1b[31mError: ' + (e.message || e) + '\x1b[0m');
            }
        } finally {
            this._termInitializing = false;
        }
    }

    async _pollTerminal() {
        if (!this._termConnected) return;
        try {
            const result = await rpc('/devops/terminal/read', {
                session_type: this._terminalType,
                instance_id: this._termInstanceId,
                pos: this._termReadPos || 0,
            });
            if (result.pos !== undefined) {
                this._termReadPos = result.pos;
            }
            if (result.output && this._term) {
                this._term.write(result.output);
            }
            if (!result.alive) {
                this._termDeadCount = (this._termDeadCount || 0) + 1;
                if (this._termDeadCount > 3) {
                    this._termConnected = false;
                    if (this._term) this._term.writeln('\r\n\x1b[31m[Session ended]\x1b[0m');
                    return;
                }
            } else {
                this._termDeadCount = 0;
            }
        } catch (e) { /* ignore */ }

        if (this._termConnected) {
            // Fast (200ms) right after user input, slow (1.5s) when idle
            const delay = this._termHasInput ? 200 : 1500;
            this._termHasInput = false;
            this._termPollTimeout = setTimeout(() => this._pollTerminal(), delay);
        }
    }

    _cleanupTerminal() {
        this._doCleanupTerminal();
    }

    _doCleanupTerminal() {
        // Internal cleanup — does NOT touch reactive state to avoid re-render loops
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

    _refitTerminals() {
        // Called when mobile keyboard opens/closes — refit all active terminals
        try { if (this._fitAddon) this._fitAddon.fit(); } catch (e) {}
        try { if (this._aiFitAddon) this._aiFitAddon.fit(); } catch (e) {}
        // Also send resize to AI WebSocket
        if (this._aiWs && this._aiWs.readyState === WebSocket.OPEN && this._aiFitAddon) {
            const dims = this._aiFitAddon.proposeDimensions();
            if (dims) {
                this._aiWs.send(JSON.stringify({ type: 'resize', rows: dims.rows, cols: dims.cols }));
            }
        }
    }

    _getTerminalContainer() {
        // Return the DOM element for the current terminal tab
        const ref = this.state.activeContentTab === 'shell' ? 'terminalShell' : 'terminalLogs';
        return this.__owl__.refs[ref] || null;
    }

    _getAiTerminalContainer() {
        return this.__owl__.refs.terminalAI || null;
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
    // Git changes (AI tab panel) — loaded once on tab enter + manual refresh
    // ------------------------------------------------------------------

    _toggleGitPanel() {
        this.state.gitPanelCollapsed = !this.state.gitPanelCollapsed;
        rpc('/devops/user/prefs/save', { git_collapsed: this.state.gitPanelCollapsed }).catch(() => {});
    }

    _minimizeSidebar() {
        this.state.sidebarMinimized = true;
        rpc('/devops/user/prefs/save', { sidebar_minimized: true }).catch(() => {});
    }

    _expandSidebar() {
        this.state.sidebarMinimized = false;
        rpc('/devops/user/prefs/save', { sidebar_minimized: false }).catch(() => {});
    }

    _onGitRepoChange(ev) {
        this.state.gitSelectedRepo = ev.target.value;
        this._refreshGitStatus();
    }

    _onCommitMessageInput(ev) {
        this.state.gitCommitMessage = ev.target.value;
    }

    async _refreshGitStatus() {
        if (!this.state.currentProjectId) return;
        if (this.state.historyRepos.length === 0) {
            await this._loadHistoryRepos();
        }
        // Auto-select first repo if none selected
        if (!this.state.gitSelectedRepo && this.state.historyRepos.length > 0) {
            this.state.gitSelectedRepo = this.state.historyRepos[0].path;
        }
        const repoPath = this.state.gitSelectedRepo;
        if (!repoPath) return;
        try {
            const result = await rpc('/devops/git/status', {
                project_id: this.state.currentProjectId,
                instance_id: this.state.selectedInstance ? this.state.selectedInstance.id : null,
                repo_path: repoPath,
            });
            if (!result.error) {
                this.state.gitStaged = result.staged || [];
                this.state.gitUnstaged = result.unstaged || [];
                this.state.gitUntracked = result.untracked || [];
                this.state.gitOutgoing = result.outgoing || [];
            }
        } catch (e) { /* ignore */ }
    }

    async _gitShowDiff(filePath, staged = false) {
        if (!this.state.gitSelectedRepo || !this.state.currentProjectId) return;
        // Toggle off if clicking the same file
        if (this.state.gitDiffFile === filePath && this.state.gitDiffStaged === staged) {
            this.state.gitDiffFile = '';
            this.state.gitDiffContent = '';
            return;
        }
        this.state.gitDiffFile = filePath;
        this.state.gitDiffStaged = staged;
        this.state.gitDiffContent = 'Cargando...';
        try {
            const result = await rpc('/devops/git/diff', {
                project_id: this.state.currentProjectId,
                repo_path: this.state.gitSelectedRepo,
                file_path: filePath,
                staged: staged,
            });
            this.state.gitDiffContent = result.diff || result.error || 'Sin diferencias';
        } catch (e) {
            this.state.gitDiffContent = 'Error cargando diff';
        }
    }

    async _loadGitPanelWidth() {
        try {
            const result = await rpc('/devops/user/prefs');
            if (result.git_panel_width) {
                this.state.gitPanelWidth = result.git_panel_width;
            }
        } catch (e) { /* ignore */ }
    }

    _saveGitPanelWidth() {
        rpc('/devops/user/prefs/save', { git_panel_width: this.state.gitPanelWidth }).catch(() => {});
    }

    _onResizeStart(ev) {
        ev.preventDefault();
        this.state.gitResizing = true;
        const startX = ev.type === 'touchstart' ? ev.touches[0].clientX : ev.clientX;
        const startWidth = this.state.gitPanelWidth;

        const onMove = (e) => {
            const clientX = e.type === 'touchmove' ? e.touches[0].clientX : e.clientX;
            const delta = clientX - startX;
            this.state.gitPanelWidth = Math.max(150, Math.min(600, startWidth + delta));
        };

        const onEnd = () => {
            this.state.gitResizing = false;
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onEnd);
            document.removeEventListener('touchmove', onMove);
            document.removeEventListener('touchend', onEnd);
            this._saveGitPanelWidth();
        };

        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onEnd);
        document.addEventListener('touchmove', onMove, { passive: false });
        document.addEventListener('touchend', onEnd);
    }

    async _checkGitAuth() {
        try {
            const result = await rpc('/devops/git/auth/check');
            this.state.gitAuthIsAdmin = result.is_admin || false;
            this.state.gitAuthenticated = result.authenticated || false;
        } catch (e) { /* ignore */ }
    }

    _onGitAuthLoginInput(ev) { this.state.gitAuthLogin = ev.target.value; }
    _onGitAuthPasswordInput(ev) { this.state.gitAuthPassword = ev.target.value; }
    _onGitAuthKeyup(ev) { if (ev.key === 'Enter') this._gitLogin(); }

    async _gitLogin() {
        if (!this.state.gitAuthLogin || !this.state.gitAuthPassword) return;
        this.state.gitAuthLoading = true;
        this.state.gitAuthError = '';
        try {
            const result = await rpc('/devops/git/auth', {
                login: this.state.gitAuthLogin,
                password: this.state.gitAuthPassword,
            });
            if (result.error) {
                this.state.gitAuthError = result.error;
            } else {
                this.state.gitAuthenticated = true;
                this.state.gitAuthPassword = '';
                this.state.gitAuthError = '';
            }
        } catch (e) {
            this.state.gitAuthError = 'Error de conexión';
        }
        this.state.gitAuthLoading = false;
    }

    async _gitStageAll() {
        if (!this.state.gitSelectedRepo || !this.state.currentProjectId) return;
        try {
            await rpc('/devops/git/stage', {
                project_id: this.state.currentProjectId,
                repo_path: this.state.gitSelectedRepo,
            });
            await this._refreshGitStatus();
        } catch (e) { /* ignore */ }
    }

    async _gitCommit() {
        const msg = this.state.gitCommitMessage.trim();
        if (!msg || !this.state.gitSelectedRepo || !this.state.currentProjectId) return;
        this.state.gitCommitting = true;
        try {
            const result = await rpc('/devops/git/commit', {
                project_id: this.state.currentProjectId,
                repo_path: this.state.gitSelectedRepo,
                message: msg,
            });
            if (result.auth_required) {
                this.state.gitAuthenticated = false;
                return;
            }
            if (result.error) {
                alert('Error: ' + result.error);
            } else {
                this.state.gitCommitMessage = '';
            }
            await this._refreshGitStatus();
        } catch (e) {
            alert('Error: ' + (e.message || e));
        }
        this.state.gitCommitting = false;
    }

    async _gitPush() {
        if (!this.state.gitSelectedRepo || !this.state.currentProjectId) return;
        this.state.gitPushing = true;
        try {
            const result = await rpc('/devops/git/push', {
                project_id: this.state.currentProjectId,
                repo_path: this.state.gitSelectedRepo,
            });
            if (result.auth_required) {
                this.state.gitAuthenticated = false;
                this.state.gitPushing = false;
                return;
            }
            if (result.error) {
                alert('Error: ' + result.error);
            }
            await this._refreshGitStatus();
        } catch (e) {
            alert('Error: ' + (e.message || e));
        }
        this.state.gitPushing = false;
    }

    async _gitCommitAndPush() {
        await this._gitCommit();
        if (!this.state.gitCommitMessage) {
            // Commit succeeded (message was cleared)
            await this._gitPush();
        }
    }

    async _gitPull() {
        if (!this.state.gitSelectedRepo || !this.state.currentProjectId) return;
        this.state.gitPushing = true; // reuse for loading state
        try {
            const result = await rpc('/devops/git/pull', {
                project_id: this.state.currentProjectId,
                repo_path: this.state.gitSelectedRepo,
            });
            if (result.error) {
                alert('Error: ' + result.error);
            }
            await this._refreshGitStatus();
        } catch (e) {
            alert('Error: ' + (e.message || e));
        }
        this.state.gitPushing = false;
    }

    async _gitMerge(source, target) {
        if (!this.state.gitSelectedRepo || !this.state.currentProjectId) return;
        if (!confirm(`¿Merge ${source} → ${target}?`)) return;
        this.state.gitPushing = true;
        try {
            const result = await rpc('/devops/branch/merge', {
                project_id: this.state.currentProjectId,
                repo_path: this.state.gitSelectedRepo,
                source_branch: source,
                target_branch: target,
            });
            if (result.error) {
                alert('Error: ' + result.error);
            } else {
                alert(result.output || 'Merge exitoso');
            }
            await this._refreshGitStatus();
        } catch (e) {
            alert('Error: ' + (e.message || e));
        }
        this.state.gitPushing = false;
    }

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

    // ------------------------------------------------------------------
    // Editor context menu
    // ------------------------------------------------------------------

    _onFileContextMenu(ev, item) {
        ev.preventDefault();
        ev.stopPropagation();
        const basePath = this._editorBaseDir || '';
        const fullPath = basePath ? `${basePath}/${item.path}` : item.path;
        // Store data separately so it survives ctxMenu being nulled
        this._ctxData = { name: item.name, path: item.path, fullPath };
        this.state.ctxMenu = {
            x: ev.clientX,
            y: ev.clientY,
            item: item,
            fullPath: fullPath,
        };
        // Close on next click anywhere (non-capture, so menu handlers fire first)
        const close = () => {
            // Delay null so copy handlers can read ctxData
            setTimeout(() => { this.state.ctxMenu = null; }, 50);
            document.removeEventListener('click', close);
            document.removeEventListener('contextmenu', close);
        };
        setTimeout(() => {
            document.addEventListener('click', close);
            document.addEventListener('contextmenu', close);
        }, 0);
    }

    _ctxCopyName() { this._ctxCopy(this._ctxData?.name || ''); }
    _ctxCopyRelPath() { this._ctxCopy(this._ctxData?.path || ''); }
    _ctxCopyFullPath() { this._ctxCopy(this._ctxData?.fullPath || ''); }

    async _ctxCopy(text) {
        this.state.ctxMenu = null;
        if (!text) return;
        try {
            await navigator.clipboard.writeText(text);
        } catch (e) {
            // Fallback for older browsers or permission issues
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.left = '-9999px';
            ta.style.top = '-9999px';
            document.body.appendChild(ta);
            ta.focus();
            ta.select();
            try { document.execCommand('copy'); } catch (e2) {}
            document.body.removeChild(ta);
        }
    }

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

    async _loadUpgradeRepos() {
        if (!this.state.selectedInstance || !this.state.currentProjectId) return;
        try {
            const result = await rpc('/devops/instance/repos', {
                project_id: this.state.currentProjectId,
                instance_id: this.state.selectedInstance.id,
            });
            this.state.upgradeRepos = (result.repos || []).filter(
                r => r.repo_type === 'custom'
            );
        } catch (e) {
            this.state.upgradeRepos = [];
        }
    }

    async _deployChanges() {
        await this._startDeploy('');
    }

    async _deployRepo(ev) {
        const btn = ev.target.closest('button[data-path]');
        const repoPath = btn ? btn.dataset.path : '';
        if (repoPath) await this._startDeploy(repoPath);
    }

    async _startDeploy(repoPath) {
        if (!this.state.selectedInstance || this.state.deploying) return;
        this.state.deploying = true;
        this.state.deployLog = '';
        this.state.deployResult = null;
        try {
            const result = await rpc('/devops/instance/deploy', {
                instance_id: this.state.selectedInstance.id,
                repo_path: repoPath,
            });
            if (result.error) {
                this.state.deployResult = { ok: false, message: result.error };
                this.state.deploying = false;
                return;
            }
            // Poll for status
            this._deployLogPos = 0;
            this._pollDeploy(result.deploy_id);
        } catch (e) {
            this.state.deployResult = { ok: false, message: 'Error: ' + (e.message || e) };
            this.state.deploying = false;
        }
    }

    async _pollDeploy(deployId) {
        try {
            const result = await rpc('/devops/instance/deploy_status', {
                deploy_id: deployId,
                log_pos: this._deployLogPos || 0,
            });
            if (result.log) {
                this.state.deployLog += result.log;
            }
            this._deployLogPos = result.log_pos || 0;

            if (result.status === 'done') {
                this.state.deploying = false;
                this.state.deployResult = { ok: true, message: 'Deploy completado' };
                await this._loadUpgradeRepos();
            } else {
                setTimeout(() => this._pollDeploy(deployId), 2000);
            }
        } catch (e) {
            setTimeout(() => this._pollDeploy(deployId), 3000);
        }
    }

    async _loadDashboard() {
        if (!this.state.currentProjectId) return;
        try {
            this.state.dashboard = await rpc('/devops/reports/dashboard', { project_id: this.state.currentProjectId });
        } catch (e) { this.state.dashboard = null; }
    }

    // ---- Meetings ----

    async _loadMeetings() {
        if (!this.state.currentProjectId) return;
        try {
            const result = await rpc('/devops/meetings/list', { project_id: this.state.currentProjectId });
            this.state.meetings = result.meetings || [];
        } catch (e) { this.state.meetings = []; }
    }

    _meetCreate() { this.state.meetCreating = true; this.state.meetNewName = ''; this.state.meetNewUrl = ''; this.state.meetType = 'jitsi'; }
    _meetCancelCreate() { this.state.meetCreating = false; }
    _meetSetTypeJitsi() { this.state.meetType = 'jitsi'; }
    _meetSetTypeExternal() { this.state.meetType = 'external'; }
    _onMeetNameInput(ev) { this.state.meetNewName = ev.target.value; }
    _onMeetUrlInput(ev) { this.state.meetNewUrl = ev.target.value; }

    async _meetSave() {
        if (!this.state.meetNewName.trim()) return;
        const result = await rpc('/devops/meetings/create', {
            project_id: this.state.currentProjectId,
            name: this.state.meetNewName,
            meet_url: this.state.meetNewUrl,
            meet_type: this.state.meetType,
            instance_id: this.state.selectedInstance ? this.state.selectedInstance.id : null,
        });
        this.state.meetCreating = false;
        await this._loadMeetings();
        // Auto-join if Jitsi
        if (result.jitsi_room && this.state.meetType === 'jitsi') {
            this._startJitsiCall(result.id, this.state.meetNewName, result.jitsi_room);
        }
    }

    async _meetQuickCall() {
        const name = 'Llamada ' + new Date().toLocaleTimeString();
        const result = await rpc('/devops/meetings/create', {
            project_id: this.state.currentProjectId,
            name: name,
            meet_type: 'jitsi',
            instance_id: this.state.selectedInstance ? this.state.selectedInstance.id : null,
        });
        await this._loadMeetings();
        if (result.jitsi_room) {
            this._startJitsiCall(result.id, name, result.jitsi_room);
        }
    }

    _meetJoinJitsi(ev) {
        const mid = parseInt(ev.currentTarget.dataset.mid);
        const meeting = this.state.meetings.find(m => m.id === mid);
        if (meeting && meeting.jitsi_room) {
            this._startJitsiCall(mid, meeting.name, meeting.jitsi_room);
        }
    }

    async _startJitsiCall(meetingId, name, roomName) {
        // Load Jitsi API
        if (!window.JitsiMeetExternalAPI) {
            await this._loadScript('https://meet.jit.si/external_api.js');
        }
        this.state.meetActiveId = meetingId;
        this.state.meetActiveName = name;
        // Update state to in_progress
        await rpc('/devops/meetings/update', { meeting_id: meetingId, state: 'in_progress' });

        // Wait for DOM to render the container
        await new Promise(r => setTimeout(r, 200));
        const container = this.__owl__.refs.jitsiContainer;
        if (!container) return;

        const user = this.state.currentProject ? this.state.currentProject.name : 'PMB';
        this._jitsiApi = new window.JitsiMeetExternalAPI('meet.jit.si', {
            roomName: roomName,
            parentNode: container,
            width: '100%',
            height: '100%',
            configOverwrite: {
                startWithAudioMuted: false,
                startWithVideoMuted: true,
                prejoinPageEnabled: false,
                disableDeepLinking: true,
            },
            interfaceConfigOverwrite: {
                TOOLBAR_BUTTONS: [
                    'microphone', 'camera', 'desktop', 'chat',
                    'recording', 'raisehand', 'tileview', 'hangup',
                ],
                SHOW_JITSI_WATERMARK: false,
                DEFAULT_BACKGROUND: '#1e1e2e',
            },
            userInfo: {
                displayName: user,
            },
        });

        this._jitsiApi.addListener('readyToClose', () => {
            this._meetEndCall();
        });
    }

    async _meetStartRecording(ev) {
        const mid = parseInt(ev.currentTarget.dataset.mid);
        try {
            let stream;
            const isMobile = window.innerWidth <= 768 || /Android|iPhone|iPad/i.test(navigator.userAgent);

            if (isMobile || !navigator.mediaDevices.getDisplayMedia) {
                // Mobile: mic captures speaker output + your voice
                stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            } else {
                // Desktop: mix tab audio + mic for full conversation capture
                try {
                    const tabStream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
                    tabStream.getVideoTracks().forEach(t => t.stop());

                    if (tabStream.getAudioTracks().length > 0) {
                        // Also get mic
                        let micStream;
                        try {
                            micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
                        } catch (e) { /* mic denied, tab-only */ }

                        // Mix both with AudioContext
                        const ctx = new AudioContext();
                        const dest = ctx.createMediaStreamDestination();
                        ctx.createMediaStreamSource(tabStream).connect(dest);
                        if (micStream) {
                            ctx.createMediaStreamSource(micStream).connect(dest);
                            this._recordMicStream = micStream;
                        }
                        this._recordAudioCtx = ctx;
                        this._recordTabStream = tabStream;
                        stream = dest.stream;
                    } else {
                        tabStream.getTracks().forEach(t => t.stop());
                        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                    }
                } catch (displayErr) {
                    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                }
            }

            if (!stream || stream.getAudioTracks().length === 0) {
                alert('No se pudo acceder al audio. Verifica los permisos del navegador.');
                return;
            }

            this._recordStream = stream;
            this._recordChunks = [];

            // Use a mimeType that works across browsers
            let mimeType = 'audio/webm';
            for (const mt of ['audio/webm;codecs=opus', 'audio/webm', 'video/webm', 'audio/ogg']) {
                if (MediaRecorder.isTypeSupported(mt)) { mimeType = mt; break; }
            }
            this._mediaRecorder = new MediaRecorder(this._recordStream, { mimeType });

            this._mediaRecorder.ondataavailable = (e) => {
                if (e.data.size > 0) this._recordChunks.push(e.data);
            };

            this._mediaRecorder.onstop = async () => {
                const blob = new Blob(this._recordChunks, { type: 'audio/webm' });
                // Convert to base64 and upload
                const reader = new FileReader();
                reader.onload = async () => {
                    const base64 = reader.result.split(',')[1];
                    await rpc('/devops/meetings/upload_audio', {
                        meeting_id: mid,
                        audio_data: base64,
                        filename: `recording_${Date.now()}.webm`,
                    });
                    await rpc('/devops/meetings/update', { meeting_id: mid, state: 'done' });
                    await this._loadMeetings();
                };
                reader.readAsDataURL(blob);
                this._recordChunks = [];
            };

            this._mediaRecorder.start(1000); // collect data every second
            this.state.meetRecordingId = mid;
            this._recordStartTime = Date.now();

            // Timer display
            this._recordTimer = setInterval(() => {
                const elapsed = Math.floor((Date.now() - this._recordStartTime) / 1000);
                const min = Math.floor(elapsed / 60);
                const sec = elapsed % 60;
                this.state.meetRecordingTime = `${min}:${sec.toString().padStart(2, '0')}`;
            }, 1000);

            // Update state
            await rpc('/devops/meetings/update', { meeting_id: mid, state: 'in_progress' });
            await this._loadMeetings();

            // Auto-stop if stream ends (user stops sharing)
            const autoStop = () => this._meetStopRecording();
            this._recordStream.getAudioTracks().forEach(t => t.onended = autoStop);
            if (this._recordTabStream) {
                this._recordTabStream.getAudioTracks().forEach(t => t.onended = autoStop);
            }

        } catch (e) {
            if (e.name !== 'NotAllowedError') {
                alert('Error iniciando grabacion: ' + e.message);
            }
        }
    }

    async _meetStopRecording() {
        if (this._mediaRecorder && this._mediaRecorder.state !== 'inactive') {
            this._mediaRecorder.stop();
        }
        if (this._recordStream) {
            this._recordStream.getTracks().forEach(t => t.stop());
            this._recordStream = null;
        }
        if (this._recordTabStream) {
            this._recordTabStream.getTracks().forEach(t => t.stop());
            this._recordTabStream = null;
        }
        if (this._recordMicStream) {
            this._recordMicStream.getTracks().forEach(t => t.stop());
            this._recordMicStream = null;
        }
        if (this._recordAudioCtx) {
            this._recordAudioCtx.close().catch(() => {});
            this._recordAudioCtx = null;
        }
        if (this._recordTimer) {
            clearInterval(this._recordTimer);
            this._recordTimer = null;
        }
        // Save duration
        if (this.state.meetRecordingId && this._recordStartTime) {
            const duration = Math.round((Date.now() - this._recordStartTime) / 60000);
            await rpc('/devops/meetings/update', {
                meeting_id: this.state.meetRecordingId,
                duration: duration || 1,
            });
        }
        this.state.meetRecordingId = null;
        this.state.meetRecordingTime = '';
        this._recordStartTime = null;
    }

    async _meetEndCall() {
        if (this._jitsiApi) {
            this._jitsiApi.dispose();
            this._jitsiApi = null;
        }
        if (this.state.meetActiveId) {
            await rpc('/devops/meetings/update', { meeting_id: this.state.meetActiveId, state: 'done' });
        }
        this.state.meetActiveId = null;
        this.state.meetActiveName = '';
        await this._loadMeetings();
    }

    async _meetDelete(ev) {
        const mid = parseInt(ev.currentTarget.dataset.mid);
        if (!confirm('Eliminar esta reunion?')) return;
        await rpc('/devops/meetings/delete', { meeting_id: mid });
        await this._loadMeetings();
    }

    async _meetSaveNotes(ev) {
        const mid = parseInt(ev.target.dataset.mid);
        if (!mid) return;
        await rpc('/devops/meetings/update', { meeting_id: mid, notes: ev.target.value });
    }

    async _meetUploadAudio(ev) {
        const mid = parseInt(ev.target.dataset.mid);
        const file = ev.target.files && ev.target.files[0];
        if (!mid || !file) return;
        const reader = new FileReader();
        reader.onload = async () => {
            const base64 = reader.result.split(',')[1];
            await rpc('/devops/meetings/upload_audio', {
                meeting_id: mid,
                audio_data: base64,
                filename: file.name,
            });
            await this._loadMeetings();
        };
        reader.readAsDataURL(file);
    }

    async _meetTranscribe(ev) {
        const mid = parseInt(ev.currentTarget.dataset.mid);
        ev.currentTarget.disabled = true;
        ev.currentTarget.textContent = 'Transcribiendo...';
        try {
            const result = await rpc('/devops/meetings/transcribe', { meeting_id: mid });
            if (result.error) {
                alert('Error: ' + result.error);
            } else {
                this.state.meetTranscriptionId = mid;
                this.state.meetTranscription = result.transcription;
            }
            await this._loadMeetings();
        } catch (e) { alert('Error: ' + e.message); }
    }

    async _meetShowTranscription(ev) {
        const mid = parseInt(ev.currentTarget.dataset.mid);
        if (this.state.meetTranscriptionId === mid) {
            this.state.meetTranscriptionId = null;
            this.state.meetTranscription = '';
            return;
        }
        const result = await rpc('/devops/meetings/transcription', { meeting_id: mid });
        this.state.meetTranscriptionId = mid;
        this.state.meetTranscription = result.transcription || '';
    }

    async _meetAnalyzeTasks(ev) {
        const mid = parseInt(ev.currentTarget.dataset.mid);
        ev.currentTarget.disabled = true;
        ev.currentTarget.textContent = '🤖 Analizando...';
        try {
            const result = await rpc('/devops/meetings/analyze', { meeting_id: mid });
            if (result.error) {
                alert('Error: ' + result.error);
            } else {
                this.state.meetAnalyzedId = mid;
                this.state.meetAnalyzedTasks = result.tasks || [];
            }
        } catch (e) { alert('Error: ' + e.message); }
        ev.currentTarget.disabled = false;
        ev.currentTarget.textContent = '🤖 Extraer tareas con IA';
    }

    async _meetCreateTasks(ev) {
        const mid = parseInt(ev.currentTarget.dataset.mid);
        try {
            const result = await rpc('/devops/meetings/create_tasks', {
                meeting_id: mid,
                tasks: this.state.meetAnalyzedTasks,
            });
            if (result.error) {
                alert('Error: ' + result.error);
            } else {
                this.state.meetAnalyzedId = null;
                this.state.meetAnalyzedTasks = [];
                // Show created tasks
                this.state.meetTasksId = mid;
                const tasksResult = await rpc('/devops/meetings/tasks', { meeting_id: mid });
                this.state.meetTasks = tasksResult.tasks || [];
            }
        } catch (e) { alert('Error: ' + e.message); }
    }

    async _onGroqKeyChange(ev) {
        const key = ev.target.value;
        this.state.groqApiKey = key;
        await rpc('/devops/settings/groq_key', { key });
    }

    // ---- Diagnostics ----

    async _runDiagnose() {
        if (!this.state.selectedInstance) return;
        this.state.fixResult = null;
        try {
            this.state.diagnoseResult = await rpc('/devops/instance/diagnose', {
                instance_id: this.state.selectedInstance.id,
            });
        } catch (e) { this.state.diagnoseResult = { issues: [{ type: 'error', msg: 'Error: ' + e.message }], info: [], total_issues: 1 }; }
    }

    async _runFix(ev) {
        if (!this.state.selectedInstance) return;
        const fixType = ev.currentTarget.dataset.fix;
        try {
            this.state.fixResult = await rpc('/devops/instance/fix', {
                instance_id: this.state.selectedInstance.id,
                fix_type: fixType,
            });
            await this._runDiagnose();
            await this._loadProjectData();
        } catch (e) { this.state.fixResult = { error: e.message }; }
    }

    async _restartInstance() {
        if (!this.state.selectedInstance) return;
        await rpc('/devops/instance/restart', { instance_id: this.state.selectedInstance.id });
        await this._loadProjectData();
    }

    // ------------------------------------------------------------------
    // Server metrics
    // ------------------------------------------------------------------

    async _loadMetrics() {
        if (!this.state.currentProjectId) return;
        try {
            const result = await rpc('/devops/project/metrics', {
                project_id: this.state.currentProjectId,
            });
            if (!result.error) {
                this.state.serverMetrics = result.metrics;
                this.state.metricsUpdated = result.updated || '';
            }
        } catch (e) {}
    }

    async _refreshMetrics() {
        if (!this.state.currentProjectId) return;
        try {
            const result = await rpc('/devops/project/metrics', {
                project_id: this.state.currentProjectId,
                refresh: true,
            });
            if (!result.error) {
                this.state.serverMetrics = result.metrics;
                this.state.metricsUpdated = result.updated || '';
            }
        } catch (e) {}
    }

    // ------------------------------------------------------------------
    // Register production instance
    // ------------------------------------------------------------------

    _onProdSetupInput(ev) {
        if (ev.target.dataset.field === 'prodService') {
            this.state.prodSetup.service = ev.target.value;
        }
    }

    _onProdSetupKey(ev) {
        if (ev.key === 'Enter') this._detectService();
    }

    async _detectService() {
        const name = this.state.prodSetup.service.trim();
        if (!name) return;
        this.state.prodSetup = { service: name, detected: false, error: '' };
        try {
            const r = await rpc('/devops/instance/detect_service', { service_name: name });
            if (r.error) {
                this.state.prodSetup.error = r.error;
            } else {
                this.state.prodSetup = {
                    service: name,
                    detected: true,
                    active: r.active || false,
                    db: r.database_name || '',
                    port: r.port || 8069,
                    gevent_port: r.gevent_port || 0,
                    path: r.instance_path || '',
                    config_path: r.config_path || '',
                    repo_path: r.repo_path || '',
                    enterprise_path: r.enterprise_path || '',
                    error: '',
                };
            }
        } catch (e) {
            this.state.prodSetup.error = 'Error: ' + (e.message || e);
        }
    }

    async _registerProduction() {
        const s = this.state.prodSetup;
        if (!s.service || !s.db) {
            alert('Servicio y base de datos son requeridos');
            return;
        }
        try {
            const result = await rpc('/devops/instance/register_production', {
                project_id: this.state.currentProjectId,
                service_name: s.service,
                database_name: s.db,
                port: s.port || 8069,
                instance_path: s.path || '',
            });
            if (result.error) {
                alert(result.error);
            } else {
                await this._loadProjectData();
            }
        } catch (e) {
            alert('Error: ' + (e.message || e));
        }
    }

    // ------------------------------------------------------------------
    // Settings / Project management
    // ------------------------------------------------------------------

    async _loadSettings() {
        // Skip if creating a new project (form already set to empty)
        if (this.state.settingsProject && !this.state.settingsProject.id) return;
        if (!this.state.currentProjectId) return;
        this.state.settingsSaved = false;
        this.state.sshPublicKey = '';
        this.state.sshTestResult = null;
        try {
            const result = await rpc('/devops/project/get', {
                project_id: this.state.currentProjectId,
            });
            if (result.error) {
                this.state.settingsProject = null;
            } else {
                this.state.settingsProject = result;
            }
        } catch (e) {
            this.state.settingsProject = null;
        }
        // Load members and available users
        await this._loadMembers();
        await this._loadAvailableUsers();
    }

    async _loadAvailableUsers() {
        try {
            const result = await rpc('/devops/users/list');
            this.state.availableUsers = result.users || [];
        } catch (e) { this.state.availableUsers = []; }
    }

    async _loadMembers() {
        if (!this.state.currentProjectId) return;
        try {
            const result = await rpc('/devops/project/members', { project_id: this.state.currentProjectId });
            this.state.projectMembers = result.members || [];
        } catch (e) { this.state.projectMembers = []; }
    }

    _onMemberUserSelect(ev) { this.state.memberNewLogin = ev.target.value; }
    _onMemberRoleInput(ev) { this.state.memberNewRole = ev.target.value; }

    async _addMember() {
        if (!this.state.memberNewLogin || !this.state.currentProjectId) return;
        this.state.memberError = '';
        const result = await rpc('/devops/project/members/add', {
            project_id: this.state.currentProjectId,
            user_login: this.state.memberNewLogin,
            role: this.state.memberNewRole || 'developer',
        });
        if (result.error) {
            this.state.memberError = result.error;
        } else {
            this.state.memberNewLogin = '';
            await this._loadMembers();
        }
    }

    async _removeMember(ev) {
        const mid = parseInt(ev.currentTarget.dataset.mid);
        await rpc('/devops/project/members/remove', { member_id: mid });
        await this._loadMembers();
    }

    async _onMemberRoleChange(ev) {
        const mid = parseInt(ev.target.dataset.mid);
        await rpc('/devops/project/members/update_role', { member_id: mid, role: ev.target.value });
        await this._loadMembers();
    }

    _newProject() {
        this.state.settingsProject = {
            id: null,
            name: '',
            domain: '',
            repo_path: '',
            enterprise_path: '',
            database_name: '',
            connection_type: 'local',
            ssh_host: '',
            ssh_user: 'root',
            ssh_port: 22,
            ssh_key_path: '',
            max_staging: 3,
            max_development: 5,
            auto_destroy_hours: 24,
            production_branch: 'main',
            ssh_key_configured: false,
        };
        this.state.sshPublicKey = '';
        this.state.settingsSaved = false;
    }

    async _saveProject() {
        const p = this.state.settingsProject;
        if (!p) return;
        try {
            const result = await rpc('/devops/project/save', {
                project_id: p.id || null,
                name: p.name,
                domain: p.domain,
                repo_path: p.repo_path,
                enterprise_path: p.enterprise_path,
                database_name: p.database_name,
                connection_type: p.connection_type,
                ssh_host: p.ssh_host,
                ssh_user: p.ssh_user,
                ssh_port: p.ssh_port,
                max_staging: p.max_staging,
                max_development: p.max_development,
                auto_destroy_hours: p.auto_destroy_hours,
                production_branch: p.production_branch,
            });
            if (result.error) {
                alert(result.error);
            } else {
                this.state.settingsSaved = true;
                if (!p.id) {
                    p.id = result.project_id;
                }
                // Reload projects list
                await this._loadProjects();
                this.state.currentProjectId = p.id;
                this.state.currentProject = this.state.projects.find(pr => pr.id === p.id) || null;
                setTimeout(() => { this.state.settingsSaved = false; }, 3000);
            }
        } catch (e) {
            alert('Error: ' + (e.message || e));
        }
    }

    async _generateSshKey() {
        const p = this.state.settingsProject;
        if (!p || !p.id) {
            alert('Guarda el proyecto primero');
            return;
        }
        try {
            const result = await rpc('/devops/project/generate_ssh_key', {
                project_id: p.id,
            });
            if (result.error) {
                alert(result.error);
            } else {
                this.state.sshPublicKey = result.public_key;
                p.ssh_key_path = result.key_path;
                p.ssh_key_configured = true;
            }
        } catch (e) {
            alert('Error: ' + (e.message || e));
        }
    }

    _onSettingsInput(ev) {
        const field = ev.target.dataset.field;
        if (!field || !this.state.settingsProject) return;
        const val = ev.target.dataset.type === 'int' ? parseInt(ev.target.value) || 0 : ev.target.value;
        this.state.settingsProject[field] = val;
    }

    _onConnectionTypeChange(ev) {
        this.state.settingsProject.connection_type = ev.target.value;
    }

    async _testSshConnection() {
        const p = this.state.settingsProject;
        if (!p || !p.id) return;
        this.state.sshTestResult = null;
        try {
            const result = await rpc('/devops/project/test_ssh', {
                project_id: p.id,
            });
            this.state.sshTestResult = {
                ok: result.status === 'ok',
                message: result.message || result.error || '',
            };
        } catch (e) {
            this.state.sshTestResult = { ok: false, message: e.message || 'Error' };
        }
    }

    // ------------------------------------------------------------------
    // AI Terminal (xterm.js via WebSocket)
    // ------------------------------------------------------------------

    async _initAiTerminal() {
        if (this._aiTermInitializing) return;
        this._aiTermInitializing = true;

        // Initialize client-side scrollback buffer if not exists
        if (!this._aiScrollback) this._aiScrollback = [];

        try {
            // Dispose old xterm (DOM was destroyed by t-if)
            if (this._aiResizeObserver) { this._aiResizeObserver.disconnect(); this._aiResizeObserver = null; }
            if (this._aiTerm) { this._aiTerm.dispose(); this._aiTerm = null; }
            this._aiFitAddon = null;

            // Load xterm.js from CDN if not loaded
            if (!window.Terminal) {
                await this._loadScript('https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js');
                await this._loadCSS('https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css');
                await this._loadScript('https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js');
            }

            const container = this._getAiTerminalContainer();
            if (!container) return;

            // Create xterm
            this._aiTerm = new window.Terminal({
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
            this._aiFitAddon = new window.FitAddon.FitAddon();
            this._aiTerm.loadAddon(this._aiFitAddon);
            this._aiTerm.open(container);
            this._aiFitAddon.fit();

            // Re-fit on container resize
            this._aiResizeObserver = new ResizeObserver(() => {
                if (this._aiFitAddon) { try { this._aiFitAddon.fit(); } catch (e) {} }
            });
            this._aiResizeObserver.observe(container);

            // Forward keyboard input to WebSocket
            this._aiTerm.onData((data) => {
                if (this._aiWs && this._aiWs.readyState === WebSocket.OPEN) {
                    this._aiWs.send(JSON.stringify({ type: 'input', data: data }));
                }
            });

            // Image paste support: intercept clipboard paste on the terminal container
            this._aiPasteHandler = (ev) => {
                const items = ev.clipboardData && ev.clipboardData.items;
                if (!items) return;
                for (const item of items) {
                    if (item.type.startsWith('image/')) {
                        ev.preventDefault();
                        ev.stopPropagation();
                        const blob = item.getAsFile();
                        if (!blob) return;
                        const reader = new FileReader();
                        reader.onload = () => {
                            const base64 = reader.result.split(',')[1];
                            if (this._aiWs && this._aiWs.readyState === WebSocket.OPEN) {
                                this._aiWs.send(JSON.stringify({
                                    type: 'image',
                                    data: base64,
                                    filename: `paste_${Date.now()}.png`,
                                }));
                                if (this._aiTerm) {
                                    this._aiTerm.writeln('\x1b[33m[Imagen pegada: enviando...]\x1b[0m');
                                }
                            }
                        };
                        reader.readAsDataURL(blob);
                        return;
                    }
                }
            };
            container.addEventListener('paste', this._aiPasteHandler, true);
            // Also listen on document for when xterm has focus
            document.addEventListener('paste', this._aiPasteHandler, false);

            // If WebSocket already connected, replay client scrollback and reattach
            if (this._aiWs && this._aiWs.readyState === WebSocket.OPEN) {
                // Replay saved scrollback to new xterm
                if (this._aiScrollback && this._aiScrollback.length > 0) {
                    for (const chunk of this._aiScrollback) {
                        this._aiTerm.write(chunk);
                    }
                }
                this._aiWs.onmessage = (event) => this._onAiWsMessage(event);
                const dims = this._aiFitAddon.proposeDimensions();
                if (dims) {
                    this._aiWs.send(JSON.stringify({ type: 'resize', rows: dims.rows, cols: dims.cols }));
                }
                return;
            }

            // New WebSocket connection
            this._aiTerm.writeln('\x1b[33mConectando a Claude Code...\x1b[0m');

            const tokenResult = await rpc('/devops/ai/token', {
                project_id: this.state.currentProjectId || null,
                instance_id: this.state.selectedInstance ? this.state.selectedInstance.id : null,
            });
            if (tokenResult.error) {
                this._aiTerm.writeln('\x1b[31mError: ' + tokenResult.error + '\x1b[0m');
                return;
            }

            const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${proto}//${location.host}${tokenResult.ws_url}`;
            this._aiWs = new WebSocket(wsUrl);

            this._aiWs.onopen = () => {
                this._aiWs.send(JSON.stringify({ token: tokenResult.token }));
            };
            this._aiWs.onmessage = (event) => this._onAiWsMessage(event);
            this._aiWs.onclose = () => {
                this.state.aiConnected = false;
                if (this._aiTerm) this._aiTerm.writeln('\r\n\x1b[31m[Sesion terminada]\x1b[0m');
            };
            this._aiWs.onerror = () => {
                this.state.aiConnected = false;
                if (this._aiTerm) this._aiTerm.writeln('\r\n\x1b[31m[Error de conexion WebSocket]\x1b[0m');
            };

        } catch (e) {
            if (this._aiTerm) this._aiTerm.writeln('\x1b[31mError: ' + (e.message || e) + '\x1b[0m');
        } finally {
            this._aiTermInitializing = false;
        }
    }

    _onAiWsMessage(event) {
        if (!this._aiTerm) return;
        if (typeof event.data === 'string') {
            try {
                const msg = JSON.parse(event.data);
                if (msg.type === 'ready') {
                    this.state.aiConnected = true;
                    if (msg.reattached) {
                        this._aiTerm.writeln('\x1b[32mReconectado a sesion existente\x1b[0m\r\n');
                    } else {
                        this._aiTerm.writeln('\x1b[32mConectado a Claude Code\x1b[0m\r\n');
                    }
                    const dims = this._aiFitAddon ? this._aiFitAddon.proposeDimensions() : null;
                    if (dims && this._aiWs) {
                        this._aiWs.send(JSON.stringify({ type: 'resize', rows: dims.rows, cols: dims.cols }));
                    }
                    return;
                }
                if (msg.type === 'error') {
                    this._aiTerm.writeln('\x1b[31mError: ' + msg.data + '\x1b[0m');
                    return;
                }
                if (msg.type === 'info') {
                    this._aiTerm.writeln('\x1b[32m' + msg.data + '\x1b[0m');
                    return;
                }
            } catch (e) {}
        }
        // Write to terminal and save to client scrollback
        let chunk = event.data;
        if (event.data instanceof ArrayBuffer) {
            chunk = new Uint8Array(event.data);
            this._aiTerm.write(chunk);
        } else if (event.data instanceof Blob) {
            event.data.arrayBuffer().then(buf => {
                const u8 = new Uint8Array(buf);
                if (this._aiTerm) this._aiTerm.write(u8);
                if (this._aiScrollback) this._aiScrollback.push(u8);
            });
            return;
        } else {
            this._aiTerm.write(chunk);
        }
        // Save to scrollback (keep max ~64KB worth of chunks)
        if (this._aiScrollback) {
            this._aiScrollback.push(chunk);
            // Limit: keep last 200 chunks
            if (this._aiScrollback.length > 200) {
                this._aiScrollback = this._aiScrollback.slice(-150);
            }
        }
    }

    async _loadClaudeSessions() {
        if (!this.state.selectedInstance) return;
        try {
            const result = await rpc('/devops/claude/sessions', {
                instance_id: this.state.selectedInstance.id,
                search: this.state.claudeSessionSearch,
            });
            this.state.claudeSessions = result.sessions || [];
        } catch (e) { this.state.claudeSessions = []; }
    }

    _onClaudeSessionSearch(ev) {
        this.state.claudeSessionSearch = ev.target.value;
        this._loadClaudeSessions();
    }

    async _deleteClaudeSession(sessionId) {
        if (!this.state.selectedInstance) return;
        try {
            await rpc('/devops/claude/sessions/delete', {
                instance_id: this.state.selectedInstance.id,
                session_id: sessionId,
            });
            await this._loadClaudeSessions();
        } catch (e) { /* ignore */ }
    }

    _toggleClaudeSessions() {
        this.state.claudeSessionsVisible = !this.state.claudeSessionsVisible;
        if (this.state.claudeSessionsVisible) this._loadClaudeSessions();
    }

    _onResumeSession(ev) {
        const sid = ev.currentTarget.dataset.sid;
        if (sid) this._resumeClaudeSession(sid);
    }

    _onDeleteSession(ev) {
        const sid = ev.currentTarget.dataset.sid;
        if (sid) this._deleteClaudeSession(sid);
    }

    _resumeClaudeSession(sessionId) {
        if (!this._aiWs || this._aiWs.readyState !== WebSocket.OPEN) return;
        const ws = this._aiWs;
        // Cancel any pending input, then send /resume inside Claude Code
        ws.send(JSON.stringify({ type: 'input', data: '\x03' }));  // Ctrl+C
        setTimeout(() => {
            ws.send(JSON.stringify({ type: 'input', data: `/resume ${sessionId}\n` }));
        }, 300);
        this.state.claudeSessionsVisible = false;
        if (this._aiTerm) this._aiTerm.focus();
    }

    _sendTermKey(data) {
        if (this._aiWs && this._aiWs.readyState === WebSocket.OPEN) {
            this._aiWs.send(JSON.stringify({ type: 'input', data }));
        }
        if (this._aiTerm) this._aiTerm.focus();
    }

    _sendShellKey(data) {
        // Shell terminal uses HTTP polling, send via the input endpoint
        if (this._termSessionId && this.state.currentProjectId) {
            rpc('/devops/terminal/input', {
                project_id: this.state.currentProjectId,
                session_id: this._termSessionId,
                data: data,
            }).catch(() => {});
        }
        if (this._term) this._term.focus();
    }

    _cleanupAiTerminal() {
        if (this._aiPasteHandler) {
            document.removeEventListener('paste', this._aiPasteHandler, false);
            this._aiPasteHandler = null;
        }
        if (this._aiResizeObserver) { this._aiResizeObserver.disconnect(); this._aiResizeObserver = null; }
        if (this._aiWs) { this._aiWs.close(); this._aiWs = null; }
        if (this._aiTerm) { this._aiTerm.dispose(); this._aiTerm = null; }
        this._aiFitAddon = null;
        this.state.aiConnected = false;
    }
}

registry.category("actions").add("pmb_devops_main", PmbDevopsApp);
