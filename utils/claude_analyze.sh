#!/bin/bash
# Wrapper to run claude CLI in clean environment
export HOME=/opt/odooAL
export PATH="$HOME/.local/bin:$PATH"
exec "$HOME/.local/bin/claude" -p --output-format text < "$1"
