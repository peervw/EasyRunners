# EasyRunners

EasyRunners is a small, self-hosted control plane for **ephemeral official GitHub Actions
runners**. It runs on an ordinary Linux Docker host—no Kubernetes, database server, queue, or fake
CI engine. GitHub remains responsible for workflows, scheduling, logs, secrets, and job results;
EasyRunners supplies fresh Docker runner capacity and removes it after one job.

## How it works

```text
workflow_job queued webhook        periodic REST reconciliation
              │                                  │
              └──────────────┬───────────────────┘
                             ▼
                    per-pool scheduler
                             │
            short-lived registration token from GitHub
                             │
                             ▼
       official actions/runner container --ephemeral
                             │
                 exactly one GitHub Actions job
                             │
                             ▼
          automatic de-registration + container removal
```

Every runner receives a new container filesystem and `_work` directory. Runner registration tokens
are requested just in time, never stored, and never written to logs. Containers are labeled so the
manager can adopt or clean them after a restart. See [the architecture decision](docs/ARCHITECTURE.md).

## Requirements

- x86_64 or ARM64 Ubuntu, or another modern Linux distribution
- Docker Engine and Docker Compose v2
- A browser-reachable HTTPS URL (public for webhooks, private/LAN is fine for polling-only mode)
- One or more trusted GitHub repositories, or a GitHub organization

