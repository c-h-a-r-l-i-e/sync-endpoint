#!/bin/bash
if [ -f /sync-endpoint ]; then
  echo "System already configured. Skipping script"
  exit 0
fi

wget https://raw.githubusercontent.com/c-h-a-r-l-i-e/sync-endpoint/master/startup-bg.sh

nohup /bin/bash startup-bg.sh &
