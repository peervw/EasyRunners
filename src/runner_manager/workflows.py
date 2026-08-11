from __future__ import annotations

from runner_manager.models import RunnerPoolConfig


def workflow_for(pool_name: str, pool: RunnerPoolConfig, template: str) -> str:
    labels = ", ".join(["self-hosted", "linux", "x64", *pool.custom_labels])
    jobs: dict[str, str] = {
        "python": """      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: python -m pip install -r requirements.txt
      - run: pytest
""",
        "node": """      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: '22'
          cache: npm
      - run: npm ci
      - run: npm test
""",
        "docker": """      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - run: docker build -t my-app:${{ github.sha }} .
""",
        "rust": """      - uses: actions/checkout@v4
      - name: Toolchain information
        run: rustc --version --verbose && cargo --version
      - name: Restore Cargo cache
        uses: Swatinem/rust-cache@49a0bdc70d2e1b713ca9e2869b211fcce03d3c1c # v2
      - name: Check formatting
        run: cargo fmt --all -- --check
      - name: Run Clippy
        run: cargo clippy --workspace --all-targets --all-features --locked -- -D warnings
      - name: Run tests
        run: cargo test --workspace --all-features --locked
""",
        "deploy": """      - uses: actions/checkout@v4
      - name: Deploy
        run: ./scripts/deploy.sh
""",
    }
    if template not in jobs:
        raise KeyError(template)
    environment = "    environment: production\n" if template == "deploy" else ""
    job_environment = (
        "    env:\n      CARGO_TERM_COLOR: always\n      RUST_BACKTRACE: '1'\n"
        if template == "rust"
        else ""
    )
    return (
        f"name: {template.title()} on EasyRunners\n\n"
        "on:\n  workflow_dispatch:\n  push:\n    branches: [main]\n\n"
        "jobs:\n"
        f"  {template}:\n"
        f"    runs-on: [{labels}]\n"
        f"    timeout-minutes: {max(1, pool.job_timeout // 60)}\n"
        f"{environment}"
        f"{job_environment}"
        "    steps:\n"
        f"{jobs[template]}"
    )
