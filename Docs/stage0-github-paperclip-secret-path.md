# Stage 0 GitHub and Paperclip Secret Path

Scope: Government Watchdog Stage 0 automation for the Alpine-only backend and website repos.

## Current credential path

GOV agents and local self-hosted runners should use the host-local GitHub CLI authentication already present in the macOS keyring.

- Backend repo: `xXKillerNoobYT/Government-watchdog`
- Website repo: `xXKillerNoobYT/Government-watchdog-website`
- Required commands: `gh repo view`, `gh workflow run`, `gh run view`, `gh run list`, `git clone`, `git fetch`, `git push` when an issue explicitly requires a push
- Minimum GitHub token scopes for the local keyring account: `repo` and `workflow`

Do not copy the GitHub token into repo files, Obsidian docs, Paperclip issue comments, runner logs, workflow output, or agent instructions.

## Paperclip secret fallback

Only create a Paperclip company secret if a GOV agent or workflow must run somewhere that cannot use the host-local `gh` keyring.

When needed, the secret must be owned by the Government Watchdog company, not referenced from another Paperclip company. Use a GOV-local secret record with this non-secret name:

`GOV_GITHUB_TOKEN`

Bind it into an agent adapter environment as:

```json
{
  "env": {
    "GITHUB_TOKEN": {
      "type": "secret_ref",
      "secretId": "<GOV-owned Paperclip secret UUID>",
      "version": "latest"
    }
  }
}
```

The token value must be entered only through Paperclip's board-level secrets UI/API. Agents should never paste, print, or commit it.

## Safety checks

- GOV repos remain private until explicit owner/publication approval.
- Prefer runner labels scoped to GOV repos: `government-watchdog`, `gov-backend`, and `gov-website`.
- GitHub Actions secrets are not required for the current local-runner smoke workflow.
- Any future workflow that needs credentials should consume GitHub-provided `GITHUB_TOKEN` or a repo/environment secret, never a plaintext value.
