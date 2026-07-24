"""학습 큐 모니터링/관리 웹 서버 (stdlib only, 127.0.0.1 전용).

GET  /            — 대시보드 HTML
GET  /api/status  — GPU, 실행 중 학습(로그 파싱), 큐, 히스토리, 러너 상태
POST /api/action  — {action: pause|resume|remove|move_up|move_down|add, ...}
"""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qlib

PORT = int(os.environ.get('DASH_PORT', '8080'))
BIND = os.environ.get('DASH_BIND', '127.0.0.1')  # LAN 공개 시 0.0.0.0
INDEX = os.path.join(qlib.BASE, 'index.html')


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


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == '/' or self.path.startswith('/index'):
            with open(INDEX, 'rb') as f:
                self._send(200, f.read(), 'text/html; charset=utf-8')
        elif self.path == '/api/status':
            self._send(200, status())
        else:
            self._send(404, {'error': 'not found'})

    def do_POST(self):
        if self.path == '/api/action':
            n = int(self.headers.get('Content-Length', 0))
            try:
                body = json.loads(self.rfile.read(n) or b'{}')
                self._send(200, do_action(body))
            except Exception as e:
                self._send(400, {'ok': False, 'error': repr(e)})
        else:
            self._send(404, {'error': 'not found'})

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f'serving on http://{BIND}:{PORT}')
    srv.serve_forever()
