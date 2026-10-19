#!/bin/sh
# find-file.sh — thin wrapper for the universal cached search (zero-token).
exec python3 "$(dirname "$0")/find_file.py" "$@"
