#!/bin/bash
# 큐 러너 + 웹 서버 시작 (재부팅 후 이걸 실행하면 복구됨)
# 이 파일을 start_ui.sh로 복사한 뒤 환경에 맞게 수정하세요. (start_ui.sh는 gitignore)
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=/path/to/conda_env/bin/python

export DASH_CODE_DIR=/path/to/train_code      # train.py가 있는 디렉토리
export DASH_EXP_DIR=/path/to/experiments      # training_{N} 출력 디렉토리들의 상위
export DASH_TORCHRUN=/path/to/conda_env/bin/torchrun
export DASH_GPUS=0,1,2                        # CUDA_VISIBLE_DEVICES
export DASH_PORT=8080
export DASH_BIND=127.0.0.1                    # LAN 공개 시 0.0.0.0 (보안 주의)

if pgrep -f "queue_ui/runner.py" > /dev/null; then
    echo "runner already running"
else
    nohup ${PY} "${BASE}/runner.py" >> "${BASE}/runner_stdout.log" 2>&1 &
    echo "runner started (pid $!)"
fi

if pgrep -f "queue_ui/server.py" > /dev/null; then
    echo "server already running"
else
    nohup ${PY} "${BASE}/server.py" >> "${BASE}/server_stdout.log" 2>&1 &
    echo "server started (pid $!) — http://${DASH_BIND}:${DASH_PORT}"
fi
