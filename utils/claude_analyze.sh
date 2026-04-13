#!/bin/bash
export HOME=/opt/odooAL
export PATH="$HOME/.local/bin:$PATH"
export TERM=xterm-256color
export LANG=en_US.UTF-8
export NO_COLOR=1
export CLAUDE_CODE_DISABLE_AUTOUPDATE=1
# Detach from parent process group to avoid systemd fd inheritance issues
exec setsid "$HOME/.local/bin/claude" -p --output-format text < "$1" 2>&1
