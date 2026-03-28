#!/usr/bin/env bash
# Test all Lambda functions for a given stack.
#
# Usage:
#   ./scripts/test-lambdas.sh                    # Test paper stack (default)
#   ./scripts/test-lambdas.sh stock-trading-bot-2 # Test Bot 2 stack
#   ./scripts/test-lambdas.sh stock-trading-bot-live # Test live stack
#
# Each Lambda is invoked and the response is printed.
# Requires: aws CLI configured with appropriate credentials.

set -euo pipefail

STACK="${1:-stock-trading-bot}"
OUTFILE="/tmp/lambda-response.json"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "============================================"
echo "  Lambda Test Suite — Stack: ${STACK}"
echo "============================================"
echo ""

# Helper: get physical resource ID from logical ID
get_fn() {
    aws cloudformation describe-stack-resources \
        --stack-name "$STACK" \
        --logical-resource-id "$1" \
        --query "StackResources[0].PhysicalResourceId" \
        --output text 2>/dev/null
}

# Helper: invoke a Lambda and print result
invoke() {
    local label="$1"
    local logical_id="$2"
    local payload="$3"

    local fn_name
    fn_name=$(get_fn "$logical_id")

    if [ -z "$fn_name" ] || [ "$fn_name" = "None" ]; then
        echo -e "${RED}SKIP${NC} ${label} — resource not found in stack"
        return
    fi

    echo -n "  ${label}... "

    local http_code
    http_code=$(aws lambda invoke \
        --function-name "$fn_name" \
        --cli-binary-format raw-in-base64-out \
        --payload "$payload" \
        "$OUTFILE" \
        --query "StatusCode" \
        --output text 2>/dev/null)

    if [ "$http_code" = "200" ]; then
        local body
        body=$(cat "$OUTFILE" 2>/dev/null)
        # Check for function errors
        if echo "$body" | grep -q '"errorMessage"' 2>/dev/null; then
            local err
            err=$(echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('errorMessage','unknown'))" 2>/dev/null || echo "$body")
            echo -e "${RED}FAIL${NC} (Lambda error: ${err})"
        else
            local status
            status=$(echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('statusCode', d.get('body','OK')))" 2>/dev/null || echo "$body")
            echo -e "${GREEN}OK${NC} (${status})"
        fi
    else
        echo -e "${RED}FAIL${NC} (HTTP ${http_code})"
    fi
}

echo "1. Kill Switch (status)"
invoke "KillSwitch status" "KillSwitchFunction" '{"action":"status"}'
echo ""

echo "2. Kill Switch (alive — safe no-op)"
invoke "KillSwitch alive" "KillSwitchFunction" '{"action":"alive"}'
echo ""

echo "3. Monitor Stops (runs trailing stops + entry/exit scan)"
invoke "MonitorStops" "MonitorStopsFunction" '{}'
echo ""

echo "4. Daily Scan (full trading scan)"
invoke "DailyScan" "DailyScanFunction" '{}'
echo ""

echo "5. EOD Snapshot (end-of-day summary)"
invoke "EodSnapshot" "EodSnapshotFunction" '{}'
echo ""

echo "6. Weekly Digest"
invoke "WeeklyDigest" "WeeklyDigestFunction" '{}'
echo ""

echo "7. Hourly Digest"
invoke "HourlyDigest" "HourlyDigestFunction" '{}'
echo ""

echo "============================================"
echo "  All tests complete for: ${STACK}"
echo "============================================"

# Cleanup
rm -f "$OUTFILE"
