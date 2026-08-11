#!/usr/bin/env bash
set -Eeuo pipefail

: "${RUNNER_URL:?RUNNER_URL is required}"
: "${RUNNER_TOKEN:?RUNNER_TOKEN is required}"
: "${RUNNER_NAME:?RUNNER_NAME is required}"

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
./config.sh "${args[@]}"
unset RUNNER_TOKEN

echo "runner.online name=${RUNNER_NAME}"
exec timeout --signal=TERM --kill-after=30s "${RUNNER_MAX_LIFETIME:-3900}" ./run.sh

