/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { rpc } from "@web/core/network/rpc";

const XTERM_JS_URL = "https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js";
const XTERM_CSS_URL = "https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css";
const FIT_ADDON_URL = "https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js";

const TOKYO_NIGHT_THEME = {
    background: "#1a1b26",
    foreground: "#c0caf5",
    cursor: "#c0caf5",
    cursorAccent: "#1a1b26",
    selectionBackground: "#33467c",
    selectionForeground: "#c0caf5",
    black: "#15161e",
    red: "#f7768e",
    green: "#9ece6a",
    yellow: "#e0af68",
    blue: "#7aa2f7",
    magenta: "#bb9af7",
    cyan: "#7dcfff",
    white: "#a9b1d6",
    brightBlack: "#414868",
    brightRed: "#f7768e",
    brightGreen: "#9ece6a",
    brightYellow: "#e0af68",
    brightBlue: "#7aa2f7",
    brightMagenta: "#bb9af7",
    brightCyan: "#7dcfff",
    brightWhite: "#c0caf5",
};

const WELCOME_BANNER = [
    "\x1b[38;2;122;162;247m",
    "  ____       _       _     __  __       ____        _       ",
    " |  _ \\ __ _| |_ ___| |__ |  \\/  |_   _| __ ) _   _| |_ ___ ",
    " | |_) / _` | __/ __| '_ \\| |\\/| | | | |  _ \\| | | | __/ _ \\",
    " |  __/ (_| | || (__| | | | |  | | |_| | |_) | |_| | ||  __/",
    " |_|   \\__,_|\\__\\___|_| |_|_|  |_|\\__, |____/ \\__, |\\__\\___|",
    "                                   |___/       |___/         ",
    "\x1b[0m",
    "\x1b[38;2;158;206;106m  PatchMyByte DevOps Terminal\x1b[0m",
    "",
    "\x1b[38;2;192;202;245m  Select a project and a session type to begin.\x1b[0m",
    "",
].join("\r\n");

class DevopsTerminal extends Component {
    static template = "pmb_devops.DevopsTerminal";
    static props = { "*": true };

    setup() {
        this.terminalContainerRef = useRef("terminalContainer");
        this.state = useState({
            projects: [],
            projectId: null,
            activeTab: null,
            status: "Desconectado",
        });

        this._term = null;
        this._fitAddon = null;
        this._pollTimer = null;
        this._readPos = 0;
        this._resizeObserver = null;
        this._alive = false;

        onMounted(async () => {
            await this._loadProjects();
            await this._initTerminal();
        });

        onWillUnmount(() => {
            this._cleanup();
        });
    }

    // ------------------------------------------------------------------
    // Load projects from the server
    // ------------------------------------------------------------------

    async _loadProjects() {
        try {
            const projects = await rpc("/web/dataset/call_kw", {
                model: "devops.project",
                method: "search_read",
                args: [[["active", "=", true]]],
                kwargs: {
                    fields: ["id", "name", "odoo_service_name"],
                    limit: 100,
                },
            });
            this.state.projects = projects;
            if (projects.length > 0 && !this.state.projectId) {
                this.state.projectId = projects[0].id;
            }
        } catch (err) {
            console.error("DevopsTerminal: error loading projects", err);
        }
    }

    // ------------------------------------------------------------------
    // Initialize xterm.js
    // ------------------------------------------------------------------

    async _initTerminal() {
        // Load xterm.js and FitAddon from CDN
        await this._loadCSS(XTERM_CSS_URL);
        await this._loadScript(XTERM_JS_URL);
        await this._loadScript(FIT_ADDON_URL);

        const Terminal = window.Terminal;
        const FitAddon = window.FitAddon ? window.FitAddon.FitAddon : null;

        if (!Terminal) {
            console.error("DevopsTerminal: xterm.js not loaded");
            return;
        }

        const container = this.terminalContainerRef.el;
        if (!container) {
            return;
        }

        this._term = new Terminal({
            theme: TOKYO_NIGHT_THEME,
            fontSize: 14,
            fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', 'Consolas', monospace",
            cursorBlink: true,
            cursorStyle: "bar",
            scrollback: 5000,
            allowProposedApi: true,
        });

        if (FitAddon) {
            this._fitAddon = new FitAddon();
            this._term.loadAddon(this._fitAddon);
        }

        this._term.open(container);

        if (this._fitAddon) {
            try {
                this._fitAddon.fit();
            } catch {
                // ignore fit errors during init
            }
        }

        // Show welcome banner
        this._term.write(WELCOME_BANNER);

        // Handle user input
        this._term.onData((data) => {
            if (this._alive && this.state.activeTab) {
                this._sendInput(data);
            }
        });

        // Handle terminal resize
        this._term.onResize(({ cols, rows }) => {
            if (this._alive && this.state.activeTab) {
                this._sendResize(rows, cols);
            }
        });

        // ResizeObserver for auto-fit
        this._resizeObserver = new ResizeObserver(() => {
            if (this._fitAddon && this._term) {
                try {
                    this._fitAddon.fit();
                } catch {
                    // ignore
                }
            }
        });
        this._resizeObserver.observe(container);
    }

    // ------------------------------------------------------------------
    // Session management
    // ------------------------------------------------------------------

