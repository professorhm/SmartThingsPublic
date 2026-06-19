#!/bin/bash
# Adds a cron job to run the price monitor every hour.
# Run this once: bash setup_cron.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(which python3)"
LOG="$SCRIPT_DIR/monitor.log"

CRON_LINE="0 * * * * cd \"$SCRIPT_DIR\" && $PYTHON price_monitor.py >> \"$LOG\" 2>&1"

# Check if cron entry already exists
if crontab -l 2>/dev/null | grep -qF "price_monitor.py"; then
    echo "Cron job already exists. Nothing changed."
else
    (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
    echo "Cron job added — runs every hour."
    echo "Logs will appear in: $LOG"
fi

echo ""
echo "Current crontab:"
crontab -l
