#!/bin/bash
CHROME="/c/Program Files/Google/Chrome/Application/chrome.exe"
UDD="$LOCALAPPDATA/hermes/chrome-debug2"
PORT=9223
rm -rf "$UDD"
"$CHROME" --headless=new --disable-gpu --remote-debugging-port=$PORT --remote-allow-origins=* --user-data-dir="$UDD" about:blank >/dev/null 2>&1 &
CHROME_PID=$!
# wait for debug endpoint
for i in $(seq 1 20); do
  if curl -s "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; then break; fi
  sleep 1
done
node "$(dirname "$0")/contrast_check.js" "$PORT"
RC=$?
kill $CHROME_PID 2>/dev/null
exit $RC
