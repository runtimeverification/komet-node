#!/usr/bin/env bash
# Wire up a writable global git config that inherits the host user's identity.
#
# devcontainer.json bind-mounts the host ~/.gitconfig read-only at
# /home/vscode/.gitconfig and sets GIT_CONFIG_GLOBAL=/home/vscode/.gitconfig.local.
# Git therefore writes global config to the (writable) .gitconfig.local while
# still reading the host's name/email/aliases through the include set up below.
#
# The result: every user of this container can `git commit` with their own host
# identity and run `git config --global ...` inside the container, without the
# EBUSY breakage a directly-writable bind mount over ~/.gitconfig would cause.
#
# Adapted from trailofbits/claude-code-devcontainer's post_install.py. Invoked
# from devcontainer.json's postCreateCommand. Idempotent — safe to re-run.
set -euo pipefail

host_gitconfig="/home/vscode/.gitconfig"
local_gitconfig="/home/vscode/.gitconfig.local"

# Ensure the writable global config exists without truncating it — a previous run
# (or the user) may have written real settings here (e.g. core.editor), and this
# script must not clobber them.
touch "${local_gitconfig}"

if [ -f "${host_gitconfig}" ]; then
    # Host provided a ~/.gitconfig (bind-mounted as a regular file): inherit it.
    # Set just the include.path key via `git config` so any other keys the user
    # already wrote to the global config are preserved. --replace-all collapses
    # the entry to a single value, keeping the operation idempotent across re-runs.
    git config --file "${local_gitconfig}" --replace-all include.path "${host_gitconfig}"
    echo "setup-git: global config includes host identity from ${host_gitconfig}"
else
    # No host ~/.gitconfig — the missing bind source is materialized as an empty
    # directory, which must NOT be included. Drop any stale include.path we may
    # have added on an earlier run, but leave every other key untouched.
    git config --file "${local_gitconfig}" --unset-all include.path 2>/dev/null || true
    echo "setup-git: no host ~/.gitconfig found; using empty ${local_gitconfig}"
    echo "setup-git: set your identity with 'git config --global user.name ...' / user.email"
fi
