# Deployment scripts

`deploy.sh` builds and publishes the komet-node release artifacts. It is the
single source of truth for the deployment steps: the `Release` workflow
(`.github/workflows/release.yml`) invokes its subcommands, and you run the same
subcommands by hand when CI is unavailable.

## What it does

The script has three subcommands:

| Subcommand  | Action |
| ----------- | ------ |
| `nix-cache` | Builds `.#komet-node`, pushes the full build closure to the `k-framework` Cachix cache, publishes the kup-installable binary to the `k-framework-binary` cache, then verifies the push and pin. Both caches are public; `binary` is a historical name, not an access level. |
| `docker`    | Builds the runtime image from `Dockerfile`, runs `komet-node --help` in it as a smoke test, then pushes it to Docker Hub. |
| `all`       | Runs `nix-cache` then `docker`. |

The image tag is `runtimeverificationinc/komet-node:ubuntu-jammy-<version>`,
where `<version>` comes from `package/version`.

## Prerequisites

Both subcommands run from a checkout of the revision you are releasing, with the
following tools on `PATH`:

- `nix-cache` needs `nix` and `cachix`. It fetches `kup` from
  `github:runtimeverification/kup` itself, so you do not install kup separately.
- `docker` needs `docker`.

Each subcommand checks its tools up front and exits with a clear message if one
is missing, before starting the build.

The project devcontainer (`.devcontainer/`) provides all of these: `nix` and
`cachix` are installed into the Nix profile, and the docker-in-docker feature
supplies `docker`. If you added these to an existing container, rebuild it
("Dev Containers: Rebuild Container") so the new tools are present.

Provide the secrets as environment variables. Everything else has a default (see
the `Shared configuration` block in `deploy.sh`), so a normal checkout needs no
further setup.

| Subcommand  | Required environment variables |
| ----------- | ------------------------------ |
| `nix-cache` | `CACHIX_PUBLIC_TOKEN`, `CACHIX_PRIVATE_KFB_TOKEN` |
| `docker`    | `DOCKERHUB_PASSWORD` |

`OWNER_REPO` and `REV` default to the current checkout's `origin` remote and
`HEAD`. Override them if you are publishing a revision other than the one checked
out. `DOCKERHUB_USERNAME`, `DOCKERHUB_NAMESPACE`, and `DOCKERHUB_REPO` default to
the release account and repository; override them only to publish elsewhere.

Both subcommands refuse to run if the working tree is not clean. CI builds from a
fresh checkout, so a manual build must too: uncommitted changes and untracked
files would otherwise be baked into the published image or flake build without
matching the `REV` they are published under. Commit, stash, or clean the tree
first. `git status --porcelain` ignores `.gitignore`d paths, so runtime artifacts
and dev caches do not count. Set `ALLOW_DIRTY=1` to override the check when you
deliberately want to publish an uncommitted state.

## Manual release while CI is down

The workflow also drafts, cleans up, and finalizes the GitHub release around the
deployment jobs. When you deploy by hand, run those `gh` steps yourself in this
order. `nix-cache` runs once per architecture (the workflow uses an `x86_64` and
an `ARM64` runner), so run it on a machine of each architecture that should be
cached.

```sh
VERSION=v$(cat package/version)

# 1. Draft the release.
gh release create "$VERSION" --draft --title "$VERSION" --target "$(git rev-parse HEAD)"

# 2. Deploy. Export the secrets first (see the table above).
#    Run nix-cache once per architecture; docker once.
./scripts/deploy.sh nix-cache
./scripts/deploy.sh docker

# 3. Finalize the release once every deployment has succeeded.
gh release edit "$VERSION" --draft=false

# If a deployment fails, remove the draft instead:
#   gh release delete "$VERSION" --yes --cleanup-tag
```
