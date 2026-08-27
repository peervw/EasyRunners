# Architecture decision: webhook-first ephemeral runners

EasyRunners uses GitHub's official ephemeral runner rather than implementing job execution. A
`workflow_job` webhook is the low-latency demand signal. Periodic repository-scoped REST polling
repairs missed events and reconstructs queued demand after restart. GitHub does not expose a single
organization-wide endpoint that lists queued jobs by self-hosted runner labels, so organization
reconciliation enumerates repositories accessible to the App installation.

Repository registration scope and installation scope are deliberately separate. A personal account
has no account-wide self-hosted runner API, so each runner is registered only to the selected
repository whose queued job caused it to be created. The repository is persisted as a Docker label
for restart adoption and cleanup. Organization mode instead uses shared organization runner
registrations and GitHub runner groups for repository access.

A deployment may connect several GitHub accounts or organizations. GitHub private Apps belong to
one owner, so onboarding creates one App and installation per connected owner. Connection metadata
is stored in SQLite while each App key and webhook secret lives in its own mode-`0600` directory.
Workflow jobs, Docker containers, registration tokens, runner discovery, webhook replay IDs, and
repository scans carry the connection ID. Owners are unique within a deployment to prevent the same
GitHub delivery from being observed through overlapping installations. Runner pool minimums and
maximums remain deployment-wide.

The manager is a single async process. A reconciliation lock serializes capacity changes. Docker
labels are the source of truth for locally managed runner containers, and GitHub's runner list is
used to distinguish registering, idle, busy, and stale runners. No runner lifecycle state must be
restored from the embedded database.

Each capacity unit is a fresh container running GitHub's official `config.sh --ephemeral` and
`run.sh`. GitHub automatically de-registers it after one job; EasyRunners captures diagnostics and
removes the container. Socket-mode runners receive a unique Compose project namespace. Teardown and
a periodic age-gated janitor remove only containers and networks carrying that exact runner-owned
namespace; volumes require an explicit opt-in. Host-wide counts and the exact eligible targets are
visible before cleanup, and no global prune operation is used.

Docker socket mode is intentionally simple but gives the workflow root-equivalent control of the
Docker host. It is suitable only for trusted workflow code. The optional isolated backend instead
starts a private daemon inside a privileged runner container. This is the only backend that makes
arbitrary workflow-created Docker resources share the runner's lifecycle, although a separate VM is
still required for strong isolation from hostile code.

The embedded database holds control-plane authentication, GitHub connection metadata, one-time
setup state, connection-scoped webhook replay IDs, and bounded history. GitHub App keys remain files
with mode `0600` in the same persistent volume. One EasyRunners deployment may manage several App
installations but must run exactly one manager replica.
