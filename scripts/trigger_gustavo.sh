#!/bin/bash
# Gustavo Auto-Trigger Script
# Fires the Gustavo News Update workflow every 15 minutes if AI_API_KEY is set

TOKEN=$(cat ~/.git-credentials | grep -o 'ghp_[^@]*')
REPO="Nick-Machiavelli/Gustavo"
WORKFLOW="python-app.yml"

# Check if AI_API_KEY exists (we can't directly, so just trigger unconditionally)
# Trigger only if last run was more than 14 minutes ago to avoid overlap
RESPONSE=$(curl -s -m 10 "https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/runs?per_page=1")
LAST_RUN_UPDATED_AT=$(echo "$RESPONSE" | grep -o '"updated_at":"[^"]*"' | head -1 | cut -d'"' -f4)

if [ -z "$LAST_RUN_UPDATED_AT" ]; then
  # No runs yet
  SHOULD_TRIGGER=1
else
  # Parse time difference
  LAST_RUN_EPOCH=$(date -d "$LAST_RUN_UPDATED_AT" +%s)
  NOW_EPOCH=$(date +%s)
  DIFF=$((NOW_EPOCH - LAST_RUN_EPOCH))
  
  # If more than 14 minutes (840 seconds), trigger a new run
  if [ $DIFF -gt 840 ]; then
    SHOULD_TRIGGER=1
  else
    SHOULD_TRIGGER=0
  fi
fi

if [ "$SHOULD_TRIGGER" -eq 1 ]; then
  echo "$(date): Triggering workflow..."
  curl -s -X POST \
    -H "Accept: application/vnd.github+json" \
    -H "Authorization: Bearer $TOKEN" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches" \
    -d '{"ref":"main"}'
  echo "$(date): Workflow triggered."
else
  echo "$(date): Skipping trigger — last run was only $((DIFF / 60)) minutes ago."
fi