    async _startSession(sessionType) {
        // Stop any existing polling
        this._stopPolling();

        // Clear terminal
        if (this._term) {
            this._term.clear();
            this._term.reset();
        }

        this._readPos = 0;
        this.state.activeTab = sessionType;
        this.state.status = "Conectando...";

        // Build params for the start RPC
        const params = { session_type: sessionType };
        if (sessionType === "logs" && this.state.projectId) {
            const project = this.state.projects.find(
                (p) => p.id === this.state.projectId
            );
            if (project && project.odoo_service_name) {
                params.service = project.odoo_service_name;
            } else {
                params.service = "odoo19";
            }
        }

        try {
            const result = await rpc("/devops/terminal/start", params);
            if (result.error) {
                this.state.status = `Error: ${result.error}`;
                if (this._term) {
                    this._term.write(
                        `\r\n\x1b[31mError: ${result.error}\x1b[0m\r\n`
                    );
                }
                return;
            }

            this._alive = true;
            this.state.status = `${sessionType.toUpperCase()} - Conectado`;

            if (this._term && result.message) {
                this._term.write(
                    `\x1b[38;2;122;162;247m${result.message}\x1b[0m\r\n\r\n`
                );
            }

            // Send initial resize
            if (this._term && this._fitAddon) {
                try {
                    this._fitAddon.fit();
                } catch {
                    // ignore
                }
                this._sendResize(this._term.rows, this._term.cols);
            }

            // Start polling for output
            this._startPolling();
        } catch (err) {
            console.error("DevopsTerminal: error starting session", err);
            this.state.status = "Error de conexion";
            if (this._term) {
                this._term.write(
                    `\r\n\x1b[31mError starting session: ${err.message || err}\x1b[0m\r\n`
                );
            }
        }
    }

    // ------------------------------------------------------------------
    // Output polling
    // ------------------------------------------------------------------

    _startPolling() {
        this._stopPolling();
        this._pollTimer = setInterval(() => this._readOutput(), 100);
    }

    _stopPolling() {
        if (this._pollTimer) {
            clearInterval(this._pollTimer);
            this._pollTimer = null;
        }
    }

    async _readOutput() {
        if (!this.state.activeTab || !this._alive) {
            return;
        }

        try {
            const result = await rpc("/devops/terminal/read", {
                session_type: this.state.activeTab,
                pos: this._readPos,
            });

            if (result.output && this._term) {
                this._term.write(result.output);
            }

            if (result.pos !== undefined) {
                this._readPos = result.pos;
            }

            if (result.alive === false) {
                this._alive = false;
                this.state.status = `${this.state.activeTab.toUpperCase()} - Desconectado`;
                this._stopPolling();
                if (this._term) {
                    this._term.write(
                        "\r\n\x1b[33m[Session ended]\x1b[0m\r\n"
                    );
                }
            }
        } catch {
            // Silently ignore polling errors to avoid console spam
        }
    }

    // ------------------------------------------------------------------
    // Input & Resize
    // ------------------------------------------------------------------

    async _sendInput(data) {
        if (!this.state.activeTab) {
            return;
        }
        try {
            await rpc("/devops/terminal/write", {
                session_type: this.state.activeTab,
                data: data,
            });
        } catch {
            // ignore
        }
    }

    async _sendResize(rows, cols) {
        if (!this.state.activeTab) {
            return;
        }
        try {
            await rpc("/devops/terminal/resize", {
                session_type: this.state.activeTab,
                rows: rows,
                cols: cols,
            });
        } catch {
            // ignore
        }
    }

    // ------------------------------------------------------------------
    // Project change
    // ------------------------------------------------------------------

    _onProjectChange(ev) {
        const val = parseInt(ev.target.value, 10);
        this.state.projectId = val || null;
    }

    // ------------------------------------------------------------------
    // Cleanup
    // ------------------------------------------------------------------

    _cleanup() {
        this._stopPolling();

        if (this._resizeObserver) {
            this._resizeObserver.disconnect();
            this._resizeObserver = null;
        }

        // Stop server-side session
        if (this.state.activeTab && this._alive) {
            rpc("/devops/terminal/stop", {
                session_type: this.state.activeTab,
            }).catch(() => {});
        }

        if (this._term) {
            this._term.dispose();
            this._term = null;
        }

        this._fitAddon = null;
        this._alive = false;
    }

    // ------------------------------------------------------------------
    // CDN loading helpers
    // ------------------------------------------------------------------

    _loadScript(url) {
        return new Promise((resolve, reject) => {
            // Check if already loaded
            const existing = document.querySelector(`script[src="${url}"]`);
            if (existing) {
                resolve();
                return;
            }
            const script = document.createElement("script");
            script.src = url;
            script.onload = resolve;
            script.onerror = () =>
                reject(new Error(`Failed to load script: ${url}`));
            document.head.appendChild(script);
        });
    }

    _loadCSS(url) {
        return new Promise((resolve, reject) => {
            // Check if already loaded
            const existing = document.querySelector(`link[href="${url}"]`);
            if (existing) {
                resolve();
                return;
            }
            const link = document.createElement("link");
            link.rel = "stylesheet";
            link.href = url;
            link.onload = resolve;
            link.onerror = () =>
                reject(new Error(`Failed to load CSS: ${url}`));
            document.head.appendChild(link);
        });
    }
}

registry.category("actions").add("devops_terminal", DevopsTerminal);
