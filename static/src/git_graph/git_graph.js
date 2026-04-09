/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { rpc } from "@web/core/network/rpc";

const BRANCH_COLORS = {
    production: "#f7768e",
    staging: "#e0af68",
    development: "#9ece6a",
};

const TYPE_LABELS = {
    production: "PROD",
    staging: "STG",
    development: "DEV",
};

const CANVAS_CONFIG = {
    branchSpacingX: 200,
    commitSpacingY: 60,
    dotRadius: 6,
    lineWidth: 3,
    startX: 80,
    startY: 60,
    fontSize: 12,
    hashFontSize: 11,
    labelFontSize: 11,
    bgColor: "#1a1b26",
    textColor: "#c0caf5",
    dimTextColor: "#565f89",
    headBadgeColor: "#e0af68",
};

class DevopsGitGraph extends Component {
    static template = "pmb_devops.DevopsGitGraph";
    static props = { "*": true };

    setup() {
        this.canvasRef = useRef("graphCanvas");
        this.state = useState({
            projects: [],
            projectId: null,
            branches: [],
            selectedBranch: null,
            loading: false,
        });

        this._resizeObserver = null;

        onMounted(async () => {
            await this._loadProjects();
            if (this.state.projectId) {
                await this._loadData();
            }
        });

        onWillUnmount(() => {
            if (this._resizeObserver) {
                this._resizeObserver.disconnect();
                this._resizeObserver = null;
            }
        });
    }

    // ------------------------------------------------------------------
    // Load projects
    // ------------------------------------------------------------------

    async _loadProjects() {
        try {
            const projects = await rpc("/web/dataset/call_kw", {
                model: "devops.project",
                method: "search_read",
                args: [[["active", "=", true]]],
                kwargs: {
                    fields: ["id", "name", "repo_current_branch"],
                    limit: 100,
                },
            });
            this.state.projects = projects;
            if (projects.length > 0 && !this.state.projectId) {
                this.state.projectId = projects[0].id;
            }
        } catch (err) {
            console.error("DevopsGitGraph: error loading projects", err);
        }
    }

    // ------------------------------------------------------------------
    // Load branch data
    // ------------------------------------------------------------------

    async _loadData() {
        if (!this.state.projectId) {
            return;
        }

        this.state.loading = true;

        try {
            const branches = await rpc("/web/dataset/call_kw", {
                model: "devops.branch",
                method: "search_read",
                args: [[["project_id", "=", this.state.projectId]]],
                kwargs: {
                    fields: [
                        "id",
                        "name",
                        "branch_type",
                        "is_current",
                        "last_commit_hash",
                        "last_commit_message",
                        "last_commit_author",
                        "last_commit_date",
                        "commit_history",
                        "commits_ahead",
                        "commits_behind",
                    ],
                    order: "branch_type, name",
                    limit: 200,
                },
            });

            // Parse commit_history JSON for each branch
            for (const branch of branches) {
                if (branch.commit_history) {
                    try {
                        branch._commits = JSON.parse(branch.commit_history);
                    } catch {
                        branch._commits = [];
                    }
                } else {
                    branch._commits = [];
                }
            }

            this.state.branches = branches;
            this.state.loading = false;

            // Draw after state update (next tick)
            setTimeout(() => this._drawGraph(), 0);
        } catch (err) {
            console.error("DevopsGitGraph: error loading branches", err);
            this.state.loading = false;
        }
    }

    // ------------------------------------------------------------------
    // Draw the git graph on canvas
    // ------------------------------------------------------------------

