# Architecture decision: webhook-first ephemeral runners

EasyRunners uses GitHub's official ephemeral runner rather than implementing job execution. A
`workflow_job` webhook is the low-latency demand signal. Periodic repository-scoped REST polling
repairs missed events and reconstructs queued demand after restart. GitHub does not expose a single
organization-wide endpoint that lists queued jobs by self-hosted runner labels, so organization
reconciliation enumerates repositories accessible to the App installation.

Repository registration scope and installation scope are deliberately separate. A personal account
has no account-wide self-hosted runner API, so each runner is registered only to the selected
repository whose queued job caused it to be created. The repository is persisted as a Docker label
for restart adoption and cleanup. Pool maximums remain deployment-wide. Organization mode instead
uses shared organization runner registrations and GitHub runner groups for repository access.

The manager is a single async process. A reconciliation lock serializes capacity changes. Docker
labels are the source of truth for locally managed runner containers, and GitHub's runner list is
used to distinguish registering, idle, busy, and stale runners. No runner lifecycle state must be
restored from the embedded database.

Each capacity unit is a fresh container running GitHub's official `config.sh --ephemeral` and
`run.sh`. GitHub automatically de-registers it after one job; EasyRunners captures diagnostics and
removes the container. The default Docker socket mode is intentionally simple but gives the workflow
root-equivalent control of the Docker host. It is suitable only for trusted workflow code.

The embedded database holds control-plane authentication, one-time setup state, webhook replay IDs,
and bounded history. GitHub App keys remain files with mode `0600` in the same persistent volume.
One EasyRunners deployment manages one GitHub App installation and must run exactly one manager
replica. That installation may contain multiple selected repositories owned by the same account.
