#!/bin/bash
if [ -f /root/sync-endpoint]; then
  echo "System already configured. Skipping script"
  exit 0
fi

wget https://raw.githubusercontent.com/c-h-a-r-l-i-e/sync-endpoint/master/startup-bg.sh

/bin/bash startup-bg.sh &
