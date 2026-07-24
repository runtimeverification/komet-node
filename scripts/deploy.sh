#!/usr/bin/env bash
set -euo pipefail

# NOTE: do not run this script under `set -x` / `bash -x`: the cachix and Docker
# Hub tokens flow through the environment and the `docker login` pipe, and shell
# tracing would print them to the logs.

# deploy.sh -- build and publish komet-node release artifacts.
#
# This is the single source of truth for the deployment steps that the Release
# workflow (.github/workflows/release.yml) performs. The workflow calls the
# subcommands below, and you can run the exact same steps by hand when CI is
# unavailable. See CONTRIBUTOR.md ("Deploying a release") for the runbook.
#
# Subcommands:
#   nix-cache   Build komet-node and push it to both Nix caches (the
#               k-framework closure + the k-framework-binary kup binary).
#   docker      Build the Docker image, smoke-test it, and push to Docker Hub.
#   all         Run nix-cache then docker.
#
# Usage:
#   scripts/deploy.sh <nix-cache|docker|all>

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

# --- Shared configuration --------------------------------------------------
# Defaults live here (not in the workflow) so the manual and CI paths stay
# identical. Anything already set in the environment wins, which lets the
# workflow inject secrets and lets a developer override values locally.

# Keep the Nix daemon from garbage-collecting store paths mid-build, and enable
# flakes for every nix invocation (the build, the kup build, and the builds kup
# runs internally) so we do not depend on flakes being globally enabled.
export GC_DONT_GC="${GC_DONT_GC:-1}"
export NIX_CONFIG="${NIX_CONFIG:-extra-experimental-features = nix-command flakes}"

# owner/repo and revision, used by kup publish + the cachix pin check. Default
# to the current checkout so manual runs work without extra setup. The first
# sed clause strips any `user:token@` userinfo from an https remote before
# parsing, so a credential embedded in the remote URL never ends up in
# OWNER_REPO (which is later echoed into the deploy log by check-cachix-pin.sh).
OWNER_REPO="${OWNER_REPO:-$(git remote get-url origin | sed -E 's#^[a-z]+://[^@/]*@#https://#; s#(git@github.com:|https://github.com/)##; s#\.git$##')}"
REV="${REV:-$(git rev-parse HEAD)}"
export OWNER_REPO REV

# Docker Hub coordinates. Only DOCKERHUB_PASSWORD is a secret.
DOCKERHUB_USERNAME="${DOCKERHUB_USERNAME:-rvdockerhub}"
DOCKERHUB_NAMESPACE="${DOCKERHUB_NAMESPACE:-runtimeverificationinc}"
DOCKERHUB_REPO="${DOCKERHUB_REPO:-komet-node}"

KOMET_NODE_VERSION="$(cat package/version)"

# Fail early with a clear message if a required secret is missing.
require_env() {
  local missing=0 var
  for var in "$@"; do
    if [ -z "${!var:-}" ]; then
      echo "error: required environment variable '${var}' is not set" >&2
      missing=1
    fi
  done
  [ "${missing}" -eq 0 ] || exit 1
}

# Fail before the (long) build if a required tool is not on PATH, so a missing
# dependency surfaces immediately rather than after minutes of building. kup is
# not checked here: nix-cache fetches it via `nix build` at run time.
require_cmd() {
  local missing=0 cmd
  for cmd in "$@"; do
    if ! command -v "${cmd}" >/dev/null 2>&1; then
      echo "error: required command '${cmd}' not found on PATH" >&2
      missing=1
    fi
  done
  [ "${missing}" -eq 0 ] || exit 1
}

