#!/bin/bash
set -e

# Start API-server i baggrunden
uvicorn api:app --host 0.0.0.0 --port 8080 &

# Scheduler er hovedprocessen (exec → korrekt signal-håndtering)
exec /app/scheduler.sh
