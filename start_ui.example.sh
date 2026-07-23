#!/bin/bash
# 큐 러너 + 웹 서버 시작 (재부팅 후 이걸 실행하면 복구됨)
# 이 파일을 start_ui.sh로 복사한 뒤 환경에 맞게 수정하세요. (start_ui.sh는 gitignore)
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=/path/to/conda_env/bin/python

# "boot" 인자: 재부팅 자동시작용 — 큐를 일시정지 상태로 올림
# (재부팅으로 죽은 학습을 러너가 건너뛰고 다음 실험을 시작하는 것 방지.
#  UI에서 상태 확인 후 ▶재개를 눌러야 큐가 돈다)
# 서버 crontab 등록 예: @reboot sleep 60 && bash /path/to/queue_ui/start_ui.sh boot >> /path/to/queue_ui/boot.log 2>&1
if [ "$1" = "boot" ] && [ -f "${BASE}/queue.json" ]; then
    ${PY} -c "
import json
p = '${BASE}/queue.json'
q = json.load(open(p)); q['paused'] = True
json.dump(q, open(p, 'w'), ensure_ascii=False, indent=2)
print('boot mode: 큐 일시정지 상태로 시작')
"
fi

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
