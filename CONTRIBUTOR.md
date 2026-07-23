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

Prefer **fine-grained personal access tokens** — they let you grant only the
access you actually need, scoped to a single repository, with a short expiry.
Please generate tokens with the **least privilege** required for your work; avoid
broad "classic" tokens with the full `repo` scope unless there is no alternative.

1. Go to **GitHub → Settings → Developer settings →
   [Fine-grained personal access tokens](https://github.com/settings/personal-access-tokens/new)**.
2. Give the token a descriptive name and set the **shortest expiry** that fits
   your workflow.
3. Under **Repository access**, choose **Only select repositories** and pick just
   `runtimeverification/komet-node` (or your fork) — not "All repositories".
4. Under **Permissions → Repository permissions**, grant only what you need. For a
   typical contributor workflow that is:
   - **Contents** → *Read and write* (clone, pull, push branches)
   - **Pull requests** → *Read and write* (open and update PRs)
   - **Issues** → *Read and write* (only if you triage or comment on issues)

   `Metadata → Read` is required and is selected automatically. Leave every other
   permission at **No access**.
5. Click **Generate token** and copy it — GitHub shows it only once. Paste it into
   the `gh auth login` prompt above.

> [!TIP]
> Start with the narrowest set of permissions and add more only if a command
> fails with an authorization error. A token that can do less is a smaller risk
> if it ever leaks.
