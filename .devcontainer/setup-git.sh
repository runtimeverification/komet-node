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

if [ -f "${host_gitconfig}" ]; then
    # Host provided a ~/.gitconfig (bind-mounted as a regular file): inherit it.
    printf '[include]\n\tpath = %s\n' "${host_gitconfig}" > "${local_gitconfig}"
    echo "setup-git: global config includes host identity from ${host_gitconfig}"
else
    # No host ~/.gitconfig — the missing bind source is materialized as an empty
    # directory, which must NOT be included. Leave an empty writable global.
    : > "${local_gitconfig}"
    echo "setup-git: no host ~/.gitconfig found; created empty ${local_gitconfig}"
    echo "setup-git: set your identity with 'git config --global user.name ...' / user.email"
fi
