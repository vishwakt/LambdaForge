"""Top-level Lambda handler entry point.

The Lambda RIC resolves handlers relative to LAMBDA_TASK_ROOT.
This thin wrapper re-exports the actual handlers from src/.
"""

from src.lambda_handlers import (
    daily_scan_handler,
    monitor_stops_handler,
    eod_snapshot_handler,
)
