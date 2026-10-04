#!/bin/bash
# Monitor TV Cidade 10 stream availability
# Usage: ./check_stream.sh
# Prints 200 when stream is back online (for use in cron or automation)

STREAM_URL="https://video10.logicahost.com.br/tvcidade10/tvcidade10/playlist.m3u8"

echo "Monitoring TV Cidade 10 stream..."
echo "Press Ctrl+C to stop."
echo ""

while true; do
  HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$STREAM_URL")
  TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
  
  if [ "$HTTP_CODE" = "200" ]; then
    echo "[$TIMESTAMP] ✓ Stream is ONLINE (HTTP $HTTP_CODE)"
    # Optional: you could exit here or send a notification
  else
    echo "[$TIMESTAMP] ✗ Stream offline (HTTP $HTTP_CODE)"
  fi
  
  sleep 60
done
