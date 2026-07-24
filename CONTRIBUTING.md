# Contributing to Komet Node

## Development environment

This repository ships a [devcontainer](.devcontainer/) that provisions the full
toolchain (Nix, the flake's `nix develop` shell, the Claude Code CLI, and the
GitHub CLI). Open the repo in a devcontainer-aware editor and everything below is
available out of the box.

## Using the GitHub CLI (`gh`)

`gh` is preinstalled in the devcontainer for creating pull requests, reviewing
issues, and other GitHub tasks from the terminal.

### Authenticating

Authenticate once with a personal access token:

```bash
gh auth login
```

Choose **GitHub.com** → **HTTPS** → **Paste an authentication token** when
prompted, then paste the token you create below.

Your credentials are stored in `~/.config/gh` inside the container, which is
backed by a named volume. This means **your token survives container rebuilds** —
you only need to authenticate once, not every time the container is recreated.

To check or reset your authentication at any time:

```bash
gh auth status   # show the current login
gh auth logout   # remove stored credentials
```

### Creating a token with minimal permissions

Create a **fine-grained** personal access token at **GitHub → Settings → Developer
settings → [Fine-grained personal access tokens](https://github.com/settings/personal-access-tokens/new)**
with the least privilege for your work:

- **Repository access** → *Only select repositories* → `runtimeverification/komet-node`
- **Contents** → *Read and write*
- **Pull requests** → *Read and write*
- **Issues** → *Read and write* (only if you triage issues)

`Metadata → Read` is selected automatically; leave everything else at *No access*.

## Signing commits

To sign commits made with `git`, add an SSH or GPG key of type **Signing key** to
your GitHub account.

> [!WARNING]
> Never place an SSH key registered as an **Authentication key** in the container.
> An authentication key grants full account-wide git access and would bypass the
> minimal permissions of your fine-grained PAT. Use a **signing-only** key here.

## Deploying a release

`scripts/deploy.sh` builds and publishes the komet-node release artifacts. It is
the single source of truth for the deployment steps: the `Release` workflow
(`.github/workflows/release.yml`) invokes its subcommands, and you run the same
subcommands by hand when CI is unavailable.

### What it does

The script has three subcommands:

| Subcommand  | Action |
| ----------- | ------ |
| `nix-cache` | Builds `.#komet-node`, pushes the full build closure to the `k-framework` Cachix cache, publishes the kup-installable binary to the `k-framework-binary` cache, then verifies the push and pin. Both caches are public; `binary` is a historical name, not an access level. |
| `docker`    | Builds the runtime image from `Dockerfile`, runs `komet-node --help` in it as a smoke test, then pushes it to Docker Hub. |
| `all`       | Runs `nix-cache` then `docker`. |

The image tag is `runtimeverificationinc/komet-node:ubuntu-jammy-<version>`,
where `<version>` comes from `package/version`.

### Prerequisites

Both subcommands run from a checkout of the revision you are releasing, with the
following tools on `PATH`:

- `nix-cache` needs `nix` and `cachix`. It fetches `kup` from
  `github:runtimeverification/kup` itself, so you do not install kup separately.
- `docker` needs `docker`.

Each subcommand checks its tools up front and exits with a clear message if one
is missing, before starting the build.

The devcontainer provides all of these: `nix` and `cachix` are installed into the
Nix profile, and the docker-in-docker feature supplies `docker`. If you added
these to an existing container, rebuild it ("Dev Containers: Rebuild Container")
so the new tools are present.

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

### Manual release while CI is down

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