    _drawGraph() {
        const canvas = this.canvasRef.el;
        if (!canvas) {
            return;
        }

        const branches = this.state.branches;
        if (!branches || branches.length === 0) {
            return;
        }

        const cfg = CANVAS_CONFIG;

        // Group branches by type for ordering
        const typeOrder = ["production", "staging", "development"];
        const sortedBranches = [];
        for (const t of typeOrder) {
            for (const b of branches) {
                if (b.branch_type === t) {
                    sortedBranches.push(b);
                }
            }
        }

        // Calculate max commits across all branches for canvas height
        let maxCommits = 1;
        for (const b of sortedBranches) {
            const count = b._commits ? b._commits.length : 1;
            if (count > maxCommits) {
                maxCommits = count;
            }
        }

        // Set canvas dimensions
        const canvasWidth = Math.max(
            cfg.startX + sortedBranches.length * cfg.branchSpacingX + 100,
            800
        );
        const canvasHeight = Math.max(
            cfg.startY + maxCommits * cfg.commitSpacingY + 100,
            600
        );

        // Handle high DPI
        const dpr = window.devicePixelRatio || 1;
        canvas.width = canvasWidth * dpr;
        canvas.height = canvasHeight * dpr;
        canvas.style.width = canvasWidth + "px";
        canvas.style.height = canvasHeight + "px";

        const ctx = canvas.getContext("2d");
        ctx.scale(dpr, dpr);

        // Clear canvas
        ctx.fillStyle = cfg.bgColor;
        ctx.fillRect(0, 0, canvasWidth, canvasHeight);

        // Draw each branch
        sortedBranches.forEach((branch, branchIndex) => {
            const x = cfg.startX + branchIndex * cfg.branchSpacingX;
            const color = BRANCH_COLORS[branch.branch_type] || "#7aa2f7";
            const typeLabel = TYPE_LABELS[branch.branch_type] || "DEV";
            const commits = branch._commits || [];
            const numCommits = Math.max(commits.length, 1);

            // Draw branch vertical line
            ctx.strokeStyle = color;
            ctx.lineWidth = cfg.lineWidth;
            ctx.globalAlpha = 0.4;
            ctx.beginPath();
            ctx.moveTo(x, cfg.startY);
            ctx.lineTo(x, cfg.startY + numCommits * cfg.commitSpacingY);
            ctx.stroke();
            ctx.globalAlpha = 1.0;

            // Draw type badge
            const badgeY = cfg.startY - 35;
            ctx.fillStyle = color;
            ctx.globalAlpha = 0.2;
            const badgeWidth = ctx.measureText
                ? Math.max(ctx.measureText(typeLabel).width + 12, 40)
                : 40;
            ctx.font = `bold ${cfg.labelFontSize}px monospace`;
            const measuredBadgeWidth = ctx.measureText(typeLabel).width + 14;
            _roundRect(ctx, x - measuredBadgeWidth / 2, badgeY - 8, measuredBadgeWidth, 18, 4);
            ctx.fill();
            ctx.globalAlpha = 1.0;

            ctx.fillStyle = color;
            ctx.font = `bold ${cfg.labelFontSize}px monospace`;
            ctx.textAlign = "center";
            ctx.fillText(typeLabel, x, badgeY + 5);

            // Draw branch name
            const nameY = cfg.startY - 12;
            ctx.fillStyle = cfg.textColor;
            ctx.font = `${cfg.fontSize}px monospace`;
            ctx.textAlign = "center";

            let displayName = branch.name;
            if (displayName.length > 20) {
                displayName = displayName.substring(0, 18) + "..";
            }
            ctx.fillText(displayName, x, nameY);

            // HEAD indicator
            if (branch.is_current) {
                const headY = nameY + 14;
                ctx.fillStyle = cfg.headBadgeColor;
                ctx.font = `bold ${cfg.labelFontSize - 1}px monospace`;
                ctx.textAlign = "center";
                ctx.fillText("HEAD", x, headY);

                // Small triangle pointing down
                ctx.beginPath();
                ctx.moveTo(x - 5, headY + 4);
                ctx.lineTo(x + 5, headY + 4);
                ctx.lineTo(x, headY + 10);
                ctx.closePath();
                ctx.fill();
            }

            // Draw commits
            if (commits.length > 0) {
                commits.forEach((commit, i) => {
                    const cy = cfg.startY + i * cfg.commitSpacingY + 30;

                    // Commit dot
                    ctx.beginPath();
                    ctx.arc(x, cy, cfg.dotRadius, 0, Math.PI * 2);
                    ctx.fillStyle = color;
                    ctx.fill();

                    // White dot center
                    ctx.beginPath();
                    ctx.arc(x, cy, cfg.dotRadius - 2, 0, Math.PI * 2);
                    ctx.fillStyle = cfg.bgColor;
                    ctx.fill();

                    // Commit hash (short)
                    const shortHash = commit.short || (commit.hash || "").substring(0, 7);
                    ctx.fillStyle = cfg.dimTextColor;
                    ctx.font = `${cfg.hashFontSize}px monospace`;
                    ctx.textAlign = "left";
                    ctx.fillText(shortHash, x + cfg.dotRadius + 6, cy - 4);

                    // Commit message (truncated)
                    let msg = commit.message || "";
                    if (msg.length > 22) {
                        msg = msg.substring(0, 20) + "..";
                    }
                    ctx.fillStyle = cfg.textColor;
                    ctx.font = `${cfg.hashFontSize}px monospace`;
                    ctx.fillText(msg, x + cfg.dotRadius + 6, cy + 10);

                    // Author
                    if (commit.author) {
                        let author = commit.author;
                        if (author.length > 15) {
                            author = author.substring(0, 13) + "..";
                        }
                        ctx.fillStyle = cfg.dimTextColor;
                        ctx.font = `${cfg.hashFontSize - 1}px monospace`;
                        ctx.fillText(author, x + cfg.dotRadius + 6, cy + 22);
                    }
                });
            } else {
                // No commits loaded - draw a single placeholder dot
                const cy = cfg.startY + 30;
                ctx.beginPath();
                ctx.arc(x, cy, cfg.dotRadius, 0, Math.PI * 2);
                ctx.fillStyle = color;
                ctx.fill();

                // Show last commit info if available
                if (branch.last_commit_hash) {
                    ctx.fillStyle = cfg.dimTextColor;
                    ctx.font = `${cfg.hashFontSize}px monospace`;
                    ctx.textAlign = "left";
                    ctx.fillText(
                        branch.last_commit_hash.substring(0, 7),
                        x + cfg.dotRadius + 6,
                        cy - 4
                    );

                    if (branch.last_commit_message) {
                        let msg = branch.last_commit_message;
                        if (msg.length > 22) {
                            msg = msg.substring(0, 20) + "..";
                        }
                        ctx.fillStyle = cfg.textColor;
                        ctx.fillText(msg, x + cfg.dotRadius + 6, cy + 10);
                    }
                }
            }
        });

        // Set up resize observer for redraw
        if (!this._resizeObserver) {
            const parentEl = canvas.parentElement;
            if (parentEl) {
                this._resizeObserver = new ResizeObserver(() => {
                    this._drawGraph();
                });
                this._resizeObserver.observe(parentEl);
            }
        }
    }

    // ------------------------------------------------------------------
    // Event handlers
    // ------------------------------------------------------------------

    async _onProjectChange(ev) {
        const val = parseInt(ev.target.value, 10);
        this.state.projectId = val || null;
        this.state.branches = [];
        this.state.selectedBranch = null;
        if (this.state.projectId) {
            await this._loadData();
        }
    }

    _selectBranch(branch) {
        this.state.selectedBranch = branch.id;
    }
}

// ------------------------------------------------------------------
// Helper: draw a rounded rectangle
// ------------------------------------------------------------------

function _roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.arcTo(x + w, y, x + w, y + r, r);
    ctx.lineTo(x + w, y + h - r);
    ctx.arcTo(x + w, y + h, x + w - r, y + h, r);
    ctx.lineTo(x + r, y + h);
    ctx.arcTo(x, y + h, x, y + h - r, r);
    ctx.lineTo(x, y + r);
    ctx.arcTo(x, y, x + r, y, r);
    ctx.closePath();
}

registry.category("actions").add("devops_git_graph", DevopsGitGraph);
