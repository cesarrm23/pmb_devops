/** @odoo-module **/

import { Component, markup, onMounted, onWillUnmount, useEffect, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { rpc } from "@web/core/network/rpc";
import { DevopsDescEditor } from "../desc_editor/desc_editor";

class PmbDevopsApp extends Component {
    static template = "pmb_devops.PmbDevopsApp";
    static components = { DevopsDescEditor };
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
            activeContentTab: "ai", // ai, shell, logs, backups, upgrade, tools

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
            isDeveloper: false,

            // Terminal
            terminalConnected: false,

            // Loading
            loadingMessage: '',

            // History
            commits: [],
            historySearch: '',
            historyRepos: [],
            historyRepoPath: '',

            // Project tasks (general, not per-instance)
            projectTasks: [],
            taskStatusFilter: 'all', // all|done|in_progress — set when entering from Status
            statusTasksOverlayOpen: false, // tasks panel rendered inline within Status tab
            // Modules panel state
            modulesInstanceId: null,
            modulesData: null,
            modulesLoading: false,
            modulesFilter: '',
            modulesRepoPath: '',  // '' = all repos; otherwise filter to one
            moduleActionRunning: false,
            moduleActionOutput: null,
            taskStages: [],
            taskMembers: [],
            taskMeetings: [],  // project meetings for linking
            productionUsers: [],
            showNewTask: false,
            newTaskName: '',
            expandedTaskId: null,
            taskFullscreenId: null,
            taskFullscreen: null,
            taskDescEditingId: null,
            taskDescDraft: '',
            taskNameEditingId: null,
            taskNameDraft: '',
            isDevopsAdmin: false,

            // SSL certificate issuance
            sslIssuing: false,

            // Commit ↔ task links
            taskLinkedCommits: [],        // commits linked to the open task
            taskCommitPickerOpen: false,  // show commit picker on task fullscreen
            taskCommitPickerSearch: '',   // search filter for picker
            commitLinkedTasks: {},        // { "<full_hash>": [ {task_id, name, ...}, ... ] }
            commitTaskPickerFor: null,    // commit full_hash whose picker is open
            commitTaskPickerSearch: '',

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
            meetTranscribingId: null,   // meeting being transcribed
            meetAnalyzingId: null,      // meeting being analyzed
            meetAnalyzedId: null,       // meeting with analyzed tasks pending
            meetAnalyzedTasks: [],      // tasks extracted by AI
            meetTasksId: null,          // meeting showing created tasks
            meetTasks: [],              // created Odoo tasks
            groqApiKey: '',
            copilotAuthenticated: false,
            copilotGithubUser: '',
            copilotAuthCode: '',
            copilotAuthUri: '',
            copilotAuthPolling: false,
            copilotDeviceCode: '',
            projectMembers: [],
            availableUsers: [],
            memberNewLogin: '',
            memberNewRole: 'developer',
            memberError: '',
            autodetectService: '',
            autodetectResult: null,
            gitPanelWidth: 280,         // resizable panel width (px)
            gitResizing: false,         // drag in progress
            gitSplitPercent: 60,        // % of panel height for git changes (top)
            claudeSessions: [],         // list of claude sessions
            claudeSessionSearch: '',    // search filter
            claudeSessionsVisible: false, // toggle sessions panel
            gitAuthenticated: false,    // git auth confirmed this session
            gitAuthIsAdmin: false,      // current user is admin (no auth needed)
            gitAuthLogin: '',
            gitAuthPassword: '',
            gitAuthError: '',
            gitAuthLoading: false,

            // GitHub credentials (per instance)
            githubConfigured: false,
            githubUser: '',
            githubToken: '',
            githubError: '',
            githubLoading: false,

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
            aiLastPrompt: '',
            aiPromptHistory: [],
            aiPromptHistoryOpen: false,

            // Creation log
            creationLog: '',
            creationPid: 0,

            // Production setup
            prodSetup: { service: '', db: '', port: 8069, path: '' },

            // Deploy / Upgrade
            deploying: false,
            postCloneResult: null,
            odooProjects: [],           // project.project list for Settings dropdown
            historyFullscreen: false,   // fullscreen history overlay
            deployLog: '',
            deployResult: null,
            upgradeRepos: [],

            // AI Agents
            agents: [],
            agentRuns: [],
            agentExpandedId: null,
            agentRunning: null,  // agent_id currently executing
            showNewAgent: false,
            newAgentName: '',
            claudePropagateResult: '',
            claudeCliUpgrading: false,
            claudeCliUpgradeResult: '',
            newAgentBranch: 'HEAD',
            newAgentInterval: 1,
            newAgentIntervalType: 'days',
            newAgentProvider: 'copilot',
            newAgentCopilotModel: 'claude-opus-4.7',
            newAgentPrompt: '',
            newAgentOutputFile: '',
            agentPromptEdits: {},  // {agent_id: new_prompt_text}
            agentOutputEdits: {},  // {agent_id: new_output_file}
            agentSaveStatus: {},  // {agent_id: 'saving'|'saved'|'error'}

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

        // Reload per-instance persisted "last prompt" when selection changes
        useEffect(
            (id) => { this._loadAiLastPromptForInstance(id); },
            () => [this.state.selectedInstance && this.state.selectedInstance.id],
        );

        onMounted(async () => {
            // Set DevOps favicon
            this._setDevOpsFavicon();

            // Prevent horizontal page scroll (mobile + desktop)
            document.documentElement.style.overflowX = 'hidden';
            document.body.style.overflowX = 'hidden';

            // Load persisted UI preferences
            try {
                const prefs = await rpc('/devops/user/prefs');
                if (prefs.git_panel_width) this.state.gitPanelWidth = prefs.git_panel_width;
                if (prefs.sidebar_minimized) this.state.sidebarMinimized = true;
                if (prefs.git_collapsed) this.state.gitPanelCollapsed = true;
            } catch (e) { /* ignore */ }
            // Check admin status early (global)
            try {
                const authCheck = await rpc('/devops/git/auth/check');
                this.state.isAdmin = authCheck.is_admin || false;
                this.state.isDeveloper = authCheck.is_developer || false;
            } catch (e) {}
            await this._loadProjects();
            if (this.state.projects.length > 0) {
                this.state.currentProjectId = this.state.projects[0].id;
                this.state.currentProject = this.state.projects[0];
                // Re-check role with project context
                try {
                    const authCheck2 = await rpc('/devops/git/auth/check', { project_id: this.state.currentProjectId });
                    this.state.isAdmin = authCheck2.is_admin || false;
                    this.state.isDeveloper = authCheck2.is_developer || false;
                } catch (e) {}
                await this._loadProjectData();
            }
            // Mobile keyboard: re-fit terminals and collapse header
            // Poll sidebar state every 30s to keep status indicators fresh
            this._statusPollTimer = setInterval(() => this._refreshSidebarState(), 30000);

            // Poll project tasks every 10s while AI tab is open — picks up tasks
            // pulled from client DBs by the cron (devops.project._cron_pull_remote_tasks).
            this._tasksPollTimer = setInterval(() => {
                if (this.state.activeContentTab === 'ai' && this.state.currentProjectId) {
                    this._loadProjectTasks();
                }
            }, 10000);

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
            // Restore original favicon
            this._restoreFavicon();
            // Restore horizontal scroll
            document.documentElement.style.overflowX = '';
            document.body.style.overflowX = '';
            if (this._pollTimer) {
                clearInterval(this._pollTimer);
                this._pollTimer = null;
            }
            if (this._statusPollTimer) {
                clearInterval(this._statusPollTimer);
                this._statusPollTimer = null;
            }
            if (this._tasksPollTimer) {
                clearInterval(this._tasksPollTimer);
                this._tasksPollTimer = null;
            }
            this._cleanupTerminal();
            this._cleanupShellTerminal();
            this._cleanupAiTerminal();
            this._stopGitPolling();
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
                        "production_branch",
                        "odoo_project_id",
                    ],
                    limit: 100,
                },
            });
            this.state.projects = projects;
        } catch (err) {
            console.error("PmbDevopsApp: error loading projects", err);
        }
    }

    async _refreshSidebarState() {
        // Lightweight poll: only update instance states without full reload
        if (!this.state.currentProjectId || !this.state.instances.length) return;
        try {
            const fresh = await rpc("/web/dataset/call_kw", {
                model: "devops.instance",
                method: "search_read",
                args: [[["project_id", "=", this.state.currentProjectId]]],
                kwargs: { fields: ["id", "state", "creation_step"], limit: 200 },
            });
            const map = {};
            for (const f of fresh) map[f.id] = f;
            let changed = false;
            for (const inst of this.state.instances) {
                const f = map[inst.id];
                if (f && f.state !== inst.state) {
                    inst.state = f.state;
                    inst.creation_step = f.creation_step;
                    changed = true;
                }
            }
            // Update selected instance too
            if (changed && this.state.selectedInstance) {
                const f = map[this.state.selectedInstance.id];
                if (f) {
                    this.state.selectedInstance.state = f.state;
                    this.state.selectedInstance.creation_step = f.creation_step;
                }
            }
            // Remove instances that no longer exist
            const freshIds = new Set(fresh.map(f => f.id));
            const removed = this.state.instances.filter(i => !freshIds.has(i.id));
            if (removed.length > 0) {
                this.state.instances = this.state.instances.filter(i => freshIds.has(i.id));
                if (this.state.selectedInstance && !freshIds.has(this.state.selectedInstance.id)) {
                    this.state.selectedInstance = this.state.instances[0] || null;
                }
            }
        } catch (e) { /* ignore polling errors */ }
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
                        "ssl_status",
                        "ssl_last_error",
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
                // Restore last-selected instance from localStorage if available
                let restored = null;
                try {
                    const savedId = parseInt(localStorage.getItem(
                        'pmb.selectedInstance.' + this.state.currentProjectId
                    ));
                    if (savedId) restored = instances.find(i => i.id === savedId);
                } catch (e) { /* ignore */ }
                const production = instances.find(
                    (i) => i.instance_type === "production"
                );
                this._selectInstance(restored || production || instances[0]);
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
        try {
            const pid = this.state.currentProjectId || 'default';
            localStorage.setItem('pmb.selectedInstance.' + pid, instance.id);
        } catch (e) { /* ignore */ }
        // Cleanup terminals when switching instances
        if (this._termConnected || this._term) {
            this._cleanupTerminal();
        }
        this._cleanupShellTerminal();
        this._cleanupAiTerminal();
        this._stopGitPolling();
        // Stop any existing creation polling
        if (this._pollTimer) {
            clearInterval(this._pollTimer);
            this._pollTimer = null;
        }
        this.state.creationLog = '';
        this.state.creationPid = 0;
        this.state.claudeSessions = [];
        this.state.githubConfigured = false;
        this.state.githubUser = '';
        this.state.githubToken = '';
        this.state.githubError = '';
        this.state.gitSelectedRepo = '';
        this.state.gitStaged = [];
        this.state.gitUnstaged = [];
        this.state.gitUntracked = [];
        this.state.gitOutgoing = [];
        this.state.historyRepos = [];
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
            this.state.activeContentTab = "ai";
            // Initialize AI tab: terminal + git status + history + polling
            setTimeout(async () => {
                await this._checkGitAuth();
                await this._refreshGitStatus();
                this._startGitPolling();
                this._loadClaudeSessions();
                this._loadProjectTasks();
                if (!this.state.commits || this.state.commits.length === 0) {
                    await this._loadHistoryRepos();
                    await this._loadHistory();
                }
                const canTerminal = this.state.isAdmin || this.state.isDeveloper ||
                    (instance.instance_type !== 'production');
                if (canTerminal && instance.state === 'running') {
                    this._initAiTerminal();
                }
            }, 500);
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
        this.state.activeContentTab = "ai";

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
                project_id: this.state.currentProjectId || null,
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
            id: null, name: '', domain: '', subdomain_base: '', repo_path: '', enterprise_path: '',
            database_name: '', connection_type: 'local', ssh_host: '', ssh_user: 'root',
            ssh_port: 22, ssh_key_path: '', ssh_key_configured: false,
            max_staging: 3, max_development: 5, auto_destroy_hours: 24,
            production_branch: 'main',
        };
        this.state.sshPublicKey = '';
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
        // Reset modules panel state so the previous project's instance/path
        // doesn't leak into the new project's view.
        this.state.modulesInstanceId = null;
        this.state.modulesData = null;
        this.state.modulesFilter = '';
        this.state.modulesRepoPath = '';
        this.state.moduleActionOutput = null;
        // Re-check admin/developer role for this project
        try {
            const authCheck = await rpc('/devops/git/auth/check', { project_id: val || null });
            this.state.isAdmin = authCheck.is_admin || false;
            this.state.isDeveloper = authCheck.is_developer || false;
        } catch (e) {}
        await this._loadProjectData();
        // Reload current nav tab data
        const tab = this.state.activeNavTab;
        if (tab === 'settings') {
            await this._loadSettings();
            await this._loadMembers();
            await this._loadAvailableUsers();
            this._loadAgents();
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
            const canTerminal = this.state.isAdmin || this.state.isDeveloper ||
                (this.state.selectedInstance && this.state.selectedInstance.instance_type !== 'production');
            if (canTerminal && this.state.selectedInstance && this.state.selectedInstance.state === 'running') {
                setTimeout(() => this._initAiTerminal(), 300);
            }
        }
        if (tab === 'branches' && this.state.activeContentTab === 'shell') {
            setTimeout(() => this._initShellTerminal(), 300);
        }
        if (tab === 'settings') {
            await this._loadSettings();
            this._loadAgents();
        } else if (tab === 'status') {
            await this._loadMetrics();
            await this._loadDashboard();
        } else if (tab === 'builds') {
            await this._loadAllBuilds();
        } else if (tab === 'modules') {
            if (!this.state.modulesInstanceId && this.state.instances?.length) {
                const prod = this.state.instances.find(
                    (i) => i.instance_type === 'production');
                this.state.modulesInstanceId = (prod || this.state.instances[0]).id;
            }
            if (this.state.modulesInstanceId) {
                await this._loadModules();
            }
        }
    }

    async _onModulesInstanceChange(ev) {
        const v = ev.target.value;
        this.state.modulesInstanceId = v ? parseInt(v, 10) : null;
        this.state.modulesData = null;
        if (this.state.modulesInstanceId) {
            await this._loadModules();
        }
    }

    async _reloadModules() {
        if (!this.state.modulesInstanceId) return;
        await this._loadModules();
    }

    async _loadModules() {
        this.state.modulesLoading = true;
        this.state.modulesData = null;
        try {
            const r = await rpc('/devops/instance/modules', {
                instance_id: this.state.modulesInstanceId,
            });
            this.state.modulesData = r || {};
        } catch (e) {
            this.state.modulesData = { error: String(e.message || e) };
        } finally {
            this.state.modulesLoading = false;
        }
    }

    _filteredModules(repo) {
        const q = (this.state.modulesFilter || '').trim().toLowerCase();
        if (!q) return repo.modules;
        return repo.modules.filter((m) =>
            m.technical_name.toLowerCase().includes(q)
            || (m.name || '').toLowerCase().includes(q));
    }

    _filteredRepos() {
        const repos = this.state.modulesData?.repos || [];
        const sel = (this.state.modulesRepoPath || '').trim();
        if (!sel) return repos;
        return repos.filter((r) => r.path === sel);
    }

    _onModulesRepoChange(ev) {
        this.state.modulesRepoPath = ev.target.value || '';
    }

    async _runModuleAction(mod, action) {
        const verb = action === 'install' ? 'instalar'
            : action === 'uninstall' ? 'desinstalar' : 'actualizar';
        if (!confirm(`¿${verb[0].toUpperCase() + verb.slice(1)} el módulo "${mod.technical_name}"? El servicio se reiniciará.`)) {
            return;
        }
        this.state.moduleActionRunning = true;
        this.state.moduleActionOutput = {
            action, module: mod.technical_name,
            status: 'running', output: 'Programando operación…',
        };
        try {
            const r = await rpc('/devops/instance/module_action', {
                instance_id: this.state.modulesInstanceId,
                module_name: mod.technical_name,
                action,
            });
            if (r.error) {
                this.state.moduleActionOutput = {
                    action, module: mod.technical_name, status: 'error',
                    output: r.error + (r.detail ? '\n' + r.detail : ''),
                };
                this.state.moduleActionRunning = false;
                return;
            }
            // New async flow: the helper unit runs outside the target
            // service's cgroup. The service is stopped & restarted during
            // the op; we poll the status endpoint until rc is reported.
            this.state.moduleActionOutput = {
                action, module: mod.technical_name, status: 'running',
                output: 'Operación en progreso (el servicio se reiniciará)…',
            };
            const opId = r.op_id;
            // Brief wait so the service has time to go down.
            await new Promise((res) => setTimeout(res, 4000));
            const deadline = Date.now() + 10 * 60 * 1000;  // 10 min cap
            let finalStatus = null;
            while (Date.now() < deadline) {
                try {
                    const st = await rpc('/devops/instance/module_action_status', {
                        instance_id: this.state.modulesInstanceId, op_id: opId,
                    });
                    if (st.status === 'ok' || st.status === 'error') {
                        finalStatus = st;
                        break;
                    }
                } catch (e) { /* service may still be down */ }
                await new Promise((res) => setTimeout(res, 3000));
            }
            if (!finalStatus) {
                this.state.moduleActionOutput = {
                    action, module: mod.technical_name, status: 'error',
                    output: 'Timeout esperando resultado de la operación',
                };
            } else {
                this.state.moduleActionOutput = {
                    action, module: mod.technical_name,
                    status: finalStatus.status,
                    output: finalStatus.output || '',
                };
                if (finalStatus.status === 'ok') {
                    await this._loadModules();
                }
            }
        } catch (e) {
            this.state.moduleActionOutput = {
                action, module: mod.technical_name, status: 'error',
                output: String(e.message || e),
            };
        } finally {
            this.state.moduleActionRunning = false;
        }
    }

    _filteredProjectTasks() {
        const tasks = this.state.projectTasks || [];
        const f = this.state.taskStatusFilter || 'all';
        if (f === 'done') return tasks.filter((t) => t.done);
        if (f === 'in_progress') return tasks.filter((t) => !t.done);
        return tasks;
    }

    async _navigateToTasksFromStatus(filter) {
        // Open the tasks panel inline within the Status tab (no nav redirect).
        this.state.taskStatusFilter = filter || 'all';
        this.state.statusTasksOverlayOpen = true;
        if (!this.state.selectedInstance && this.state.instances && this.state.instances.length) {
            const prod = this.state.instances.find((i) => i.instance_type === 'production');
            this._selectInstance(prod || this.state.instances[0]);
        }
        try { await this._loadProjectTasks(); } catch (e) {}
        if (!this.state.productionUsers || !this.state.productionUsers.length) {
            try { await this._loadProductionUsers(); } catch (e) {}
        }
    }

    _closeStatusTasksOverlay() {
        this.state.statusTasksOverlayOpen = false;
        this.state.taskFullscreenId = null;
        this.state.taskFullscreen = null;
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

        // Stop git polling when leaving AI tab
        if (prevTab === 'ai') {
            this._stopGitPolling();
        }
        // Pause logs polling when leaving logs tab (keep session alive)
        if (prevTab === 'logs') {
            if (this._termPollTimeout) {
                clearTimeout(this._termPollTimeout);
                this._termPollTimeout = null;
            }
        }

        this.state.activeContentTab = tab;

        if (tab === 'ai') {
            await this._checkGitAuth();
            await this._refreshGitStatus();
            this._startGitPolling();
            // Load history for inline commits panel
            if (!this.state.commits || this.state.commits.length === 0) {
                await this._loadHistoryRepos();
                await this._loadHistory();
            }
            this._loadClaudeSessions();
            this._loadProjectTasks();
            // Only start terminal if instance is running AND user has write access
            const canTerminal = this.state.isAdmin || this.state.isDeveloper ||
                (this.state.selectedInstance && this.state.selectedInstance.instance_type !== 'production');
            if (canTerminal && this.state.selectedInstance && this.state.selectedInstance.state === 'running') {
                setTimeout(() => this._initAiTerminal(), 200);
            }
        } else if (tab === 'shell') {
            // Shell uses WebSocket (same as AI terminal)
            setTimeout(() => this._initShellTerminal(), 200);
        } else if (tab === 'editor') {
            await this._browseDir('');
        } else if (tab === 'upgrade') {
            await this._loadUpgradeRepos();
        } else if (tab === 'backups') {
            await this._loadBackups();
        } else if (tab === 'meet') {
            await this._loadMeetings();
        } else if (tab === 'logs') {
            const sessionType = this.state.logType === 'odoo' ? 'odoo_log' : 'logs';
            if (this._termConnected && this._terminalType === sessionType && this._term) {
                if (!this._termPollTimeout) this._pollTerminal();
                setTimeout(() => { if (this._fitAddon) this._fitAddon.fit(); }, 100);
            } else {
                setTimeout(async () => {
                    if (this.state.activeContentTab === 'logs' && !this._termInitializing) {
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
        if (!this.state.isAdmin && !this.state.isDeveloper) return;
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
                        this.state.activeContentTab = 'ai';
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

    async _obtainSslCert(instanceId) {
        if (!instanceId) return;
        if (!confirm('Emitir/renovar certificado Let\'s Encrypt para esta '
                     + 'instancia? Se verificará DNS antes de contactar a '
                     + 'certbot.')) return;
        this.state.sslIssuing = true;
        try {
            const res = await rpc('/devops/instance/obtain_ssl',
                                  { instance_id: instanceId });
            if (res.status === 'ok') {
                alert('SSL emitido correctamente para ' + (res.domain || ''));
            } else {
                const detail = res.ssl_last_error || res.error || 'Error desconocido';
                alert('No se pudo emitir SSL:\n\n' + detail);
            }
            await this._loadProjectData();
            if (this.state.selectedInstance) {
                const fresh = this.state.instances.find(
                    i => i.id === this.state.selectedInstance.id);
                if (fresh) this.state.selectedInstance = fresh;
            }
        } catch (e) {
            alert('Error: ' + (e.message || e));
        } finally {
            this.state.sslIssuing = false;
        }
    }

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
        this.state.loadingMessage = 'Eliminando instancia...';

        try {
            const result = await rpc('/devops/instance/destroy', {
                instance_id: inst.id,
            });
            if (result.error) {
                alert(result.error);
            } else {
                this.state.selectedInstance = null;
                this.state.selectedBranch = null;
                await this._loadProjectData();
                // Auto-select production instance if available
                if (this.state.instances.length > 0) {
                    const prod = this.state.instances.find(i => i.instance_type === 'production');
                    this._selectInstance(prod || this.state.instances[0]);
                }
            }
        } catch (err) {
            alert("Error: " + (err.message || err));
        } finally {
            this.state.loading = false;
            this.state.loadingMessage = '';
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

    async _pullHistoryRepo() {
        if (!this.state.historyRepoPath || !this.state.currentProjectId) return;
        try {
            const result = await rpc('/devops/git/pull', {
                project_id: this.state.currentProjectId,
                repo_path: this.state.historyRepoPath,
            });
            if (result.error) {
                if (result.auth_required) {
                    this.state.gitAuthenticated = false;
                    return;
                }
                alert('Pull error: ' + result.error);
            } else {
                await this._loadHistoryRepos();
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

    _onHistoryRepoChange(ev) {
        const path = ev.target.value || '';
        if (path && path !== this.state.historyRepoPath) {
            this.state.historyRepoPath = path;
            this._loadHistory();
        }
    }

    // ------------------------------------------------------------------
    // Project Tasks
    // ------------------------------------------------------------------

    async _loadProjectTasks() {
        if (!this.state.currentProjectId) return;
        try {
            const result = await rpc('/devops/project/tasks', {
                project_id: this.state.currentProjectId,
            });
            const tasks = (result.tasks || []).map(t => ({
                ...t,
                description: t.description ? markup(t.description) : t.description,
            }));
            this.state.projectTasks = tasks;
            this.state.taskStages = result.stages || [];
            this.state.taskMembers = result.members || [];
            this.state.taskMeetings = result.meetings || [];
            this.state.isDevopsAdmin = !!result.is_admin;
            // Refresh fullscreen overlay if open
            if (this.state.taskFullscreenId) {
                const updated = tasks.find(t => t.id === this.state.taskFullscreenId);
                if (updated) {
                    this.state.taskFullscreen = { ...updated };
                } else {
                    this.state.taskFullscreen = null;
                    this.state.taskFullscreenId = null;
                }
            }
        } catch (e) { /* ignore */ }
    }

    // ------------------------------------------------------------------
    // AI Agents
    // ------------------------------------------------------------------
    async _loadAgents() {
        if (!this.state.currentProjectId || !this.state.isAdmin) return;
        try {
            const result = await rpc('/devops/agents', {
                project_id: this.state.currentProjectId,
            });
            this.state.agents = result.agents || [];
        } catch (e) { this.state.agents = []; }
    }

    async _createAgent() {
        const name = this.state.newAgentName.trim();
        if (!name || !this.state.currentProjectId) return;
        try {
            await rpc('/devops/agent/create', {
                project_id: this.state.currentProjectId,
                name: name,
                branch: this.state.newAgentBranch || 'HEAD',
                interval_number: this.state.newAgentInterval || 1,
                interval_type: this.state.newAgentIntervalType || 'days',
                provider: this.state.newAgentProvider || 'copilot',
                copilot_model: this.state.newAgentCopilotModel || 'claude-opus-4.7',
                custom_system_prompt: this.state.newAgentPrompt || '',
                output_file: this.state.newAgentOutputFile || '',
            });
            this.state.newAgentName = '';
            this.state.newAgentPrompt = '';
            this.state.newAgentOutputFile = '';
            this.state.showNewAgent = false;
            await this._loadAgents();
        } catch (e) {
            alert('Error: ' + (e.message || e));
        }
    }

    _onAgentPromptInput(agentId, ev) {
        this.state.agentPromptEdits = {
            ...this.state.agentPromptEdits,
            [agentId]: ev.target.value,
        };
    }

    _onAgentOutputFileInput(agentId, ev) {
        this.state.agentOutputEdits = {
            ...this.state.agentOutputEdits,
            [agentId]: ev.target.value,
        };
    }

    async _saveAgentPrompt(agentId) {
        const text = this.state.agentPromptEdits[agentId];
        const output = this.state.agentOutputEdits[agentId];
        if (text === undefined && output === undefined) return;
        this.state.agentSaveStatus = { ...this.state.agentSaveStatus, [agentId]: 'saving' };
        try {
            const payload = { agent_id: agentId };
            if (text !== undefined) payload.custom_system_prompt = text;
            if (output !== undefined) payload.output_file = output;
            await rpc('/devops/agent/update', payload);
            this.state.agentSaveStatus = { ...this.state.agentSaveStatus, [agentId]: 'saved' };
            await this._loadAgents();
            setTimeout(() => {
                const copy = { ...this.state.agentSaveStatus };
                delete copy[agentId];
                this.state.agentSaveStatus = copy;
            }, 1500);
        } catch (e) {
            this.state.agentSaveStatus = { ...this.state.agentSaveStatus, [agentId]: 'error' };
            alert('Error: ' + (e.message || e));
        }
    }

    async _toggleAgent(agentId) {
        try {
            await rpc('/devops/agent/toggle', { agent_id: agentId });
            await this._loadAgents();
        } catch (e) { /* ignore */ }
    }

    async _deleteAgent(agentId) {
        if (!confirm('Eliminar este agente?')) return;
        try {
            await rpc('/devops/agent/delete', { agent_id: agentId });
            this.state.agentExpandedId = null;
            this.state.agentRuns = [];
            await this._loadAgents();
        } catch (e) { /* ignore */ }
    }

    async _runAgent(agentId) {
        this.state.agentRunning = agentId;
        try {
            const result = await rpc('/devops/agent/run', { agent_id: agentId });
            if (result.error) {
                alert(result.error);
            }
            await this._loadAgents();
            if (this.state.agentExpandedId === agentId) {
                await this._loadAgentRuns(agentId);
            }
        } catch (e) {
            alert('Error: ' + (e.message || e));
        }
        this.state.agentRunning = null;
    }

    async _toggleAgentExpand(agentId) {
        if (this.state.agentExpandedId === agentId) {
            this.state.agentExpandedId = null;
            this.state.agentRuns = [];
        } else {
            this.state.agentExpandedId = agentId;
            await this._loadAgentRuns(agentId);
        }
    }

    async _loadAgentRuns(agentId) {
        try {
            const result = await rpc('/devops/agent/runs', { agent_id: agentId });
            this.state.agentRuns = result.runs || [];
        } catch (e) { this.state.agentRuns = []; }
    }

    async _loadProductionUsers() {
        if (!this.state.currentProjectId) return;
        try {
            const result = await rpc('/devops/project/production_users', {
                project_id: this.state.currentProjectId,
            });
            this.state.productionUsers = result.users || [];
        } catch (e) { /* ignore */ }
    }

    async _createTask() {
        const name = this.state.newTaskName.trim();
        if (!name || !this.state.currentProjectId) return;
        try {
            await rpc('/devops/project/task/create', {
                project_id: this.state.currentProjectId,
                name: name,
            });
            this.state.newTaskName = '';
            this.state.showNewTask = false;
            await this._loadProjectTasks();
        } catch (e) {
            alert('Error: ' + (e.message || e));
        }
    }

    async _assignTaskDev(taskId, userIdRaw) {
        const userId = parseInt(userIdRaw) || false;
        try {
            await rpc('/devops/project/task/assign', {
                task_id: taskId,
                dev_user_id: userId,
            });
            await this._loadProjectTasks();
        } catch (e) { /* ignore */ }
    }

    async _assignTaskClient(taskId, clientName) {
        try {
            await rpc('/devops/project/task/assign', {
                task_id: taskId,
                client_name: clientName || '',
            });
            await this._loadProjectTasks();
        } catch (e) { /* ignore */ }
    }

    async _updateTaskStage(taskId, stageIdRaw) {
        const stageId = parseInt(stageIdRaw);
        if (!stageId) return;
        try {
            await rpc('/devops/project/task/update', {
                task_id: taskId,
                stage_id: stageId,
            });
            await this._loadProjectTasks();
        } catch (e) { /* ignore */ }
    }

    async _updateTaskPriority(taskId, priority) {
        try {
            await rpc('/devops/project/task/update', {
                task_id: taskId,
                priority: String(priority),
            });
            await this._loadProjectTasks();
        } catch (e) { /* ignore */ }
    }

    async _updateTaskDeadline(taskId, deadline) {
        try {
            await rpc('/devops/project/task/update', {
                task_id: taskId,
                deadline: deadline || '',
            });
            await this._loadProjectTasks();
        } catch (e) { /* ignore */ }
    }

    _openTaskInOdoo(taskId) {
        // Open the task in Odoo's standard project.task form view in a new tab
        const proj = this.state.currentProject;
        const odooProjectId = proj && proj.odoo_project_id ? (Array.isArray(proj.odoo_project_id) ? proj.odoo_project_id[0] : proj.odoo_project_id) : null;
        const url = odooProjectId
            ? `/odoo/project/${odooProjectId}/tasks/${taskId}`
            : `/web#id=${taskId}&model=project.task&view_type=form`;
        window.open(url, '_blank');
    }

    _sendTaskToAi(task) {
        if (!this._aiWs || this._aiWs.readyState !== WebSocket.OPEN) return;
        const desc = task.description ? task.description.replace(/<[^>]*>/g, '').trim().substring(0, 800) : '';
        const project = this.state.currentProject;
        const repo = this.state.historyRepos.find(r => r.path === this.state.gitSelectedRepo);
        const stageMap = {
            'Levantamiento': 'Analiza los requerimientos de esta tarea, identifica los archivos relevantes y propón un plan de implementación paso a paso.',
            'Development': 'Implementa esta tarea. Busca los archivos relevantes, haz los cambios necesarios y muestra un resumen de lo que modificaste.',
            'Staging': 'Revisa que esta tarea esté correctamente implementada. Busca posibles bugs, valida la lógica y sugiere mejoras si las hay.',
            'Producción': 'Verifica que esta tarea funcione correctamente en producción. Revisa logs recientes y confirma que no hay errores relacionados.',
        };
        const action = stageMap[task.stage] || 'Analiza esta tarea y sugiere cómo implementarla. Identifica archivos relevantes y propón los cambios.';
        const lines = [
            `## Tarea: ${task.name}`,
            desc ? `Descripción: ${desc}` : '',
            `Etapa: ${task.stage || 'Sin etapa'}`,
            task.dev_assignees.length ? `Dev: ${task.dev_assignees.map(a => a.name).join(', ')}` : '',
            task.client_name ? `Cliente: ${task.client_name}` : '',
            repo ? `Repo: ${repo.path} (${repo.branch})` : '',
            '',
            action,
        ].filter(Boolean).join('\n');
        this._aiWs.send(JSON.stringify({ type: 'input', data: lines + '\n' }));
        this._setAiLastPrompt(lines);
    }

    _instanceUrlWithDebug(url) {
        if (!url) return '';
        let u;
        try {
            u = new URL(url);
        } catch (e) {
            // Not a parseable URL — fall back to naive append
            if (url.includes('debug=assets')) return url;
            const sep = url.includes('?') ? '&' : '?';
            return url + sep + 'debug=assets';
        }
        // Ensure the path lands on /odoo so the SPA opens (instead of the bare
        // hostname which redirects through /web and may drop the debug flag).
        const path = u.pathname.replace(/\/+$/, '');
        if (!path || path === '' || path === '/') {
            u.pathname = '/odoo';
        }
        u.searchParams.set('debug', 'assets');
        return u.toString();
    }

    _aiLastPromptKey(instanceId) {
        const id = instanceId
            || (this.state.selectedInstance && this.state.selectedInstance.id)
            || 'default';
        return 'pmb.aiLastPrompt.' + id;
    }

    _aiPromptHistoryKey(instanceId) {
        const id = instanceId
            || (this.state.selectedInstance && this.state.selectedInstance.id)
            || 'default';
        return 'pmb.aiPromptHistory.' + id;
    }

    _setAiLastPrompt(text) {
        if (!text) return;
        const clean = String(text).replace(/\s+/g, ' ').trim();
        if (!clean) return;
        this.state.aiLastPrompt = clean;
        try { localStorage.setItem(this._aiLastPromptKey(), clean); }
        catch (e) { /* ignore quota / private mode */ }
        // Prepend to history (dedup, cap at 30)
        const history = (this.state.aiPromptHistory || [])
            .filter((p) => p !== clean);
        history.unshift(clean);
        this.state.aiPromptHistory = history.slice(0, 30);
        try {
            localStorage.setItem(this._aiPromptHistoryKey(),
                JSON.stringify(this.state.aiPromptHistory));
        } catch (e) { /* ignore */ }
    }

    _loadAiLastPromptForInstance(instanceId) {
        try {
            const saved = localStorage.getItem(this._aiLastPromptKey(instanceId));
            this.state.aiLastPrompt = saved || '';
        } catch (e) {
            this.state.aiLastPrompt = '';
        }
        try {
            const raw = localStorage.getItem(
                this._aiPromptHistoryKey(instanceId));
            this.state.aiPromptHistory = raw ? JSON.parse(raw) : [];
        } catch (e) {
            this.state.aiPromptHistory = [];
        }
        this.state.aiPromptHistoryOpen = false;
    }

    _clearAiLastPrompt() {
        this.state.aiLastPrompt = '';
        try { localStorage.removeItem(this._aiLastPromptKey()); }
        catch (e) { /* ignore */ }
    }

    _toggleAiPromptHistory() {
        this.state.aiPromptHistoryOpen = !this.state.aiPromptHistoryOpen;
    }

    _clearAiPromptHistory() {
        this.state.aiPromptHistory = [];
        this.state.aiPromptHistoryOpen = false;
        try { localStorage.removeItem(this._aiPromptHistoryKey()); }
        catch (e) { /* ignore */ }
    }

    _resendAiPrompt(prompt) {
        if (!prompt) return;
        // Resend to the AI terminal: close history, update active prompt,
        // and pipe the text through the WebSocket so Claude actually receives it.
        this.state.aiPromptHistoryOpen = false;
        if (this._aiWs && this._aiWs.readyState === WebSocket.OPEN) {
            this._aiWs.send(JSON.stringify({
                type: 'input', data: prompt + '\n',
            }));
        }
        this._setAiLastPrompt(prompt);
        if (this._aiTerm) this._aiTerm.focus();
    }

    _captureTypedInput(data) {
        // Accumulates manual keystrokes into a buffer; on Enter commits as last prompt.
        if (!data) return;
        if (this._aiInputBuffer === undefined) this._aiInputBuffer = '';
        // Ignore ANSI escape sequences (arrows, function keys, etc.)
        if (data.charCodeAt(0) === 0x1b) return;
        for (const ch of data) {
            const code = ch.charCodeAt(0);
            if (code === 0x0d || code === 0x0a) {
                // Enter: commit buffer as last prompt
                if (this._aiInputBuffer.trim()) {
                    this._setAiLastPrompt(this._aiInputBuffer);
                }
                this._aiInputBuffer = '';
            } else if (code === 0x7f || code === 0x08) {
                // Backspace
                this._aiInputBuffer = this._aiInputBuffer.slice(0, -1);
            } else if (code === 0x03) {
                // Ctrl+C: clear buffer
                this._aiInputBuffer = '';
            } else if (code >= 0x20) {
                this._aiInputBuffer += ch;
            }
        }
    }

    async _linkMeetingToTask(taskId, meetingId) {
        if (!meetingId) return;
        try {
            await rpc('/devops/project/task/link_meeting', {
                task_id: taskId,
                meeting_id: parseInt(meetingId),
                action: 'link',
            });
            await this._loadProjectTasks();
        } catch (e) { /* ignore */ }
    }

    async _unlinkMeetingFromTask(taskId, meetingId) {
        try {
            await rpc('/devops/project/task/link_meeting', {
                task_id: taskId,
                meeting_id: meetingId,
                action: 'unlink',
            });
            await this._loadProjectTasks();
        } catch (e) { /* ignore */ }
    }

    async _approveTask(taskId, action) {
        try {
            await rpc('/devops/project/task/approve', { task_id: taskId, action });
            await this._loadProjectTasks();
        } catch (e) { /* ignore */ }
    }

    _toggleTask(taskId) {
        this.state.expandedTaskId = this.state.expandedTaskId === taskId ? null : taskId;
        // Load production users on first expand
        if (this.state.expandedTaskId && this.state.productionUsers.length === 0) {
            this._loadProductionUsers();
        }
    }

    _openTaskFullscreen(taskId) {
        const task = this.state.projectTasks.find(tk => tk.id === taskId);
        if (task) {
            this.state.taskFullscreen = { ...task };
            this.state.taskFullscreenId = taskId;
        }
        if (this.state.productionUsers.length === 0) {
            this._loadProductionUsers();
        }
        this.state.taskLinkedCommits = [];
        this.state.taskCommitPickerOpen = false;
        this._loadTaskLinkedCommits(taskId);
    }

    async _deleteTaskFullscreen(taskId) {
        if (!this.state.isDevopsAdmin) return;
        if (!confirm('¿Eliminar esta tarea? Se borrará también en producción.')) return;
        try {
            const res = await rpc('/devops/project/task/delete', { task_id: taskId });
            if (res && res.error) { alert(res.error); return; }
            this.state.taskFullscreen = null;
            this.state.taskFullscreenId = null;
            await this._loadProjectTasks();
        } catch (e) { alert('Error eliminando tarea: ' + e.message); }
    }

    _startEditTaskDescription(taskId) {
        const task = this.state.projectTasks.find(tk => tk.id === taskId);
        // toString() unwraps Markup back to raw HTML for the textarea
        this.state.taskDescDraft = task && task.description ? task.description.toString() : '';
        this.state.taskDescEditingId = taskId;
    }

    _cancelEditTaskDescription() {
        this.state.taskDescEditingId = null;
        this.state.taskDescDraft = '';
    }

    async _saveTaskDescription(taskId, htmlFromEditor) {
        // When called from the Wysiwyg wrapper, the editor passes the HTML
        // directly. The old textarea flow used state.taskDescDraft instead.
        const draft = htmlFromEditor !== undefined ? htmlFromEditor : this.state.taskDescDraft;
        this.state.taskDescEditingId = null;
        this.state.taskDescDraft = '';
        try {
            const res = await rpc('/devops/project/task/update_description', {
                task_id: taskId, description: draft,
            });
            if (res && res.error) { alert(res.error); return; }
            await this._loadProjectTasks();
        } catch (e) { alert('Error guardando descripción: ' + e.message); }
    }

    _startEditTaskName(taskId) {
        const task = this.state.projectTasks.find(tk => tk.id === taskId);
        this.state.taskNameDraft = task && task.name ? task.name.toString() : '';
        this.state.taskNameEditingId = taskId;
    }

    _cancelEditTaskName() {
        this.state.taskNameEditingId = null;
        this.state.taskNameDraft = '';
    }

    _onTaskNameKeydown(ev) {
        if (ev.key === 'Enter') {
            ev.preventDefault();
            this._saveTaskName(this.state.taskFullscreenId);
        } else if (ev.key === 'Escape') {
            this._cancelEditTaskName();
        }
    }

    async _saveTaskName(taskId) {
        const draft = (this.state.taskNameDraft || '').trim();
        if (!draft) { alert('El título no puede estar vacío'); return; }
        this.state.taskNameEditingId = null;
        this.state.taskNameDraft = '';
        try {
            const res = await rpc('/devops/project/task/update', {
                task_id: taskId, name: draft,
            });
            if (res && res.error) { alert(res.error); return; }
            if (this.state.taskFullscreen && this.state.taskFullscreenId === taskId) {
                this.state.taskFullscreen = { ...this.state.taskFullscreen, name: draft };
            }
            await this._loadProjectTasks();
        } catch (e) { alert('Error guardando título: ' + e.message); }
    }

    // ---- Commit ↔ task linking ----------------------------------------

    async _loadTaskLinkedCommits(taskId) {
        if (!taskId) { this.state.taskLinkedCommits = []; return; }
        try {
            const res = await rpc('/devops/task/commits', {
                task_id: taskId,
                project_id: this.state.currentProjectId,
            });
            this.state.taskLinkedCommits = res.commits || [];
        } catch (e) {
            this.state.taskLinkedCommits = [];
        }
    }

    _toggleCommitPickerForTask() {
        this.state.taskCommitPickerOpen = !this.state.taskCommitPickerOpen;
        this.state.taskCommitPickerSearch = '';
        if (this.state.taskCommitPickerOpen
            && (!this.state.commits || this.state.commits.length === 0)) {
            this._loadHistoryRepos().then(() => this._loadHistory());
        }
    }

    _onTaskCommitPickerSearch(ev) {
        this.state.taskCommitPickerSearch = (ev.target.value || '').toLowerCase();
    }

    async _linkCommitToOpenTask(commit) {
        const taskId = this.state.taskFullscreenId;
        if (!taskId || !this.state.currentProjectId) return;
        try {
            const res = await rpc('/devops/commit/link', {
                project_id: this.state.currentProjectId,
                task_id: taskId,
                repo_path: this.state.historyRepoPath || '',
                commit_hash: commit.full_hash || commit.short_hash,
                short_hash: commit.short_hash || '',
                message: commit.message || '',
                author: commit.author || '',
                date: commit.date || '',
            });
            if (res && res.error) { alert(res.error); return; }
            await this._loadTaskLinkedCommits(taskId);
            this.state.taskCommitPickerOpen = false;
        } catch (e) { alert('Error enlazando commit: ' + e.message); }
    }

    async _unlinkCommitFromOpenTask(linkId) {
        if (!linkId) return;
        if (!confirm('¿Quitar este commit de la tarea?')) return;
        try {
            const res = await rpc('/devops/commit/unlink', { link_id: linkId });
            if (res && res.error) { alert(res.error); return; }
            await this._loadTaskLinkedCommits(this.state.taskFullscreenId);
        } catch (e) { alert('Error: ' + e.message); }
    }

    async _loadCommitLinkedTasks(commit) {
        const hash = commit.full_hash || commit.short_hash;
        if (!hash || !this.state.currentProjectId) return;
        try {
            const res = await rpc('/devops/commit/tasks', {
                project_id: this.state.currentProjectId,
                commit_hash: hash,
                repo_path: this.state.historyRepoPath || '',
            });
            this.state.commitLinkedTasks = {
                ...this.state.commitLinkedTasks,
                [hash]: res.tasks || [],
            };
        } catch (e) {
            this.state.commitLinkedTasks = {
                ...this.state.commitLinkedTasks, [hash]: [],
            };
        }
    }

    _toggleTaskPickerForCommit(commit) {
        const hash = commit.full_hash || commit.short_hash;
        this.state.commitTaskPickerFor =
            this.state.commitTaskPickerFor === hash ? null : hash;
        this.state.commitTaskPickerSearch = '';
    }

    _onCommitTaskPickerSearch(ev) {
        this.state.commitTaskPickerSearch = (ev.target.value || '').toLowerCase();
    }

    async _linkTaskToCommit(commit, taskId) {
        if (!this.state.currentProjectId || !taskId) return;
        try {
            const res = await rpc('/devops/commit/link', {
                project_id: this.state.currentProjectId,
                task_id: taskId,
                repo_path: this.state.historyRepoPath || '',
                commit_hash: commit.full_hash || commit.short_hash,
                short_hash: commit.short_hash || '',
                message: commit.message || '',
                author: commit.author || '',
                date: commit.date || '',
            });
            if (res && res.error) { alert(res.error); return; }
            await this._loadCommitLinkedTasks(commit);
            this.state.commitTaskPickerFor = null;
            // Refresh task panel if it's for the same task
            if (this.state.taskFullscreenId === taskId) {
                await this._loadTaskLinkedCommits(taskId);
            }
        } catch (e) { alert('Error enlazando tarea: ' + e.message); }
    }

    async _unlinkTaskFromCommit(commit, linkId, taskId) {
        if (!linkId) return;
        if (!confirm('¿Quitar esta tarea del commit?')) return;
        try {
            const res = await rpc('/devops/commit/unlink', { link_id: linkId });
            if (res && res.error) { alert(res.error); return; }
            await this._loadCommitLinkedTasks(commit);
            if (this.state.taskFullscreenId === taskId) {
                await this._loadTaskLinkedCommits(taskId);
            }
        } catch (e) { alert('Error: ' + e.message); }
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

    async _openHistoryFullscreen() {
        this.state.historyFullscreen = true;
        if (!this.state.commits || this.state.commits.length === 0) {
            await this._loadHistoryRepos();
            await this._loadHistory();
        }
    }

    async _commitReviewCreate(commit) {
        if (!this.state.currentProjectId) return;
        const repo = this.state.historyRepos.find(r => r.path === this.state.historyRepoPath);
        const branch = repo ? repo.branch : '';
        try {
            const result = await rpc('/devops/commit/review', {
                project_id: this.state.currentProjectId,
                commit_hash: commit.full_hash || commit.short_hash,
                commit_message: commit.message,
                branch: branch,
                action: 'create',
            });
            if (result.task_id) {
                commit._reviewId = result.task_id;
            } else if (result.error) {
                alert(result.error);
            }
        } catch (e) { alert('Error: ' + e.message); }
    }

    async _commitReviewDone(commit) {
        if (!this.state.currentProjectId) return;
        try {
            const result = await rpc('/devops/commit/review', {
                project_id: this.state.currentProjectId,
                commit_hash: commit.full_hash || commit.short_hash,
                commit_message: commit.message,
                action: 'done',
            });
            if (result.completed) {
                commit._reviewDone = true;
            } else if (result.error) {
                alert(result.error);
            }
        } catch (e) { alert('Error: ' + e.message); }
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
        this._loadCommitLinkedTasks(commit);
    }

    // ------------------------------------------------------------------
    // Terminal (xterm.js)
    // ------------------------------------------------------------------

    // ------------------------------------------------------------------
    // Shell terminal (WebSocket) — same tech as AI terminal
    // ------------------------------------------------------------------

    async _initShellTerminal() {
        if (this._shellTermInitializing) return;
        this._shellTermInitializing = true;

        if (!this._shellScrollback) this._shellScrollback = [];

        try {
            // Dispose old xterm (DOM may have been destroyed by tab switch)
            if (this._shellResizeObserver) { this._shellResizeObserver.disconnect(); this._shellResizeObserver = null; }
            if (this._shellTerm) { this._shellTerm.dispose(); this._shellTerm = null; }
            this._shellFitAddon = null;

            // Load xterm.js from CDN if not loaded
            if (!window.Terminal) {
                await this._loadScript('https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js');
                await this._loadCSS('https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css');
                await this._loadScript('https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js');
            }

            const container = this.__owl__.refs.terminalShell || null;
            if (!container) return;

            this._shellTerm = new window.Terminal({
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
            this._shellFitAddon = new window.FitAddon.FitAddon();
            this._shellTerm.loadAddon(this._shellFitAddon);
            this._shellTerm.open(container);
            this._shellFitAddon.fit();

            // Prevent horizontal scroll from propagating to page
            container.addEventListener('wheel', (ev) => {
                if (Math.abs(ev.deltaX) > 0) ev.preventDefault();
            }, { passive: false });

            // Re-fit on container resize
            this._shellResizeObserver = new ResizeObserver(() => {
                if (this._shellFitAddon) { try { this._shellFitAddon.fit(); } catch (e) {} }
            });
            this._shellResizeObserver.observe(container);

            // Forward keyboard input to WebSocket
            this._shellTerm.onData((data) => {
                if (this._shellWs && this._shellWs.readyState === WebSocket.OPEN) {
                    this._shellWs.send(JSON.stringify({ type: 'input', data: data }));
                }
            });

            // If WebSocket already connected, replay client scrollback and reattach
            if (this._shellWs && this._shellWs.readyState === WebSocket.OPEN) {
                if (this._shellScrollback && this._shellScrollback.length > 0) {
                    for (const chunk of this._shellScrollback) {
                        this._shellTerm.write(chunk);
                    }
                }
                this._shellWs.onmessage = (event) => this._onShellWsMessage(event);
                const dims = this._shellFitAddon.proposeDimensions();
                if (dims) {
                    this._shellWs.send(JSON.stringify({ type: 'resize', rows: dims.rows, cols: dims.cols }));
                }
                return;
            }

            // New WebSocket connection
            this._shellTerm.writeln('\x1b[33mConectando terminal...\x1b[0m');

            const tokenResult = await rpc('/devops/ai/token', {
                project_id: this.state.currentProjectId || null,
                instance_id: this.state.selectedInstance ? this.state.selectedInstance.id : null,
                cmd_type: 'shell',
            });
            if (tokenResult.error) {
                this._shellTerm.writeln('\x1b[31mError: ' + tokenResult.error + '\x1b[0m');
                return;
            }

            const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${proto}//${location.host}${tokenResult.ws_url}`;
            this._shellWs = new WebSocket(wsUrl);

            this._shellWs.onopen = () => {
                this._shellWs.send(JSON.stringify({ token: tokenResult.token }));
            };
            this._shellWs.onmessage = (event) => this._onShellWsMessage(event);
            this._shellWs.onclose = () => {
                this.state.terminalConnected = false;
                if (this._shellTerm) this._shellTerm.writeln('\r\n\x1b[31m[Sesion terminada]\x1b[0m');
            };
            this._shellWs.onerror = () => {
                this.state.terminalConnected = false;
                if (this._shellTerm) this._shellTerm.writeln('\r\n\x1b[31m[Error de conexion WebSocket]\x1b[0m');
            };

        } catch (e) {
            if (this._shellTerm) this._shellTerm.writeln('\x1b[31mError: ' + (e.message || e) + '\x1b[0m');
        } finally {
            this._shellTermInitializing = false;
        }
    }

    _onShellWsMessage(event) {
        if (!this._shellTerm) return;
        if (typeof event.data === 'string') {
            try {
                const msg = JSON.parse(event.data);
                if (msg.type === 'ready') {
                    this.state.terminalConnected = true;
                    if (msg.reattached) {
                        this._shellTerm.writeln('\x1b[32mReconectado a sesion existente\x1b[0m\r\n');
                    } else {
                        this._shellTerm.writeln('\x1b[32mTerminal conectada\x1b[0m\r\n');
                    }
                    const dims = this._shellFitAddon ? this._shellFitAddon.proposeDimensions() : null;
                    if (dims && this._shellWs) {
                        this._shellWs.send(JSON.stringify({ type: 'resize', rows: dims.rows, cols: dims.cols }));
                    }
                    return;
                }
                if (msg.type === 'error') {
                    this._shellTerm.writeln('\x1b[31mError: ' + msg.data + '\x1b[0m');
                    return;
                }
                if (msg.type === 'info') {
                    this._shellTerm.writeln('\x1b[32m' + msg.data + '\x1b[0m');
                    return;
                }
            } catch (e) {}
        }
        // Write to terminal and save to client scrollback
        let chunk = event.data;
        if (event.data instanceof ArrayBuffer) {
            chunk = new Uint8Array(event.data);
            this._shellTerm.write(chunk);
        } else if (event.data instanceof Blob) {
            event.data.arrayBuffer().then(buf => {
                const u8 = new Uint8Array(buf);
                if (this._shellTerm) this._shellTerm.write(u8);
                if (this._shellScrollback) this._shellScrollback.push(u8);
            });
            return;
        } else {
            this._shellTerm.write(chunk);
        }
        if (this._shellScrollback) {
            this._shellScrollback.push(chunk);
            if (this._shellScrollback.length > 200) {
                this._shellScrollback = this._shellScrollback.slice(-150);
            }
        }
    }

    _cleanupShellTerminal() {
        if (this._shellResizeObserver) { this._shellResizeObserver.disconnect(); this._shellResizeObserver = null; }
        if (this._shellWs) { this._shellWs.close(); this._shellWs = null; }
        if (this._shellTerm) { this._shellTerm.dispose(); this._shellTerm = null; }
        this._shellFitAddon = null;
        this._shellScrollback = null;
        this.state.terminalConnected = false;
    }

    // ------------------------------------------------------------------
    // Logs terminal (HTTP polling — kept separate from shell)
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

            const container = this._getLogsTerminalContainer();
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
        try { if (this._shellFitAddon) this._shellFitAddon.fit(); } catch (e) {}
        // Send resize to AI WebSocket
        if (this._aiWs && this._aiWs.readyState === WebSocket.OPEN && this._aiFitAddon) {
            const dims = this._aiFitAddon.proposeDimensions();
            if (dims) {
                this._aiWs.send(JSON.stringify({ type: 'resize', rows: dims.rows, cols: dims.cols }));
            }
        }
        // Send resize to Shell WebSocket
        if (this._shellWs && this._shellWs.readyState === WebSocket.OPEN && this._shellFitAddon) {
            const dims = this._shellFitAddon.proposeDimensions();
            if (dims) {
                this._shellWs.send(JSON.stringify({ type: 'resize', rows: dims.rows, cols: dims.cols }));
            }
        }
    }

    _getLogsTerminalContainer() {
        return this.__owl__.refs.terminalLogs || null;
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

    _setDevOpsFavicon() {
        // Save original favicon and set DevOps ⚡ icon
        const existing = document.querySelector('link[rel="icon"], link[rel="shortcut icon"]');
        if (existing) {
            this._originalFavicon = existing.href;
        }
        let link = document.querySelector('link[rel="icon"]');
        if (!link) {
            link = document.createElement('link');
            link.rel = 'icon';
            document.head.appendChild(link);
        }
        link.href = '/pmb_devops/static/src/img/favicon.svg';
        link.type = 'image/svg+xml';
        // Also set page title
        this._originalTitle = document.title;
        document.title = '⚡ PMB DevOps';
    }

    _restoreFavicon() {
        const link = document.querySelector('link[rel="icon"]');
        if (link && this._originalFavicon) {
            link.href = this._originalFavicon;
        }
        if (this._originalTitle) {
            document.title = this._originalTitle;
        }
    }

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

    _startGitPolling() {
        this._stopGitPolling();
        this._gitPollTimer = setInterval(() => {
            if (this.state.activeContentTab === 'ai' && !this.state.gitCommitting && !this.state.gitPushing) {
                this._refreshGitStatus();
                // Only reload history if user is NOT viewing details (diff, fullscreen)
                if (!this.state.gitDiffFile && !this.state.historyFullscreen) {
                    this._loadHistory();
                }
            }
        }, 10000); // every 10 seconds
    }

    _stopGitPolling() {
        if (this._gitPollTimer) {
            clearInterval(this._gitPollTimer);
            this._gitPollTimer = null;
        }
    }

    async _refreshGitStatus() {
        if (!this.state.currentProjectId) return;
        if (this.state.historyRepos.length === 0) {
            await this._loadHistoryRepos();
        }
        // Auto-select repo: prefer custom repos (where dev changes are)
        if (!this.state.gitSelectedRepo && this.state.historyRepos.length > 0) {
            const custom = this.state.historyRepos.find(r => r.repo_type === 'custom');
            this.state.gitSelectedRepo = custom ? custom.path : this.state.historyRepos[0].path;
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

    _onSplitResizeStart(ev) {
        ev.preventDefault();
        const panel = ev.currentTarget.closest('.pmb-git-panel');
        if (!panel) return;
        const panelRect = panel.getBoundingClientRect();
        const startY = ev.type === 'touchstart' ? ev.touches[0].clientY : ev.clientY;
        const startPercent = this.state.gitSplitPercent;

        const onMove = (e) => {
            const clientY = e.type === 'touchmove' ? e.touches[0].clientY : e.clientY;
            const deltaY = clientY - startY;
            const deltaPercent = (deltaY / panelRect.height) * 100;
            this.state.gitSplitPercent = Math.max(20, Math.min(80, startPercent + deltaPercent));
        };
        const onEnd = () => {
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onEnd);
            document.removeEventListener('touchmove', onMove);
            document.removeEventListener('touchend', onEnd);
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
        // Also check GitHub credentials for this instance
        await this._checkGithubAuth();
    }

    async _checkGithubAuth() {
        if (!this.state.selectedInstance) return;
        if (this.state.selectedInstance.instance_type === 'production') {
            this.state.githubConfigured = true; // production uses repo's existing creds
            return;
        }
        try {
            const result = await rpc('/devops/git/github/check', {
                instance_id: this.state.selectedInstance.id,
            });
            this.state.githubConfigured = result.configured || false;
            if (result.github_user) this.state.githubUser = result.github_user;
        } catch (e) { /* ignore */ }
    }

    _onGithubUserInput(ev) { this.state.githubUser = ev.target.value; }
    _onGithubTokenInput(ev) { this.state.githubToken = ev.target.value; }
    _onGithubKeyup(ev) { if (ev.key === 'Enter') this._githubLogin(); }

    async _githubLogin() {
        if (!this.state.githubUser || !this.state.githubToken || !this.state.selectedInstance) return;
        this.state.githubLoading = true;
        this.state.githubError = '';
        try {
            const result = await rpc('/devops/git/github/save', {
                instance_id: this.state.selectedInstance.id,
                github_user: this.state.githubUser,
                github_token: this.state.githubToken,
            });
            if (result.error) {
                this.state.githubError = result.error;
            } else {
                this.state.githubConfigured = true;
                this.state.githubToken = '';
                this.state.githubError = '';
            }
        } catch (e) {
            this.state.githubError = 'Error de conexion';
        }
        this.state.githubLoading = false;
    }

    async _githubOAuthLogin() {
        if (!this.state.selectedInstance) return;
        this.state.githubLoading = true;
        this.state.githubError = '';
        try {
            const result = await rpc('/devops/git/github/oauth/start', {
                instance_id: this.state.selectedInstance.id,
            });
            if (result.error) {
                this.state.githubError = result.error;
            } else if (result.auth_url) {
                window.open(result.auth_url, '_self');
            }
        } catch (e) {
            this.state.githubError = 'Error de conexion';
        }
        this.state.githubLoading = false;
    }

    async _githubLogout() {
        if (!this.state.selectedInstance) return;
        await rpc('/devops/git/github/logout', {
            instance_id: this.state.selectedInstance.id,
        });
        this.state.githubConfigured = false;
        this.state.githubUser = '';
        this.state.githubToken = '';
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
                instance_id: this.state.selectedInstance ? this.state.selectedInstance.id : null,
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
        const id = this.state.selectedInstance.id;
        await rpc('/devops/instance/start', { instance_id: id });
        await this._loadProjectData();
        const updated = this.state.instances.find(i => i.id === id);
        if (updated) this.state.selectedInstance = updated;
    }

    async _stopInstance() {
        if (!this.state.selectedInstance) return;
        if (!confirm('Detener esta instancia?')) return;
        const id = this.state.selectedInstance.id;
        await rpc('/devops/instance/stop', { instance_id: id });
        await this._loadProjectData();
        const updated = this.state.instances.find(i => i.id === id);
        if (updated) this.state.selectedInstance = updated;
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
                instance_id: this.state.selectedInstance ? this.state.selectedInstance.id : null,
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
                stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            } else {
                // Desktop: capture tab audio + mic
                try {
                    // Request tab sharing with audio — video required by API but we stop it
                    const displayOpts = {
                        video: true,
                        audio: true,
                        // Chrome 107+: prefer sharing a tab (shows tab picker first)
                        preferCurrentTab: false,
                    };
                    // Chrome 105+: request system audio to include tab audio by default
                    try { displayOpts.systemAudio = 'include'; } catch (e) {}

                    const tabStream = await navigator.mediaDevices.getDisplayMedia(displayOpts);
                    // Stop video tracks immediately (we only need audio)
                    tabStream.getVideoTracks().forEach(t => t.stop());

                    const tabAudioTracks = tabStream.getAudioTracks();
                    if (tabAudioTracks.length > 0) {
                        // Got tab audio — now also get mic for local voice
                        let micStream;
                        try {
                            micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
                        } catch (e) { /* mic denied */ }

                        // Mix tab audio + mic with AudioContext
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
                        // User didn't check "Share tab audio"
                        tabStream.getTracks().forEach(t => t.stop());
                        const retry = confirm(
                            'No se detecto audio de la pestaña.\n\n' +
                            'Para capturar el audio de la otra persona:\n' +
                            '1. Click "Grabar" de nuevo\n' +
                            '2. Selecciona la pestaña de la llamada\n' +
                            '3. MARCA la casilla "Compartir audio de la pestaña"\n\n' +
                            '¿Grabar solo con microfono?'
                        );
                        if (retry) {
                            stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                        } else {
                            return;
                        }
                    }
                } catch (displayErr) {
                    if (displayErr.name === 'NotAllowedError') return;
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
            // 32 kbps Opus: ~7 MB/hour. Keeps each recording well under Groq
            // Whisper's 25 MB per-file limit even for multi-hour sessions.
            // Voice transcription quality stays excellent at this bitrate.
            this._mediaRecorder = new MediaRecorder(this._recordStream, {
                mimeType,
                audioBitsPerSecond: 32000,
            });

            this._recordChunkRecId = null;
            this._recordFilename = `recording_${Date.now()}.webm`;
            this._recordChunkIndex = 0;
            this._recordChunkQueue = [];
            this._recordUploading = false;
            // Rotation: start a fresh recording every N minutes so no single
            // file balloons past Groq's limit. With 32 kbps @ 25 min ≈ 6 MB.
            const ROTATE_AFTER_MS = 25 * 60 * 1000;
            this._recordSegmentStart = Date.now();

            // Upload chunks incrementally (every 30 seconds of audio)
            const CHUNK_INTERVAL = 30000; // 30 sec per chunk upload
            this._mediaRecorder.ondataavailable = (e) => {
                if (e.data.size > 0) this._recordChunks.push(e.data);
            };

            this._mediaRecorder.onstop = async () => {
                // Upload final remaining chunks
                if (this._recordChunks.length > 0) {
                    const blob = new Blob(this._recordChunks, { type: 'audio/webm' });
                    await this._uploadAudioChunk(mid, blob, true);
                    this._recordChunks = [];
                }
                await rpc('/devops/meetings/update', { meeting_id: mid, state: 'done' });
                await this._loadMeetings();
            };

            // Periodic chunk upload + rotation
            this._chunkUploadTimer = setInterval(async () => {
                if (this._recordChunks.length > 0 && !this._recordUploading) {
                    const blob = new Blob(this._recordChunks, { type: 'audio/webm' });
                    this._recordChunks = [];
                    // Close out this segment if we've been recording long enough;
                    // the next chunk will open a fresh recording server-side.
                    const shouldRotate =
                        Date.now() - this._recordSegmentStart >= ROTATE_AFTER_MS;
                    await this._uploadAudioChunk(mid, blob, shouldRotate, shouldRotate);
                    if (shouldRotate) {
                        // Start a new server-side recording on the next upload
                        this._recordChunkRecId = null;
                        this._recordChunkIndex = 0;
                        this._recordFilename = `recording_${Date.now()}.webm`;
                        this._recordSegmentStart = Date.now();
                    }
                }
            }, CHUNK_INTERVAL);

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

    async _uploadAudioChunk(mid, blob, isLast, rotating = false) {
        this._recordUploading = true;
        try {
            const base64 = await new Promise((resolve) => {
                const reader = new FileReader();
                reader.onload = () => resolve(reader.result.split(',')[1]);
                reader.readAsDataURL(blob);
            });
            const duration = this._recordStartTime ? Math.round((Date.now() - this._recordStartTime) / 60000) : 0;
            const result = await rpc('/devops/meetings/upload_chunk', {
                meeting_id: mid,
                recording_id: this._recordChunkRecId || null,
                chunk_data: base64,
                chunk_index: this._recordChunkIndex++,
                is_last: isLast,
                rotating: rotating,
                filename: this._recordFilename,
                duration: duration || 1,
            });
            if (result.recording_id) this._recordChunkRecId = result.recording_id;
        } catch (e) {
            console.error('Chunk upload error:', e);
        }
        this._recordUploading = false;
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
        if (this._chunkUploadTimer) {
            clearInterval(this._chunkUploadTimer);
            this._chunkUploadTimer = null;
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

    async _meetTranscribeAll(ev) {
        const mid = parseInt(ev.currentTarget.dataset.mid);
        const force = this.state.meetings?.find(m => m.id === mid)?.has_transcription || false;
        this.state.meetTranscribingId = mid;
        try {
            const result = await rpc('/devops/meetings/transcribe_all', { meeting_id: mid, force });
            if (result.error) {
                alert('Error: ' + result.error);
            } else {
                this.state.meetTranscriptionId = mid;
                this.state.meetTranscription = result.transcription;
                if (result.warnings && result.warnings.length > 0) {
                    alert('Advertencias:\n' + result.warnings.join('\n'));
                }
            }
        } catch (e) { alert('Error: ' + e.message); }
        this.state.meetTranscribingId = null;
        await this._loadMeetings();
    }

    async _meetExtractTasks(ev) {
        const mid = parseInt(ev.currentTarget.dataset.mid);
        this.state.meetAnalyzingId = mid;
        try {
            const analysis = await rpc('/devops/meetings/analyze', { meeting_id: mid });
            if (analysis.error) {
                alert('Error: ' + analysis.error);
            } else {
                this.state.meetAnalyzedId = mid;
                this.state.meetAnalyzedTasks = analysis.tasks || [];
                if (this.state.meetAnalyzedTasks.length === 0) {
                    alert('No se encontraron tareas en la transcripcion.');
                }
            }
        } catch (e) { alert('Error: ' + e.message); }
        this.state.meetAnalyzingId = null;
        await this._loadMeetings();
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

    _copilotProjectId() {
        // Per-project auth: use the project currently shown in settings.
        return this.state.settingsProject?.id || null;
    }

    async _copilotLoadStatus() {
        const project_id = this._copilotProjectId();
        if (!project_id) {
            this.state.copilotAuthenticated = false;
            this.state.copilotGithubUser = '';
            return;
        }
        try {
            const res = await rpc('/devops/copilot/status', { project_id });
            this.state.copilotAuthenticated = res.authenticated || false;
            this.state.copilotGithubUser = res.github_user || '';
        } catch (e) { /* ignore */ }
    }

    async _copilotStartAuth() {
        const project_id = this._copilotProjectId();
        if (!project_id) { alert('Guarda el proyecto primero.'); return; }
        this.state.copilotAuthCode = '';
        this.state.copilotAuthUri = '';
        try {
            const res = await rpc('/devops/copilot/start_auth');
            if (res.error) { alert(res.error); return; }
            this.state.copilotAuthCode = res.user_code;
            this.state.copilotAuthUri = res.verification_uri;
            this.state.copilotDeviceCode = res.device_code;
            window.open(res.verification_uri, '_blank');
            this._copilotPoll(res.device_code, res.interval || 5, project_id);
        } catch (e) { alert('Error: ' + (e.message || e)); }
    }

    async _copilotPoll(deviceCode, interval, project_id) {
        this.state.copilotAuthPolling = true;
        const maxAttempts = 60;
        for (let i = 0; i < maxAttempts; i++) {
            await new Promise(r => setTimeout(r, interval * 1000));
            if (!this.state.copilotAuthPolling) break;
            try {
                const res = await rpc('/devops/copilot/poll_auth', {
                    device_code: deviceCode,
                    project_id,
                });
                if (res.status === 'success') {
                    this.state.copilotAuthenticated = true;
                    this.state.copilotGithubUser = res.github_user || '';
                    this.state.copilotAuthCode = '';
                    this.state.copilotAuthPolling = false;
                    return;
                } else if (res.status === 'slow_down') {
                    interval = res.interval || interval + 5;
                } else if (res.status === 'expired' || res.status === 'denied' || res.status === 'error') {
                    this.state.copilotAuthCode = '';
                    this.state.copilotAuthPolling = false;
                    alert('Autenticación ' + (res.status === 'expired' ? 'expirada' : 'denegada') + '. Intenta de nuevo.');
                    return;
                }
            } catch (e) { break; }
        }
        this.state.copilotAuthPolling = false;
    }

    async _copilotDisconnect() {
        const project_id = this._copilotProjectId();
        if (!project_id) return;
        if (!confirm('Desconectar GitHub Copilot de este proyecto?')) return;
        try {
            await rpc('/devops/copilot/disconnect', { project_id });
            this.state.copilotAuthenticated = false;
            this.state.copilotGithubUser = '';
        } catch (e) { /* ignore */ }
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
        const id = this.state.selectedInstance.id;
        await rpc('/devops/instance/restart', { instance_id: id });
        await this._loadProjectData();
        const updated = this.state.instances.find(i => i.id === id);
        if (updated) this.state.selectedInstance = updated;
    }

    async _runPostCloneScript() {
        if (!this.state.selectedInstance) return;
        this.state.postCloneResult = ['Ejecutando...'];
        try {
            const result = await rpc('/devops/instance/run_post_clone', {
                instance_id: this.state.selectedInstance.id,
            });
            if (result.error) {
                this.state.postCloneResult = [result.error];
            } else {
                this.state.postCloneResult = result.results || ['OK'];
            }
        } catch (e) {
            this.state.postCloneResult = ['Error: ' + e.message];
        }
    }

    _runPostCloneWithClaude() {
        if (!this.state.selectedInstance) return;
        const inst = this.state.selectedInstance;
        const prompt = `Ejecuta el siguiente script de post-clonacion en la base de datos "${inst.database_name}" de esta instancia ${inst.instance_type}:

1. Actualiza web.base.url a https://${inst.full_domain || inst.url || ''}
2. Actualiza report.url al mismo dominio
3. Elimina database.uuid y database.enterprise_code de ir_config_parameter
4. Desactiva todos los mail servers (ir_mail_server)
5. Desactiva todos los fetchmail servers
6. Desactiva crons innecesarios (deja solo session cleanup y autovacuum)
7. Verifica que los cambios se aplicaron correctamente

Usa psql -d ${inst.database_name} para ejecutar los comandos SQL.`;

        // Switch to AI tab and send the prompt
        this._onContentTabChange('ai');
        setTimeout(() => {
            if (this._aiWs && this._aiWs.readyState === WebSocket.OPEN) {
                this._aiWs.send(JSON.stringify({ type: 'input', data: prompt + '\n' }));
                this._setAiLastPrompt(prompt);
            }
        }, 2000);
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
            const r = await rpc('/devops/instance/detect_service', {
                service_name: name,
                project_id: this.state.currentProjectId || null,
            });
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
        // Load members, available users, Odoo projects, and Copilot status
        await this._loadMembers();
        await this._loadAvailableUsers();
        await this._loadOdooProjects();
        this._copilotLoadStatus();
    }

    async _loadOdooProjects() {
        try {
            const projects = await rpc('/web/dataset/call_kw', {
                model: 'project.project', method: 'search_read',
                args: [[]],
                kwargs: { fields: ['id', 'name'], limit: 100, order: 'name' },
            });
            this.state.odooProjects = projects;
        } catch (e) { this.state.odooProjects = []; }
    }

    _onOdooProjectChange(ev) {
        const val = parseInt(ev.target.value) || false;
        if (this.state.settingsProject) {
            this.state.settingsProject.odoo_project_id = val;
        }
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

    async _saveProject() {
        const p = this.state.settingsProject;
        if (!p) return;
        try {
            const result = await rpc('/devops/project/save', {
                project_id: p.id || null,
                name: p.name,
                domain: p.domain,
                subdomain_base: p.subdomain_base,
                repo_path: p.repo_path,
                enterprise_path: p.enterprise_path,
                database_name: p.database_name,
                connection_type: p.connection_type,
                odoo_service_name: p.odoo_service_name || this.state.autodetectService || '',
                ssh_host: p.ssh_host,
                ssh_user: p.ssh_user,
                ssh_port: p.ssh_port,
                max_staging: p.max_staging,
                max_development: p.max_development,
                auto_destroy_hours: p.auto_destroy_hours,
                production_branch: p.production_branch,
                github_client_id: p.github_client_id,
                github_client_secret: p.github_client_secret,
                post_clone_script: p.post_clone_script,
                odoo_project_id: p.odoo_project_id || false,
                sync_tasks_to_production: !!p.sync_tasks_to_production,
                production_admin_login: p.production_admin_login || '',
                production_admin_password: p.production_admin_password || '',
                production_project_id_remote: p.production_project_id_remote || 0,
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

    _onSettingsCheckbox(ev, field) {
        if (!this.state.settingsProject) return;
        this.state.settingsProject[field] = ev.target.checked;
    }

    async _propagateClaudeModel() {
        this.state.claudePropagateResult = 'Propagando...';
        try {
            const res = await rpc('/devops/admin/propagate_claude_model', { target_model: 'claude-opus-4-7' });
            if (res.error) {
                this.state.claudePropagateResult = 'ERROR: ' + res.error;
                return;
            }
            const lines = ['Target: ' + res.target_model, ''];
            Object.keys(res.summary || {}).forEach(proj => {
                lines.push(proj + ':');
                Object.keys(res.summary[proj]).forEach(db => {
                    lines.push('  ' + db + ': ' + res.summary[proj][db]);
                });
            });
            this.state.claudePropagateResult = lines.join('\n');
        } catch (e) {
            this.state.claudePropagateResult = 'Error: ' + (e.message || e);
        }
    }

    async _upgradeClaudeCli() {
        const p = this.state.settingsProject;
        if (!p || !p.id) return;
        this.state.claudeCliUpgrading = true;
        this.state.claudeCliUpgradeResult = 'Ejecutando npm install en ' + (p.ssh_host || 'local') + '...';
        try {
            const res = await rpc('/devops/project/upgrade_claude_cli', { project_id: p.id });
            if (res.error) {
                this.state.claudeCliUpgradeResult = 'ERROR: ' + res.error;
            } else {
                const lines = ['Version: ' + (res.version || 'desconocida'), ''];
                if (res.stdout) lines.push(res.stdout);
                this.state.claudeCliUpgradeResult = lines.join('\n');
            }
        } catch (e) {
            this.state.claudeCliUpgradeResult = 'Error: ' + (e.message || e);
        } finally {
            this.state.claudeCliUpgrading = false;
        }
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

            // Prevent horizontal scroll from propagating to page
            container.addEventListener('wheel', (ev) => {
                if (Math.abs(ev.deltaX) > 0) ev.preventDefault();
            }, { passive: false });

            // Re-fit on container resize
            this._aiResizeObserver = new ResizeObserver(() => {
                if (this._aiFitAddon) { try { this._aiFitAddon.fit(); } catch (e) {} }
            });
            this._aiResizeObserver.observe(container);

            // Forward keyboard input to WebSocket
            this._aiTerm.onData((data) => {
                if (this._aiWs && this._aiWs.readyState === WebSocket.OPEN) {
                    this._aiWs.send(JSON.stringify({ type: 'input', data: data }));
                    this._captureTypedInput(data);
                }
            });

            // File paste support: intercept files from clipboard (images, PDFs, etc.)
            // Only intercept if clipboard contains files AND no plain text
            // (so normal text paste always works for xterm)
            this._aiPasteHandler = (ev) => {
                // Guard: only handle when AI tab is active (avoid double-fire)
                if (this.state.activeContentTab !== 'ai') return;
                const items = ev.clipboardData && ev.clipboardData.items;
                if (!items) return;
                // Any file item wins — image paste often also ships text metadata
                let fileItem = null;
                for (const item of items) {
                    if (item.kind === 'file') { fileItem = item; break; }
                }
                if (!fileItem) return;
                ev.preventDefault();
                ev.stopPropagation();
                if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
                const blob = fileItem.getAsFile();
                if (!blob) return;
                // Block files > 8MB (server limit is 10MB but base64 adds ~33%)
                if (blob.size > 8 * 1024 * 1024) {
                    if (this._aiTerm) {
                        this._aiTerm.writeln(`\x1b[31m[Error: Archivo demasiado grande (${(blob.size/1024/1024).toFixed(1)}MB). Máximo 8MB]\x1b[0m`);
                    }
                    return;
                }
                const ext = blob.name ? blob.name.split('.').pop() : (fileItem.type.split('/')[1] || 'bin');
                const filename = blob.name || `paste_${Date.now()}.${ext}`;
                const reader = new FileReader();
                reader.onload = () => {
                    const base64 = reader.result.split(',')[1];
                    if (this._aiWs && this._aiWs.readyState === WebSocket.OPEN) {
                        try {
                            this._aiWs.send(JSON.stringify({
                                type: 'file',
                                data: base64,
                                filename: filename,
                                mimetype: fileItem.type,
                            }));
                        } catch (e) {
                            if (this._aiTerm) {
                                this._aiTerm.writeln(`\x1b[31m[Error al enviar archivo: ${e.message}]\x1b[0m`);
                            }
                            return;
                        }
                        if (this._aiTerm) {
                            const size = blob.size > 1024 ? `${(blob.size/1024).toFixed(1)}KB` : `${blob.size}B`;
                            this._aiTerm.writeln(`\x1b[33m[Archivo: ${filename} (${size})]\x1b[0m`);
                        }
                    }
                };
                reader.readAsDataURL(blob);
            };
            // Capture phase so we beat xterm.js's helper textarea paste handler
            document.addEventListener('paste', this._aiPasteHandler, true);

            // Drag & drop file support
            this._aiDropHandler = (ev) => {
                ev.preventDefault();
                ev.stopPropagation();
                const files = ev.dataTransfer && ev.dataTransfer.files;
                if (!files || files.length === 0) return;
                for (const file of files) {
                    this._sendFileToTerminal(file);
                }
            };
            this._aiDragOverHandler = (ev) => { ev.preventDefault(); ev.stopPropagation(); };
            container.addEventListener('drop', this._aiDropHandler, true);
            container.addEventListener('dragover', this._aiDragOverHandler, true);

            // If WebSocket already connected OR connecting, reattach instead of creating new
            if (this._aiWs && (this._aiWs.readyState === WebSocket.OPEN || this._aiWs.readyState === WebSocket.CONNECTING)) {
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

            const forceNew = this._aiForceNew || false;
            this._aiForceNew = false;
            const tokenResult = await rpc('/devops/ai/token', {
                project_id: this.state.currentProjectId || null,
                instance_id: this.state.selectedInstance ? this.state.selectedInstance.id : null,
                force_new: forceNew,
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
        if (this._aiWs && this._aiWs.readyState === WebSocket.OPEN) {
            this._aiWs.send(JSON.stringify({ type: 'input', data: '/resume' + String.fromCharCode(13) }));
        }
        if (this._aiTerm) this._aiTerm.focus();
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
        ws.send(JSON.stringify({ type: 'input', data: String.fromCharCode(3) }));  // Ctrl+C
        setTimeout(() => {
            ws.send(JSON.stringify({ type: 'input', data: `/resume ${sessionId}` + String.fromCharCode(13) }));
        }, 300);
        this.state.claudeSessionsVisible = false;
        if (this._aiTerm) this._aiTerm.focus();
    }

    // Control character map for terminal buttons
    _ctrlChar(code) { return String.fromCharCode(code); }

    _sendTermKey(data) {
        if (this._aiWs && this._aiWs.readyState === WebSocket.OPEN) {
            this._aiWs.send(JSON.stringify({ type: 'input', data }));
        }
        if (this._aiTerm) this._aiTerm.focus();
    }

    _sendShellKey(data) {
        if (this._shellWs && this._shellWs.readyState === WebSocket.OPEN) {
            this._shellWs.send(JSON.stringify({ type: 'input', data }));
        }
        if (this._shellTerm) this._shellTerm.focus();
    }

    _sendFileToTerminal(file) {
        if (!this._aiWs || this._aiWs.readyState !== WebSocket.OPEN) return;
        // Block files > 8MB
        if (file.size > 8 * 1024 * 1024) {
            if (this._aiTerm) {
                this._aiTerm.writeln(`\x1b[31m[Error: Archivo demasiado grande (${(file.size/1024/1024).toFixed(1)}MB). Máximo 8MB]\x1b[0m`);
            }
            return;
        }
        const reader = new FileReader();
        reader.onload = () => {
            const base64 = reader.result.split(',')[1];
            try {
                this._aiWs.send(JSON.stringify({
                    type: 'file',
                    data: base64,
                    filename: file.name,
                    mimetype: file.type,
                }));
            } catch (e) {
                if (this._aiTerm) {
                    this._aiTerm.writeln(`\x1b[31m[Error al enviar archivo: ${e.message}]\x1b[0m`);
                }
                return;
            }
            if (this._aiTerm) {
                const size = file.size > 1024 ? `${(file.size/1024).toFixed(1)}KB` : `${file.size}B`;
                this._aiTerm.writeln(`\x1b[33m[Archivo: ${file.name} (${size})]\x1b[0m`);
            }
        };
        reader.readAsDataURL(file);
    }

    async _pasteToAiTerminal() {
        if (!this._aiWs || this._aiWs.readyState !== WebSocket.OPEN) return;
        // First try the async Clipboard API for files (images/pdfs/etc).
        // readText() returns empty when the clipboard holds only a Blob.
        if (navigator.clipboard && navigator.clipboard.read) {
            try {
                const items = await navigator.clipboard.read();
                for (const item of items) {
                    const fileType = item.types.find(t =>
                        t.startsWith('image/') ||
                        t === 'application/pdf' ||
                        t.startsWith('application/') ||
                        t.startsWith('audio/') ||
                        t.startsWith('video/')
                    );
                    if (fileType) {
                        const blob = await item.getType(fileType);
                        const ext = fileType.split('/')[1].split('+')[0] || 'bin';
                        const filename = `paste_${Date.now()}.${ext}`;
                        this._sendBlobToAiTerminal(blob, filename, fileType);
                        if (this._aiTerm) this._aiTerm.focus();
                        return;
                    }
                }
            } catch (e) {
                // fall through to text path
            }
        }
        try {
            const text = await navigator.clipboard.readText();
            if (text) {
                this._aiWs.send(JSON.stringify({ type: 'input', data: text }));
                this._captureTypedInput(text);
                if (this._aiTerm) this._aiTerm.focus();
            }
        } catch (e) {
            const text = prompt('Pegar texto:');
            if (text) {
                this._aiWs.send(JSON.stringify({ type: 'input', data: text }));
                this._captureTypedInput(text);
            }
        }
    }

    _sendBlobToAiTerminal(blob, filename, mimetype) {
        if (!blob) return;
        if (blob.size > 8 * 1024 * 1024) {
            if (this._aiTerm) {
                this._aiTerm.writeln(`\x1b[31m[Error: Archivo demasiado grande (${(blob.size/1024/1024).toFixed(1)}MB). Máximo 8MB]\x1b[0m`);
            }
            return;
        }
        const reader = new FileReader();
        reader.onload = () => {
            const base64 = reader.result.split(',')[1];
            if (!this._aiWs || this._aiWs.readyState !== WebSocket.OPEN) {
                if (this._aiTerm) this._aiTerm.writeln('\x1b[31m[WS cerrado]\x1b[0m');
                return;
            }
            try {
                this._aiWs.send(JSON.stringify({
                    type: 'file',
                    data: base64,
                    filename: filename,
                    mimetype: mimetype || blob.type || 'application/octet-stream',
                }));
                if (this._aiTerm) {
                    const size = blob.size > 1024 ? `${(blob.size/1024).toFixed(1)}KB` : `${blob.size}B`;
                    this._aiTerm.writeln(`\x1b[33m[Archivo: ${filename} (${size})]\x1b[0m`);
                }
            } catch (e) {
                if (this._aiTerm) this._aiTerm.writeln(`\x1b[31m[Error al enviar archivo: ${e.message}]\x1b[0m`);
            }
        };
        reader.readAsDataURL(blob);
    }

    _attachFileToTerminal() {
        const input = document.createElement('input');
        input.type = 'file';
        input.multiple = true;
        input.onchange = () => {
            for (const file of input.files) {
                this._sendFileToTerminal(file);
            }
        };
        input.click();
    }

    _newClaudeSession() {
        if (!this._aiWs || this._aiWs.readyState !== WebSocket.OPEN) return;
        // Ctrl+C to cancel current, then /exit + Enter, wait, reconnect creates new session
        this._aiWs.send(JSON.stringify({ type: 'input', data: String.fromCharCode(3) }));
        setTimeout(() => {
            this._aiWs.send(JSON.stringify({ type: 'input', data: '/exit' + String.fromCharCode(13) }));
        }, 300);
        if (this._aiTerm) this._aiTerm.focus();
    }

    _reconnectAiTerminal() {
        // Reconnect to existing session (same behavior as browser reload)
        if (this._aiWs) { this._aiWs.close(); this._aiWs = null; }
        this.state.aiConnected = false;
        this._aiTermInitializing = false;
        this._initAiTerminal();
    }

    _restartAiTerminal() {
        // Force kill existing PTY + spawn fresh process (needed after Claude CLI upgrade)
        this._aiForceNew = true;
        this._aiScrollback = [];
        if (this._aiTerm) {
            this._aiTerm.clear();
            this._aiTerm.writeln('\x1b[33m[Reiniciando sesion Claude con proceso nuevo...]\x1b[0m');
        }
        if (this._aiWs) { this._aiWs.close(); this._aiWs = null; }
        this.state.aiConnected = false;
        this._aiTermInitializing = false;
        this._initAiTerminal();
    }

    _reconnectShellTerminal() {
        if (this._shellWs) { this._shellWs.close(); this._shellWs = null; }
        this.state.terminalConnected = false;
        if (this._shellTerm) this._shellTerm.writeln('\r\n\x1b[33m[Reconectando...]\x1b[0m');
        this._shellTermInitializing = false;
        this._initShellTerminal();
    }

    _cleanupAiTerminal() {
        if (this._aiPasteHandler) {
            document.removeEventListener('paste', this._aiPasteHandler, true);
            this._aiPasteHandler = null;
        }
        this._aiDropHandler = null;
        this._aiDragOverHandler = null;
        if (this._aiResizeObserver) { this._aiResizeObserver.disconnect(); this._aiResizeObserver = null; }
        if (this._aiWs) { this._aiWs.close(); this._aiWs = null; }
        if (this._aiTerm) { this._aiTerm.dispose(); this._aiTerm = null; }
        this._aiFitAddon = null;
        this.state.aiConnected = false;
    }
}

registry.category("actions").add("pmb_devops_main", PmbDevopsApp);
