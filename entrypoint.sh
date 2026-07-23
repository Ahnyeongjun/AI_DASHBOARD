#!/bin/sh
# 큐 러너(백그라운드) + 웹 서버(포그라운드) 시작.
# 컨테이너가 죽으면 둘 다 같이 죽는다 (재시작은 docker-compose restart 정책에 위임).
set -e

python runner.py &
RUNNER_PID=$!
trap 'kill "$RUNNER_PID" 2>/dev/null' TERM INT

exec python server.py
