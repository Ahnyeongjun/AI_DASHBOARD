"""학습 큐 모니터링/관리 웹 서버 (FastAPI, 127.0.0.1 전용).

GET  /            — 대시보드 HTML
GET  /api/status  — GPU, 실행 중 학습(로그 파싱), 큐, 히스토리, 러너 상태
POST /api/action  — {action: pause|resume|remove|move_up|move_down|add, ...}
"""
import os
import sys
import time

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qlib

PORT = int(os.environ.get('DASH_PORT', '8080'))
BIND = os.environ.get('DASH_BIND', '127.0.0.1')  # LAN 공개 시 0.0.0.0
INDEX = os.path.join(qlib.BASE, 'index.html')

app = FastAPI()


def status():
    procs = qlib.torchrun_procs()
    running = []
    for pid, cmd in procs:
        num = qlib.exp_num_from_cmd(cmd)
        entry = {'pid': pid, 'num': num}
        if num is not None:
            entry['log'] = qlib.parse_training_log(num)
        running.append(entry)
    q = qlib.load_queue()
    state = qlib.load_state()
    runner_alive = False
    if state.get('pid'):
        try:
            os.kill(state['pid'], 0)
            runner_alive = True
        except OSError:
            pass
    return {'time': time.time(), 'gpus': qlib.gpu_status(), 'running': running,
            'queue': q['queue'], 'paused': q.get('paused', False),
            'history': q.get('history', [])[-20:],
            'runner': {'alive': runner_alive, **state}}


def do_action(body):
    act = body.get('action')
    with qlib.QueueLock():
        q = qlib.load_queue()
        if act == 'pause':
            q['paused'] = True
        elif act == 'resume':
            q['paused'] = False
        elif act in ('remove', 'move_up', 'move_down'):
            i = int(body['index'])
            if not 0 <= i < len(q['queue']):
                return {'ok': False, 'error': 'index out of range'}
            if act == 'remove':
                q['queue'].pop(i)
            elif act == 'move_up' and i > 0:
                q['queue'][i - 1], q['queue'][i] = q['queue'][i], q['queue'][i - 1]
            elif act == 'move_down' and i < len(q['queue']) - 1:
                q['queue'][i + 1], q['queue'][i] = q['queue'][i], q['queue'][i + 1]
        elif act == 'add':
            num = int(body['num'])
            cfg = body['config'].strip()
            cfg_abs = cfg if os.path.isabs(cfg) else os.path.join(qlib.CODE_DIR, cfg)
            if not os.path.exists(cfg_abs):
                return {'ok': False, 'error': f'config not found: {cfg_abs}'}
            if any(it['num'] == num for it in q['queue']):
                return {'ok': False, 'error': f'training_{num} already queued'}
            outdir = os.path.join(qlib.EXP_DIR, f'training_{num}')
            if os.path.exists(os.path.join(outdir, 'log.txt')):
                return {'ok': False, 'error': f'training_{num} output_dir already used'}
            q['queue'].append({'num': num, 'config': cfg})
        else:
            return {'ok': False, 'error': f'unknown action: {act}'}
        qlib.save_queue(q)
    return {'ok': True}


@app.get('/')
@app.get('/index')
@app.get('/index.html')
def index():
    return FileResponse(INDEX, media_type='text/html; charset=utf-8')


@app.get('/api/status')
def api_status():
    return status()


@app.post('/api/action')
async def api_action(request: Request):
    try:
        body = await request.json() if await request.body() else {}
        return do_action(body)
    except Exception as e:
        return JSONResponse({'ok': False, 'error': repr(e)}, status_code=400)


if __name__ == '__main__':
    import uvicorn
    print(f'serving on http://{BIND}:{PORT}')
    uvicorn.run(app, host=BIND, port=PORT, log_level='warning')
