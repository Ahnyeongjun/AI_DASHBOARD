"""공용 헬퍼: queue.json 락 기반 읽기/쓰기, 학습 로그 파싱."""
import fcntl
import json
import os
import re
import subprocess

BASE = os.path.dirname(os.path.abspath(__file__))
QUEUE_PATH = os.path.join(BASE, 'queue.json')
STATE_PATH = os.path.join(BASE, 'runner_state.json')
LOCK_PATH = os.path.join(BASE, '.queue.lock')
# 배포 환경별 설정은 환경변수로 주입 (start_ui.sh 참고)
CODE_DIR = os.environ.get('DASH_CODE_DIR', os.getcwd())
EXP_DIR = os.environ.get('DASH_EXP_DIR', './experiments')


class QueueLock:
    def __enter__(self):
        self._f = open(LOCK_PATH, 'w')
        fcntl.flock(self._f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self._f, fcntl.LOCK_UN)
        self._f.close()


def load_queue():
    if not os.path.exists(QUEUE_PATH):
        return {'paused': False, 'queue': [], 'history': []}
    with open(QUEUE_PATH) as f:
        return json.load(f)


def save_queue(q):
    tmp = QUEUE_PATH + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(q, f, ensure_ascii=False, indent=2)
    os.replace(tmp, QUEUE_PATH)


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    tmp = STATE_PATH + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def torchrun_procs():
    """실행 중인 torchrun 런처 목록 [(pid, cmdline)]. train.py 워커는 매칭 안 됨."""
    try:
        out = subprocess.run(['pgrep', '-f', '-a', 'bin/torchrun'],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    procs = []
    for line in out.strip().splitlines():
        pid, _, cmd = line.partition(' ')
        if 'bin/torchrun' in cmd:
            procs.append((int(pid), cmd))
    return procs


def exp_num_from_cmd(cmd):
    m = re.search(r'train_(\d+)', cmd)
    return int(m.group(1)) if m else None


def parse_training_log(num, tail_epochs=500):
    """experiments/training_N/log.txt에서 epoch별 mIoU 시리즈와 best를 파싱."""
    log_path = os.path.join(EXP_DIR, f'training_{num}', 'log.txt')
    info = {'num': num, 'epochs': [], 'miou7': [], 'miou6': [],
            'best': None, 'best_epoch': None, 'max_epochs': None}
    if not os.path.exists(log_path):
        return info
    re_ep = re.compile(r'Epoch (\d+) \| val mIoU=([0-9.]+)')
    re_6 = re.compile(r'  val 6cls_ndvi mIoU=([0-9.]+)')
    re_best = re.compile(r'best_kor=([0-9.]+) \(epoch (\d+)\)')
    last_ep = None
    with open(log_path, errors='replace') as f:
        for line in f:
            m = re_ep.search(line)
            if m:
                last_ep = int(m.group(1))
                info['epochs'].append(last_ep)
                info['miou7'].append(float(m.group(2)))
                info['miou6'].append(None)
                continue
            m = re_6.search(line)
            if m and info['miou6'] and info['miou6'][-1] is None:
                info['miou6'][-1] = float(m.group(1))
                continue
            m = re_best.search(line)
            if m:
                info['best'] = float(m.group(1))
                info['best_epoch'] = int(m.group(2))
    if len(info['epochs']) > tail_epochs:
        for k in ('epochs', 'miou7', 'miou6'):
            info[k] = info[k][-tail_epochs:]
    cfg_path = os.path.join(EXP_DIR, f'training_{num}', 'config.json')
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
        args = cfg.get('args', cfg)
        info['max_epochs'] = args.get('max_epochs')
    except Exception:
        pass
    return info


def gpu_status():
    try:
        out = subprocess.run(
            ['nvidia-smi',
             '--query-gpu=index,memory.used,memory.total,utilization.gpu',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    gpus = []
    for line in out.strip().splitlines():
        idx, used, total, util = [x.strip() for x in line.split(',')]
        gpus.append({'index': int(idx), 'mem_used': int(used),
                     'mem_total': int(total), 'util': int(util)})
    return gpus
