#!/bin/sh
# scheduler.sh — Kører assistant.py på konfigurerbare tidspunkter
# Format: kommaseparerede HH:MM værdier, f.eks. "07:00,09:00,12:00,15:00,18:00,21:00"

set -eu

SCHEDULE_TIMES="${SCHEDULE_TIMES:-07:00,09:00,12:00,15:00,18:00,21:00}"

echo "mail-assistant scheduler starter"
echo "TZ: ${TZ:-UTC}"
echo "Planlagte tider: $SCHEDULE_TIMES"

TIMES=$(echo "$SCHEDULE_TIMES" | tr ',' '\n')

while true; do
    CURRENT=$(date +%H:%M)

    SHOULD_RUN=0
    for T in $TIMES; do
        if [ "$CURRENT" = "$T" ]; then
            SHOULD_RUN=1
            break
        fi
    done

    if [ "$SHOULD_RUN" = "1" ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') — Starter mail-scanning ($CURRENT)..."
        python /app/assistant.py || echo "ADVARSEL: assistant.py fejlede (exit non-zero)"
        echo "$(date '+%Y-%m-%d %H:%M:%S') — Scanning færdig. Sover 61 sek..."
        sleep 61
    else
        sleep 30
    fi
done
