#!/bin/bash
# Stop all local AI subtitle services by port.

for port in 18182 18181 18180; do
  pids=$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null)
  if [ -n "$pids" ]; then
    echo "stopping port $port (pid $pids)"
    kill $pids 2>/dev/null
  fi
done
sleep 1
echo "AI subtitle services stopped"