# CI always builds from a fresh checkout at a known revision. A manual run
# builds from your working tree, so uncommitted changes to tracked files (which
# a dirty flake build embeds) and untracked files (which `docker build .` copies
# into the image) would silently end up in the published artifacts -- and would
# not match the REV they are published under. Require a clean tree so a manual
# deploy is byte-for-byte what CI would produce. `git status --porcelain` skips
# .gitignored paths, so runtime artifacts and dev caches do not trip this.
require_clean_worktree() {
  [ -n "${ALLOW_DIRTY:-}" ] && return 0
  if [ -n "$(git status --porcelain)" ]; then
    echo "error: working tree is not clean." >&2
    echo "CI deploys from a fresh checkout; these changes would be baked into the" >&2
    echo "published artifacts but would not match REV (${REV}):" >&2
    git status --short >&2
    echo "Commit, stash, or clean them -- or set ALLOW_DIRTY=1 to override." >&2
    exit 1
  fi
}

# --- nix-cache -------------------------------------------------------------
# Build the komet-node derivation once, then push it to both Nix caches: the
# k-framework cache (full build closure, via `cachix push`) and the
# k-framework-binary cache (the kup-installable binary, via `kup publish`).
# Both caches are public; "binary" is a historical name, not an access level.
# Both pushes reuse the build output already present in this machine's Nix
# store, so the derivation is never built twice.
deploy_nix_cache() {
  require_cmd nix nix-store cachix git curl jq
  require_clean_worktree
  require_env CACHIX_PUBLIC_TOKEN CACHIX_PRIVATE_KFB_TOKEN

  local komet_node drv
  komet_node="$(nix build .#komet-node --no-link --print-out-paths)"
  drv="$(nix-store --query --deriver "${komet_node}")"

  # Push the full build closure to the public k-framework cache.
  echo ":: pushing build closure to the public k-framework cache"
  export CACHIX_AUTH_TOKEN="${CACHIX_PUBLIC_TOKEN}"
  nix-store --query --requisites --include-outputs "${drv}" | cachix push k-framework

  # Publish the binary to the k-framework-binary cache. kup reuses the
  # store paths built above.
  echo ":: publishing komet-node binary to the k-framework-binary cache"
  export CACHIX_AUTH_TOKEN="${CACHIX_PRIVATE_KFB_TOKEN}"
  export PATH="$(nix build github:runtimeverification/kup --no-link --print-out-paths)/bin:${PATH}"
  kup publish k-framework-binary .#komet-node --keep-days 180

  # Cachix has not been reliably honoring the `cachix pin` requests kup makes
  # under the hood. Verify the push and pin explicitly.
  bash .github/scripts/check-cachix-pin.sh
}

# --- docker ----------------------------------------------------------------
# Build the runtime image, smoke-test it, then push it to Docker Hub.
deploy_docker() {
  require_cmd docker git
  require_clean_worktree
  require_env DOCKERHUB_PASSWORD

  local tag k_version
  tag="${DOCKERHUB_NAMESPACE}/${DOCKERHUB_REPO}:ubuntu-jammy-${KOMET_NODE_VERSION}"
  k_version="$(cat deps/k_release)"

  echo ":: building Docker image ${tag} (K ${k_version})"
  docker build . --no-cache --tag "${tag}" --build-arg K_VERSION="${k_version}"

  echo ":: smoke-testing the image"
  docker run --rm "${tag}" komet-node --help

  echo ":: pushing ${tag} to Docker Hub"
  # Log out on the way out (success or failure) so the base64 credential
  # `docker login` writes to ~/.docker/config.json does not linger on the
  # self-hosted runner or on the operator's machine. EXIT (not RETURN) so it
  # still fires when `set -e` aborts on a failed login or push.
  trap 'docker logout >/dev/null 2>&1 || true' EXIT
  echo "${DOCKERHUB_PASSWORD}" | docker login --username "${DOCKERHUB_USERNAME}" --password-stdin
  docker image push "${tag}"
}

usage() {
  sed -n '8,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

main() {
  case "${1:-}" in
    nix-cache) deploy_nix_cache ;;
    docker)    deploy_docker ;;
    all)       deploy_nix_cache; deploy_docker ;;
    -h | --help | help | "")
      usage
      [ -n "${1:-}" ] # exit non-zero when no subcommand was given
      ;;
    *)
      echo "error: unknown subcommand '${1}'" >&2
      echo >&2
      usage >&2
      exit 1
      ;;
  esac
}

main "$@"
