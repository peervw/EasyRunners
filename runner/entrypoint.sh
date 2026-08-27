#!/usr/bin/env bash
set -Eeuo pipefail

: "${RUNNER_URL:?RUNNER_URL is required}"
: "${RUNNER_TOKEN:?RUNNER_TOKEN is required}"
: "${RUNNER_NAME:?RUNNER_NAME is required}"

run_as_runner() {
  if [[ "$(id -u)" -eq 0 ]]; then
    runuser -u runner --preserve-environment -- \
      env HOME=/home/runner USER=runner LOGNAME=runner "$@"
  else
    "$@"
  fi
}

if [[ "${RUNNER_DOCKER_MODE:-none}" == "isolated" ]]; then
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "runner.docker_isolated_requires_root" >&2
    exit 1
  fi
  mkdir -p /var/run/easyrunners-docker /var/lib/easyrunners-docker /var/run/easyrunners-docker-exec
  chown runner:runner /var/run/easyrunners-docker
  dockerd \
    --host=unix:///var/run/easyrunners-docker/docker.sock \
    --data-root=/var/lib/easyrunners-docker \
    --exec-root=/var/run/easyrunners-docker-exec \
    --pidfile=/var/run/easyrunners-docker/docker.pid \
    --group=runner \
    > /home/runner/_diag/dockerd.log 2>&1 &
  dockerd_pid=$!
  for _ in $(seq 1 60); do
    if docker info >/dev/null 2>&1; then
      echo "runner.docker_isolated_ready"
      break
    fi
    if ! kill -0 "$dockerd_pid" 2>/dev/null; then
      echo "runner.docker_isolated_failed" >&2
      tail -n 100 /home/runner/_diag/dockerd.log >&2 || true
      exit 1
    fi
    sleep 1
  done
  if ! docker info >/dev/null 2>&1; then
    echo "runner.docker_isolated_timeout" >&2
    exit 1
  fi
fi

args=(
  --url "$RUNNER_URL"
  --token "$RUNNER_TOKEN"
  --name "$RUNNER_NAME"
  --work _work
  --unattended
  --ephemeral
  --disableupdate
)

if [[ -n "${RUNNER_LABELS:-}" ]]; then
  args+=(--labels "$RUNNER_LABELS")
fi
if [[ -n "${RUNNER_GROUP:-}" ]]; then
  args+=(--runnergroup "$RUNNER_GROUP")
fi

echo "runner.registering name=${RUNNER_NAME} url=${RUNNER_URL}"
run_as_runner ./config.sh "${args[@]}"
unset RUNNER_TOKEN

echo "runner.online name=${RUNNER_NAME}"
if [[ "$(id -u)" -eq 0 ]]; then
  exec timeout --signal=TERM --kill-after=30s "${RUNNER_MAX_LIFETIME:-3900}" \
    runuser -u runner --preserve-environment -- \
      env HOME=/home/runner USER=runner LOGNAME=runner ./run.sh
fi
exec timeout --signal=TERM --kill-after=30s "${RUNNER_MAX_LIFETIME:-3900}" ./run.sh
