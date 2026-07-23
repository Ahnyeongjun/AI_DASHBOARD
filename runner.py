"""학습 큐 러너 데몬.

queue.json의 맨 앞 항목을 꺼내 torchrun으로 순차 실행한다.
- 외부 torchrun(예: 수동 실행, 기존 학습)이 돌고 있으면 대기
- paused=true면 새 학습 시작 안 함 (진행 중인 학습은 유지)
- 학습 프로세스는 start_new_session으로 분리 — 러너가 죽어도 학습은 계속됨
"""
import datetime
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qlib

TORCHRUN = os.environ.get('DASH_TORCHRUN', 'torchrun')
GPUS = os.environ.get('DASH_GPUS', '0')
NPROC = os.environ.get('DASH_NPROC', str(len(GPUS.split(','))))
RUNNER_LOG = os.path.join(qlib.BASE, 'runner.log')
POLL_SEC = 15
GPU_RELEASE_SEC = 45


def log(msg):
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(RUNNER_LOG, 'a') as f:
        f.write(f'[{ts}] {msg}\n')


def set_state(**kw):
    s = qlib.load_state()
    s.update(kw, pid=os.getpid(), updated=time.time())
    qlib.save_state(s)


def launch(item):
    num, cfg = item['num'], item['config']
    outdir = os.path.join(qlib.EXP_DIR, f'training_{num}')
    os.makedirs(outdir, exist_ok=True)
    env = os.environ.copy()
    env['CUDA_VISIBLE_DEVICES'] = GPUS
    env['PYTORCH_CUDA_ALLOC_CONF'] = 'expandable_segments:True'
    logf = open(os.path.join(outdir, 'host_run.log'), 'ab')
    logf.write(f'[runner {datetime.datetime.now()}] training_{num} start ({cfg})\n'.encode())
    logf.flush()
    proc = subprocess.Popen(
        [TORCHRUN, f'--nproc_per_node={NPROC}', 'train.py', '--config', cfg],
        cwd=qlib.CODE_DIR, stdout=logf, stderr=subprocess.STDOUT,
        env=env, start_new_session=True)
    return proc


def main():
    log(f'runner start (pid={os.getpid()})')
    child = None
    child_item = None
    started_at = None
    while True:
        try:
            if child is not None:
                rc = child.poll()
                if rc is not None:
                    log(f"training_{child_item['num']} exited with {rc}")
                    with qlib.QueueLock():
                        q = qlib.load_queue()
                        q['history'].append({
                            'num': child_item['num'], 'config': child_item['config'],
                            'exit_code': rc, 'started': started_at,
                            'ended': time.time()})
                        qlib.save_queue(q)
                    child = None
                    child_item = None
                    set_state(status='idle', running=None, child_pid=None)
                    time.sleep(GPU_RELEASE_SEC)
                else:
                    set_state(status='running', running=child_item,
                              child_pid=child.pid)

            if child is None:
                with qlib.QueueLock():
                    q = qlib.load_queue()
                if q.get('paused'):
                    set_state(status='paused', running=None, child_pid=None)
                elif not q['queue']:
                    set_state(status='idle', running=None, child_pid=None)
                else:
                    foreign = qlib.torchrun_procs()
                    if foreign:
                        set_state(status='waiting_gpu', running=None,
                                  child_pid=None,
                                  waiting_on=[p for p, _ in foreign])
                    else:
                        with qlib.QueueLock():
                            q = qlib.load_queue()
                            if q['queue'] and not q.get('paused'):
                                item = q['queue'].pop(0)
                                qlib.save_queue(q)
                            else:
                                item = None
                        if item:
                            log(f"training_{item['num']} start ({item['config']})")
                            child = launch(item)
                            child_item = item
                            started_at = time.time()
                            set_state(status='running', running=item,
                                      child_pid=child.pid)
        except Exception as e:
            log(f'runner error: {e!r}')
        time.sleep(POLL_SEC)


if __name__ == '__main__':
    main()
