from prometheus_client import Counter, Gauge, Histogram

RUNNERS = Gauge(
    "github_runner_manager_runners",
    "Managed runners by pool and state",
    ["pool", "state"],
)
QUEUED_JOBS = Gauge(
    "github_runner_manager_jobs_queued",
    "Queued jobs matched to a runner pool",
    ["pool"],
)
JOBS_COMPLETED = Counter(
    "github_runner_manager_jobs_completed_total",
    "Completed workflow jobs observed",
    ["pool", "conclusion"],
)
JOB_FAILURES = Counter(
    "github_runner_manager_job_failures_total",
    "Failed workflow jobs observed",
    ["pool"],
)
RUNNER_CREATION_FAILURES = Counter(
    "github_runner_manager_runner_creation_failures_total",
    "Runner container creation failures",
    ["pool"],
)
GITHUB_API_FAILURES = Counter(
    "github_runner_manager_github_api_failures_total",
    "GitHub API request failures",
    ["operation", "status"],
)
GITHUB_RATE_LIMIT_REMAINING = Gauge(
    "github_runner_manager_github_rate_limit_remaining",
    "Remaining GitHub REST API requests reported by GitHub",
)
WEBHOOK_FAILURES = Counter(
    "github_runner_manager_webhook_validation_failures_total",
    "Rejected GitHub webhook deliveries",
    ["reason"],
)
RECONCILE_DURATION = Histogram(
    "github_runner_manager_reconcile_duration_seconds",
    "Scheduler reconciliation duration",
    ["reason"],
)
