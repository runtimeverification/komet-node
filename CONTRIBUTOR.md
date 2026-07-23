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