Downloading and configuring GitHub's runner means accepting the applicable
[GitHub Customer Agreement](https://github.com/customer-terms).

## Quick start

### 1. Run Compose

```bash
git clone https://github.com/peervw/EasyRunners.git easy-runners
cd easy-runners
export PUBLIC_URL=https://runners.example.com
docker compose up -d --build
docker compose logs manager
```

That is the only Compose file. It always builds the manager, standard runner, and Rust runner from
the checked-out commit. No container registry login or separately published images are required.

In Dokploy, choose `compose.yaml`, set `PUBLIC_URL` in the environment, and deploy. The Compose file
already declares the persistent data volume and Docker socket; do not add another volume in the
advanced settings. Route the public HTTPS domain to the `manager` service on port 8080.

### 2. Connect GitHub

The first boot prints an `auth.bootstrap_password` JSON event exactly once. Open `PUBLIC_URL`, sign
in with that password, and replace it with a password of at least 14 characters. Paste a repository
URL such as `https://github.com/peervw/prediction-market`, then select **Connect GitHub**:

1. EasyRunners detects the owner, repository, and account type from the URL.
2. GitHub creates the preconfigured private App and asks where to install it.
3. Choose **Only select repositories** and select every trusted repository that should use these
   runners.
4. Return to the dashboard; all selected repositories appear under **GitHub connection**.

For personal accounts, EasyRunners creates repository-specific runners: a job from repository A
cannot run on a runner registered to repository B. One App installation can still serve every
repository selected on GitHub. For organizations, the optional shared-runner mode registers at the
organization level and should be restricted with GitHub runner groups.

### 3. Change one workflow line

In each existing workflow job, replace its hosted runner line:

```yaml
# before
runs-on: ubuntu-latest

# Docker jobs
runs-on: [self-hosted, linux, x64, docker]

# Rust jobs
runs-on: [self-hosted, linux, x64, rust]
```

Push the change. The dashboard shows the job as queued, creates a fresh runner for that repository,
shows it as busy, and removes its container and workspace when the job finishes. The dashboard's
**Use an existing workflow** section generates the exact line for every configured pool and diagnoses
jobs whose labels do not match a pool.

If login expires or the browser is closed during setup, sign in again and use **Continue GitHub
installation**. **Start over** clears the local connection; remove an abandoned App separately in
GitHub settings if desired.

EasyRunners stores the generated App private key and webhook secret in the `easy-runners-data`
volume with mode `0600`. It verifies that GitHub's returned installation belongs to the configured
target before accepting it.

The manager binds `127.0.0.1:8080` by default. Dokploy/Portainer deployments can route to the
`manager` service on port 8080 through the Compose network. For a host reverse proxy, proxy to
`http://127.0.0.1:8080`. Do not expose port 8080 directly to the internet without HTTPS.

### Private or LAN installation

Turn off **Instant scaling with a public HTTPS webhook** during onboarding, or set
`WEBHOOK_ENABLED=false`. GitHub does not need inbound network access to the manager; queued jobs are
discovered through periodic REST polling. Scale-up can therefore take up to
`QUEUE_POLL_INTERVAL` seconds. `PUBLIC_URL` remains canonical and is never inferred from request
headers.

### Password recovery

```bash
docker compose exec manager easyrunners admin reset-password
```

The command invalidates all sessions and prints a new one-time password.

## GitHub permissions

The manifest requests only the permissions required by the selected scope:

| Scope | GitHub App permission | Why |
|---|---|---|
| Selected repositories | Actions: read | Receive `workflow_job` and reconcile workflow jobs |
| Selected repositories | Administration: write | Create/list/delete repository-specific runner registrations |
| Organization | Actions: read | Observe jobs in installed repositories |
| Organization | Self-hosted runners: write | Create/list/delete organization runners |

The App subscribes only to `workflow_job`. GitHub's registration token expires after one hour;
EasyRunners obtains a fresh token for the requesting repository for every container. The dashboard
shows GitHub's selected repository list and warns when the installation is granted **All
repositories**. Organization owners should also use runner groups and selected-repository policies.

### Manual GitHub App or PAT setup

For headless installations set `GITHUB_AUTH_MODE=app` and provide `GITHUB_SCOPE`, target fields,
`GITHUB_APP_ID`, `GITHUB_INSTALLATION_ID`, and `GITHUB_APP_PRIVATE_KEY_PATH`. Mount the PEM into the
manager container if it is not in `/data`.

For development only, set `GITHUB_AUTH_MODE=pat` and `GITHUB_TOKEN`. Classic PATs require `repo` for
private repositories or `admin:org` for organization runners. GitHub Apps are recommended because
their installation tokens are short-lived and repository access is explicit.

## Runner pools and labels

Image defaults live in `config.yaml`; secrets remain in the environment or protected data volume.
Pools can also be added and edited in the dashboard or imported/exported as YAML. Dashboard
overrides persist in `/data` and take precedence after restart. A pool is an independently bounded
capacity class:

```yaml
runner_pools:
  default:
    labels: [self-hosted, linux, x64, docker]
    min: 0
    max: 5
    cpu: 4
    memory: 8g
    job_timeout: 3600
    max_lifetime: 3900
    docker_mode: socket

  rust:
    labels: [self-hosted, linux, x64, rust]
    min: 0
    max: 5
    priority: 10
    cpu: 4
    memory: 8g
    docker_mode: none

  deploy:
    labels: [self-hosted, linux, x64, deploy]
    min: 0
    max: 1
    priority: 10
    cpu: 2
    memory: 4g
    docker_mode: none
    environment_from: [DEPLOY_TOKEN]
```

GitHub automatically assigns `self-hosted`, `Linux`, and `X64`; EasyRunners passes custom labels to
`config.sh`. Matching is case-insensitive. When multiple pools match, the pool with the fewest extra
labels wins, followed by highest priority and pool name. Duplicate label sets are rejected at
startup. Give sensitive pools a unique discriminator such as `deploy`.

Use a pool with:

```yaml
runs-on: [self-hosted, linux, x64, docker]
```

See [Python CI](examples/python-ci.yaml), [Rust CI](examples/rust-ci.yaml),
[GHCR build](examples/docker-ghcr.yaml), and [deployment](examples/deploy.yaml) examples. The
dashboard generator creates equivalent copy-paste-ready workflows for Python, Node, Rust, Docker,
and isolated deploy pools.

### Rust runners and caching

The `rust` label automatically selects `RUST_RUNNER_IMAGE`. That dedicated image inherits the
official ephemeral runner and adds rustup, the stable minimal Rust toolchain, Clippy, rustfmt,
`clang`, `lld`, CMake, OpenSSL headers, `pkg-config`, and Protobuf. Its larger layers remain cached
on the Docker host and do not need to be downloaded for each ephemeral container.

Rust workflows use GitHub's remote Actions cache through `Swatinem/rust-cache`, pinned to an
immutable commit. It keys Cargo downloads and dependency build artifacts using the compiler,
Cargo manifests and lockfiles, toolchain files, and relevant build environment. No Cargo directory
is shared directly between repositories or runner containers.

Set `RUST_TOOLCHAIN` before building the images to bake a specific channel or version instead of
`stable`. A repository-level `rust-toolchain.toml` remains the source of truth; when it asks for a
toolchain not present in the image, install that toolchain in the workflow or rebuild the Rust image
with the same pin.

For an existing installation whose dashboard pool configuration already overrides `config.yaml`,
click **Rust preset**, review the capacity, and save it. For a new installation the `rust` pool is
present automatically with `min: 0`, so it consumes no runner capacity until a matching job queues.

### Connect a Rust repository

1. In the GitHub App installation, ensure the Rust repository is selected. Reconnecting EasyRunners
   is not necessary when adding another repository to the same installation.
2. Confirm that the dashboard readiness check finds the Rust image and that the `rust` pool appears.
3. In the target repository, copy `examples/rust-ci.yaml` to `.github/workflows/rust-ci.yml`, or copy
   the Rust `runs-on` line from the dashboard.
4. Commit and push the workflow. A job requesting
   `[self-hosted, linux, x64, rust]` queues, EasyRunners starts one ephemeral Rust container, and the
   container disappears after the job.

The generated workflow assumes a committed `Cargo.lock` and a workspace compatible with
`--all-features`. Remove `--locked`, `--workspace`, or `--all-features` if the repository intentionally
uses a different layout.

### Scaling behavior

- `workflow_job` webhooks trigger immediate reconciliation.
- REST polling enumerates the App installation's selected repositories to repair missed events and
  restore demand after restart.
- Personal-account capacity is repository-bound. Pool maximums apply across all selected
  repositories, and the oldest queued matching jobs receive available slots first.
- Organization polling enumerates App installation repositories because GitHub has no single API
  endpoint for all queued organization jobs grouped by runner labels.
- Starting containers count toward capacity. Busy runners are never stopped for ordinary scale-down.
- Idle excess waits for `idle_timeout` and an assignment grace period.
- Dashboard pre-warming is a temporary desired-capacity floor, not a permanent config change.
- Exactly one manager replica is supported.

## Docker builds and security

`docker_mode: socket` mounts `/var/run/docker.sock` and supplies Docker CLI in the runner image. This
is convenient for Docker builds, but **Docker socket access is root-equivalent access to the host**.
A workflow can inspect containers, mount host paths, and read manager secrets. Filesystem isolation
between jobs does not mitigate a malicious workflow.

Therefore:

1. Never run arbitrary or unreviewed fork pull-request code on these runners.
2. Restrict organization runners to trusted repositories and workflows.
3. Keep generic CI and production deployment in different pools and preferably on different hosts.
4. Expose deployment credentials only through the deploy pool.
5. Use `docker_mode: none` when jobs do not need Docker.
6. For hostile multi-tenant workloads, use separate VMs or ARC/scale sets rather than this socket
   architecture. Privileged Docker-in-Docker is deliberately not implemented in v1.

Runner containers are otherwise non-privileged, drop capabilities, set `no-new-privileges`, have no
published ports, run as UID/GID 1001, and receive configurable CPU, memory, PID, job, and lifetime
limits. Pool-defined host mounts deliberately weaken isolation and must be reviewed.

## Dashboard, API, and metrics

All management data is authenticated. Browser mutations also require CSRF validation. Create
revocable API tokens from the dashboard and send:

```text
Authorization: Bearer ert_...
```

Endpoints:

- `GET /health` — intentionally minimal unauthenticated liveness
- `GET /api/status`, `/api/runners`, `/api/pools`, `/api/history`
- `GET /api/readiness`, `/api/version`, and `/api/pools/{pool}/workflow`
- `GET /api/jobs` for queued and in-progress workflow jobs
- `PUT|DELETE /api/pools/{pool}` and YAML pool import/export endpoints
- `POST /api/pools/{pool}/scale` with `{"desired": 2, "ttl_seconds": 600}`
- `POST /api/readiness/test-runner` to pre-warm one runner for five minutes
- `POST /api/reconcile`
- `GET|POST /api/auth/tokens` and `DELETE /api/auth/tokens/{id}`
- `GET /api/github`, setup/disconnect routes
- `POST /webhooks/github` — HMAC-SHA256 signed GitHub deliveries only
- `GET /metrics` — Prometheus format, requiring session or bearer token

Structured events include `runner.created`, `runner.online`, `runner.job_started`,
`runner.job_finished`, `runner.removed`, `github.api_error`, and `scheduler.reconcile`. Diagnostic
archives are retained under `/data/runner-logs` for seven days by default. Treat them as sensitive.

## Updating

The dashboard compares the pinned runner version with GitHub's latest release and shows when they
differ. It deliberately does not update automatically because runner releases use a progressive
rollout.

Update and rebuild from source:

```bash
git pull --ff-only
docker compose up -d --build
```

The runner is pinned and built with automatic updates disabled to avoid downloading an update for
every ephemeral container. To update it, change `RUNNER_VERSION`, `RUNNER_SHA256_X64`, and
`RUNNER_SHA256_ARM64` in `.env` using the checksums published on the official
[actions/runner releases page](https://github.com/actions/runner/releases), then rebuild. GitHub uses
a progressive rollout, so verify the expected version in the repository or organization's **Add new
self-hosted runner** page before updating.

## Backup and restore

Runner containers and workspaces require no backup. Back up the named data volume to retain the App
private key, webhook secret, administrator hash, API tokens, and history:

```bash
docker run --rm -v easy-runners_easy-runners-data:/data:ro \
  -v "$PWD":/backup ubuntu:24.04 \
  tar czf /backup/easy-runners-data.tgz -C /data .
```

Restore only while the manager is stopped. Protect backups as authentication secrets. If the volume
is lost, create a new App through onboarding or supply manual credentials; runner capacity itself is
reconstructed from Docker and GitHub. Rotate App private keys in GitHub and replace the stored PEM
after any suspected disclosure.

## Troubleshooting

- **No runner appears with min 0:** queue a job whose complete `runs-on` labels match a pool, or
  pre-warm one runner from the dashboard.
- **Job stays queued:** compare job labels with the effective pool labels and verify the repository
  appears in the dashboard's GitHub repository list or organization runner group.
- **GitHub disconnected:** inspect `docker compose logs manager` for permission or installation-token
  errors. Reapprove changed App permissions in GitHub.
- **Webhook rejected:** ensure `PUBLIC_URL` reaches `/webhooks/github` unchanged and the reverse proxy
  does not rewrite the request body.
- **Docker unavailable:** verify the socket mount, Docker daemon, and `DOCKER_HOST`.
- **Runner registers then exits:** inspect `/data/runner-logs` and verify the pinned runner version is
  accepted by GitHub.
- **Organization polling is slow:** rely on webhooks for fast scaling, increase the full sweep
  interval, or reduce the App's installed repository selection.

Development commands:

```bash
uv sync --all-groups
ALLOW_INSECURE_PUBLIC_URL=true DATA_DIR=./data uv run easyrunners serve
uv run pytest
uv run ruff check .
uv run mypy src
```

## License

Apache-2.0. The GitHub Actions runner is downloaded separately and governed by GitHub's terms.
