# Microsoft's official devcontainer base (Ubuntu 24.04), pinned by digest for
# reproducibility. It ships a non-root `vscode` user (UID 1000) with passwordless
# sudo plus the common dev tools (git, curl, sudo, ca-certificates, openssh-client),
# so no manual user creation is needed here.
FROM mcr.microsoft.com/devcontainers/base:ubuntu24.04@sha256:4bcb1b466771b1ba1ea110e2a27daea2f6093f9527fb75ee59703ec89b5561cb

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# The base already provides git/sudo/ca-certificates/curl/openssh-client; we only
# strictly need xz-utils for the Nix installer tarball. The rest are listed
# defensively (apt is idempotent and skips already-present packages).
RUN apt-get update && apt-get install -y --no-install-recommends \
        git sudo ca-certificates curl less xz-utils openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Pre-create /nix owned by the vscode user and a system-wide nix.conf. Two things to note:
#   - Sandboxing relies on CAP_SYS_ADMIN (sethostname etc.), which container
#     builds don't grant — without `sandbox = false` both the Nix self-install
#     and every subsequent `nix profile install` fail.
#   - The K Framework binary caches let `kup install k` pull prebuilt K binaries
#     instead of compiling from source. Single-user Nix (vscode owns /nix) honors
#     these substituters from the system config, so no per-user nix.conf needed.
RUN mkdir -m 0755 /nix \
    && chown vscode:vscode /nix \
    && mkdir -p /etc/nix \
    && printf '%s\n' \
        'sandbox = false' \
        'filter-syscalls = false' \
        'experimental-features = nix-command flakes' \
        'substituters = https://cache.nixos.org https://k-framework.cachix.org https://k-framework-binary.cachix.org' \
        'trusted-public-keys = cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY= k-framework.cachix.org-1:jeyMXB2h28gpNRjuVkehg+zLj62ma1RnyyopA/20yFE= k-framework-binary.cachix.org-1:pJedQ8iG19BW3v/DMMmiRVtwRBGO3fyMv2Ws0OpBADs=' \
        > /etc/nix/nix.conf

USER vscode
WORKDIR /home/vscode

# Single-user Nix install — pinned to a specific release for reproducibility
# and supply-chain safety. Update the version when upgrading Nix.
ARG NIX_VERSION=2.28.3
RUN curl -fsSL "https://releases.nixos.org/nix/nix-${NIX_VERSION}/install" -o /tmp/nix-install \
    && sh /tmp/nix-install --no-daemon --no-channel-add \
    && rm /tmp/nix-install

ENV PATH=/home/vscode/.nix-profile/bin:/home/vscode/.npm-global/bin:$PATH

# Pin the `nixpkgs` flake to nixos-25.05 (matches the project's flake.nix), then
# install only the container-level bootstrap/runtime tooling via Nix:
#   - direnv + nix-direnv → auto-load and cache the flake's `nix develop` shell on
#                           entry to /workspace; this is what actually provides the
#                           project toolchain (python, uv, make, wabt, K Framework),
#                           so those live in flake.nix's devShell, not here.
#   - nodejs_22           → host for the Claude Code CLI (run as `claude` from
#                           anywhere, so it stays globally on PATH, not in the shell)
RUN . /home/vscode/.nix-profile/etc/profile.d/nix.sh \
    && nix registry add nixpkgs github:NixOS/nixpkgs/nixos-25.05 \
    && nix profile install \
        nixpkgs#direnv \
        nixpkgs#nix-direnv \
        nixpkgs#nodejs_22

# Claude Code CLI — installed to a per-user prefix so root isn't required.
RUN . /home/vscode/.nix-profile/etc/profile.d/nix.sh \
    && npm config set prefix /home/vscode/.npm-global \
    && npm install -g @anthropic-ai/claude-code@2.1.175

# Wire up nix-direnv, then hook direnv into interactive shells. Order matters:
# Nix must be sourced before the direnv hook so the profile PATH is in place when
# direnv evaluates the flake.
RUN mkdir -p /home/vscode/.config/direnv \
    && echo 'source $HOME/.nix-profile/share/nix-direnv/direnvrc' > /home/vscode/.config/direnv/direnvrc \
    && printf '%s\n' \
        '. /home/vscode/.nix-profile/etc/profile.d/nix.sh' \
        'eval "$(direnv hook bash)"' \
        >> /home/vscode/.bashrc

WORKDIR /workspace
