#!/usr/bin/env bash
# Run one DynamoDB migration step on one stack and fail unless it reports ok.
#
#   scripts/migration_step.sh stock-trading-bot-2 status
#
# Modes are described in src/migrate.py. The step runs inside the stack's
# MigrateFunction, with the stack's own role and Alpaca keys; nothing here
# touches a secret.
set -euo pipefail

stack="$1"
mode="$2"
report="$(mktemp)"

aws lambda invoke \
  --function-name "${stack}-migrate" \
  --cli-binary-format raw-in-base64-out \
  --cli-read-timeout 900 \
  --payload "{\"mode\": \"${mode}\"}" \
  "$report" >/dev/null

python3 -m json.tool "$report"
# An unhandled error comes back as {"errorMessage": ...}, which has no "ok".
python3 -c 'import json, sys; sys.exit(0 if json.load(open(sys.argv[1])).get("ok") else 1)' "$report"
