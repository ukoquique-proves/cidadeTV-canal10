#!/usr/bin/env bash
# Monitor TV Cidade 10 stream availability.
# Usage: ./check_stream.sh            (checks every 60 s; Ctrl+C to stop)
#        STREAM_URL=https://... ./check_stream.sh
#
# Uses the same STREAM_URL as the app: environment first, then .env, then the
# built-in default. (It used to be hard-coded, so changing STREAM_URL in .env
# left this script watching the old address.)

cd "$(dirname "$0")" || exit 1

DEFAULT_URL="https://video10.logicahost.com.br/tvcidade10/tvcidade10/playlist.m3u8"

if [ -z "$STREAM_URL" ] && [ -f .env ]; then
  # last uncommented STREAM_URL=... line; strip optional quotes and trailing comment
  STREAM_URL=$(sed -n 's/^STREAM_URL=//p' .env | tail -n1 | sed -e 's/[[:space:]]*#.*$//' -e 's/^["'"'"']//' -e 's/["'"'"']$//')
fi
STREAM_URL="${STREAM_URL:-$DEFAULT_URL}"

echo "Monitoring: $STREAM_URL"
echo "Press Ctrl+C to stop."
echo ""

while true; do
  HTTP_CODE=$(curl -s -o /dev/null -m 15 -w "%{http_code}" "$STREAM_URL")
  TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

  if [ "$HTTP_CODE" = "200" ]; then
    echo "[$TIMESTAMP] ✓ Stream is ONLINE (HTTP $HTTP_CODE)"
  else
    echo "[$TIMESTAMP] ✗ Stream offline (HTTP $HTTP_CODE)"
  fi

  sleep 60
done
