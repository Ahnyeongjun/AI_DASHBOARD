"""SSH 자동 포트포워딩 매니저 + 등록 UI (로컬 PC에서 실행).

servers.json의 서버 목록을 5초마다 읽어:
- 원격 서버 + SSH 키 인증(기본): 외부 ssh -N -L 프로세스로 터널 유지 (죽으면 자동 재접속, 백오프 30초)
- 원격 서버 + 비밀번호 인증(servers.json에 password 필드가 있으면): paramiko로 포트포워딩을 직접 구현
  (Windows OpenSSH는 비밀번호를 명령줄로 못 받아서 — sshpass/plink 같은 외부 바이너리 없이 순수 파이썬으로 처리)
- 로컬 서버(local: true, 이 PC 자체에서 도는 대시보드)는 SSH 없이 포트 응답만 주기적으로 확인
- 목록에서 빠지거나 enabled=false가 되면 터널 종료 / 상태 갱신 중단
- 파일에 서버를 추가하면 재시작 없이 자동으로 반영됨

같은 프로세스에서 FastAPI 웹 UI(TUNNEL_UI_PORT, 기본 8090)를 띄워
브라우저로 서버 등록/삭제/활성화 토글 + 연결 상태 확인 + 등록된 서버의 대시보드를 한 화면(iframe)에서 볼 수 있다.
iframe은 /dash/{idx}/* 로 이 프로세스가 대상 로컬 포트로 리버스 프록시하므로, 사용자는 이 포트(8090) 하나만
열면 되고 실제 대시보드가 어느 포트에 떠 있는지는 주소창/네트워크 탭에도 노출되지 않는다.
UI에서 등록하면 servers.json에 반영되고, 원격 서버는 위 모니터 루프가 5초 내 자동으로 터널을 연다.

원격 서버 등록 시 "자동 설치"를 선택하면 SSH로 접속해 대시보드 코드를 올리고(pip 또는 Docker,
원격에 있는 걸 자동 감지) 바로 실행까지 한다 — GPU 서버에 미리 수동으로 코드를 배포해둘 필요가 없다.

주의: 비밀번호는 servers.json에 평문으로 저장됨 (gitignore되지만 로컬 디스크 파일 — 이 PC에 접근 가능한
사람에게는 노출됨). 가능하면 SSH 키 인증을 쓰고, 비밀번호는 키를 못 쓰는 경우의 대안으로만 사용할 것.
Windows(OpenSSH)/macOS/Linux 공통.

사용:
    pip install -r requirements.txt   # fastapi, uvicorn, paramiko
    cp servers.example.json servers.json   # (선택) 초기 서버 목록 작성
    python tunnel.py                        # 포그라운드 실행, http://localhost:8090 에서 등록
부팅 자동시작 등록은 README 참고.
"""
import datetime
import json
import os
import select
import socket
import socketserver
import subprocess
import sys
import threading
import time

import httpx
import paramiko
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

# 출력이 파이프/파일로 리다이렉트되면(nohup 등) Windows에서 시스템 코드페이지(cp949 등)로
# 기본 인코딩되어 —/→ 같은 문자에서 UnicodeEncodeError가 남 — UTF-8로 고정.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(encoding='utf-8', errors='replace')

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, 'servers.json')
UI_INDEX = os.path.join(BASE, 'index.html')
POLL_SEC = 5
RETRY_SEC = 30  # 접속 실패 시 재시도 간격

UI_PORT = int(os.environ.get('TUNNEL_UI_PORT', '8090'))
UI_BIND = os.environ.get('TUNNEL_UI_BIND', '127.0.0.1')

_cfg_lock = threading.Lock()   # servers.json 읽기/쓰기 보호 (UI 스레드 vs 모니터 스레드)
_status_lock = threading.Lock()
_status = {}  # key -> {'desc': str, 'alive': bool, 'retry_in': int}


def log(msg):
    ts = datetime.datetime.now().strftime('%H:%M:%S')
    print(f'[{ts}] {msg}', flush=True)


def read_servers():
    with _cfg_lock:
        try:
            with open(CFG_PATH, encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            return {'servers': []}
        except json.JSONDecodeError as e:
            log(f'servers.json 파싱 오류: {e}')
            return None


def write_servers(cfg):
    with _cfg_lock:
        tmp = CFG_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CFG_PATH)


def entry_key(s, f):
    """서버+포워딩 → 고유 키. 로컬(SSH 불필요) 서버는 포트만으로 식별."""
    if s.get('local'):
        return f"local:{f['local']}"
    target = (s['user'] + '@' if s.get('user') else '') + s['host']
    return f"{target}:{s.get('port', 22)} L{f['local']}->{f['remote']}"


def desired_tunnels(cfg):
    """SSH 키 인증으로 터널이 필요한(원격, 비밀번호 없음) 서버만 → {키: (ssh 커맨드, 설명)} 매핑."""
    out = {}
    for s in cfg.get('servers', []):
        if not s.get('enabled', True) or s.get('local') or s.get('password'):
            continue
        target = (s['user'] + '@' if s.get('user') else '') + s['host']
        for f in s.get('forwards', []):
            key = entry_key(s, f)
            cmd = ['ssh', '-N',
                   '-o', 'ServerAliveInterval=15',
                   '-o', 'ServerAliveCountMax=3',
                   '-o', 'ExitOnForwardFailure=yes',
                   '-o', 'BatchMode=yes',
                   '-o', 'StrictHostKeyChecking=accept-new',
                   '-p', str(s.get('port', 22)),
                   '-L', f"{f['local']}:localhost:{f['remote']}",
                   target]
            desc = f"{s.get('name', s['host'])} → http://localhost:{f['local']}"
            out[key] = (cmd, desc)
    return out


def local_targets(cfg):
    """SSH 없이 바로 접근하는(이 PC 자체) 서버 → {키: (포트, 설명)} 매핑."""
    out = {}
    for s in cfg.get('servers', []):
        if not s.get('enabled', True) or not s.get('local'):
            continue
        for f in s.get('forwards', []):
            key = entry_key(s, f)
            desc = f"{s.get('name', 'local')} → http://localhost:{f['local']}"
            out[key] = (f['local'], desc)
    return out


def check_local_port(port, timeout=1.5):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=timeout):
            return True
    except OSError:
        return False


def password_tunnels(cfg):
    """비밀번호 인증이 필요한(paramiko) 서버 → {키: (server dict, forward dict, 설명)} 매핑."""
    out = {}
    for s in cfg.get('servers', []):
        if not s.get('enabled', True) or s.get('local') or not s.get('password'):
            continue
        for f in s.get('forwards', []):
            key = entry_key(s, f)
            desc = f"{s.get('name', s['host'])} → http://localhost:{f['local']}"
            out[key] = (s, f, desc)
    return out


class _ForwardHandler(socketserver.BaseRequestHandler):
    """로컬 소켓 ↔ paramiko direct-tcpip 채널 간 바이트 릴레이. transport/remote_port는 서브클래싱해서 주입."""
    transport = None
    remote_port = None

    def handle(self):
        try:
            chan = self.transport.open_channel(
                'direct-tcpip', ('127.0.0.1', self.remote_port), self.request.getpeername())
        except Exception:
            return
        if chan is None:
            return
        try:
            while True:
                r, _, _ = select.select([self.request, chan], [], [], 15)
                if self.request in r:
                    data = self.request.recv(4096)
                    if not data:
                        break
                    chan.send(data)
                if chan in r:
                    data = chan.recv(4096)
                    if not data:
                        break
                    self.request.sendall(data)
        except OSError:
            pass
        finally:
            chan.close()


class _ForwardServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_password_tunnel(s, f, key):
    """paramiko로 SSH 접속 후, 로컬 포트에서 direct-tcpip 포워딩 서버를 띄운다."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(hostname=s['host'], port=s.get('port', 22),
                       username=s.get('user') or None, password=s.get('password'),
                       timeout=10, banner_timeout=10, auth_timeout=10,
                       look_for_keys=False, allow_agent=False)
    except Exception as e:
        log(f'SSH 접속 실패 ({key}): {e!r}')
        return None

    handler_cls = type('_BoundForwardHandler', (_ForwardHandler,),
                        {'transport': client.get_transport(), 'remote_port': f['remote']})
    try:
        server = _ForwardServer(('127.0.0.1', f['local']), handler_cls)
    except OSError as e:
        log(f'로컬 포트 바인딩 실패 ({key}): {e!r}')
        client.close()
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return {'client': client, 'server': server}


def password_tunnel_alive(entry):
    t = entry['client'].get_transport()
    return t is not None and t.is_active()


def stop_password_tunnel(entry):
    try:
        entry['server'].shutdown()
        entry['server'].server_close()
    except Exception:
        pass
    try:
        entry['client'].close()
    except Exception:
        pass


REPO_ROOT = os.path.dirname(BASE)          # tunnel/ 의 상위 = 프로젝트 루트
DASHBOARD_FILES = ['qlib.py', 'runner.py', 'server.py', 'index.html', 'requirements.txt']
DOCKER_FILES = DASHBOARD_FILES + ['Dockerfile', 'entrypoint.sh']


def ssh_client_for(s):
    """등록된 서버 dict로 인증된 paramiko SSHClient 연결 (비밀번호 있으면 비밀번호, 없으면 키)."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = dict(hostname=s['host'], port=s.get('port', 22), username=s.get('user') or None,
                  timeout=10, banner_timeout=10, auth_timeout=10)
    if s.get('password'):
        kwargs.update(password=s['password'], look_for_keys=False, allow_agent=False)
    client.connect(**kwargs)
    return client


def ssh_exec(client, cmd, timeout=120):
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    return rc, stdout.read().decode(errors='replace'), stderr.read().decode(errors='replace')


def ssh_exec_detached(client, cmd, timeout=20):
    """백그라운드(&)로 데몬을 띄우는 커맨드 전용 — 실행 후에도 채널의 stdout/stderr가 계속 열려있어서
    (redirect/nohup을 걸어도 자식 프로세스가 세션을 물고 있음) .read()를 호출하면 영원히 블록된다.
    exit-status만 받고 스트림은 읽지 않는다."""
    _, stdout, _ = client.exec_command(cmd, timeout=timeout)
    return stdout.channel.recv_exit_status()


def detect_remote_tooling(client):
    rc_docker, *_ = ssh_exec(client, 'command -v docker', timeout=15)
    rc_pip, *_ = ssh_exec(client, 'command -v pip3 || command -v pip', timeout=15)
    return {'docker': rc_docker == 0, 'pip': rc_pip == 0}


def upload_files(client, filenames, remote_dir):
    sftp = client.open_sftp()
    try:
        ssh_exec(client, f'mkdir -p {remote_dir}', timeout=15)
        for name in filenames:
            sftp.put(os.path.join(REPO_ROOT, name), f'{remote_dir}/{name}')
    finally:
        sftp.close()


def install_via_pip(client, remote_dir, env):
    upload_files(client, DASHBOARD_FILES, remote_dir)
    rc, out, err = ssh_exec(
        client, f'cd {remote_dir} && (pip3 install -r requirements.txt || pip install -r requirements.txt)',
        timeout=180)
    if rc != 0:
        return False, f'pip install 실패: {(err or out).strip()[-500:]}'
    env_prefix = ' '.join(f'{k}={v}' for k, v in env.items() if v)
    # 각 커맨드를 서브셸로 감싸야 함 — 'cd x && a & b & sleep 1'은 &&가 a에만 걸리고
    # b는 cd 안 된 원래 디렉토리에서 실행돼버림 (쉘 파싱 우선순위 문제).
    start_cmd = (
        f'(cd {remote_dir} && {env_prefix} nohup python3 runner.py >> runner_stdout.log 2>&1 < /dev/null &) ; '
        f'(cd {remote_dir} && {env_prefix} nohup python3 server.py >> server_stdout.log 2>&1 < /dev/null &) ; '
        f'sleep 1'
    )
    ssh_exec_detached(client, start_cmd, timeout=20)
    return True, 'pip 설치 및 실행 완료'


def install_via_docker(client, remote_dir, env):
    upload_files(client, DOCKER_FILES, remote_dir)
    rc, out, err = ssh_exec(client, f'cd {remote_dir} && docker build -t ai-dashboard .', timeout=300)
    if rc != 0:
        return False, f'docker build 실패: {(err or out).strip()[-500:]}'
    port = env.get('DASH_PORT') or '8080'
    env_flags = ' '.join(f'-e {k}={v}' for k, v in env.items() if v)
    run_cmd = (f'docker rm -f ai-dashboard >/dev/null 2>&1; '
               f'docker run -d --name ai-dashboard -p 127.0.0.1:{port}:8080 {env_flags} ai-dashboard')
    rc, out, err = ssh_exec(client, run_cmd, timeout=60)
    if rc != 0:
        return False, f'docker run 실패: {(err or out).strip()[-500:]}'
    return True, 'Docker 빌드 및 실행 완료'


def monitor_loop():
    procs = {}          # key -> Popen (SSH 키 인증 터널)
    next_retry = {}     # key -> timestamp
    pw_tunnels = {}      # key -> {'client', 'server'} (비밀번호 인증 터널)
    pw_next_retry = {}
    last_cfg = {'servers': []}
    log(f'터널 매니저 시작 — 설정: {CFG_PATH}')
    while True:
        cfg = read_servers()
        if cfg is not None:
            last_cfg = cfg
        desired = desired_tunnels(last_cfg)
        pw_desired = password_tunnels(last_cfg)
        locals_ = local_targets(last_cfg)

        # --- SSH 키 인증 터널 (subprocess) ---
        for key in list(procs):
            if procs[key].poll() is not None:
                rc = procs[key].returncode
                log(f'터널 끊김 (exit {rc}): {key} — {RETRY_SEC}초 후 재시도')
                del procs[key]
                next_retry[key] = time.time() + RETRY_SEC
        for key in list(procs):
            if key not in desired:
                log(f'터널 종료 (목록에서 제거됨): {key}')
                procs[key].terminate()
                del procs[key]
        for key, (cmd, desc) in desired.items():
            if key in procs or time.time() < next_retry.get(key, 0):
                continue
            try:
                procs[key] = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                log(f'터널 시작: {desc}')
            except FileNotFoundError:
                log('ssh 실행 파일을 찾을 수 없음 — OpenSSH 설치 필요')
                next_retry[key] = time.time() + RETRY_SEC

        # --- 비밀번호 인증 터널 (paramiko) ---
        for key in list(pw_tunnels):
            if key not in pw_desired:
                log(f'터널 종료 (목록에서 제거됨): {key}')
                stop_password_tunnel(pw_tunnels.pop(key))
            elif not password_tunnel_alive(pw_tunnels[key]):
                log(f'터널 끊김: {key} — {RETRY_SEC}초 후 재시도')
                stop_password_tunnel(pw_tunnels.pop(key))
                pw_next_retry[key] = time.time() + RETRY_SEC
        for key, (s, f, desc) in pw_desired.items():
            if key in pw_tunnels or time.time() < pw_next_retry.get(key, 0):
                continue
            entry = start_password_tunnel(s, f, key)
            if entry:
                pw_tunnels[key] = entry
                log(f'터널 시작: {desc}')
            else:
                pw_next_retry[key] = time.time() + RETRY_SEC

        with _status_lock:
            _status.clear()
            for key, (_, desc) in desired.items():
                alive = key in procs
                retry_in = 0 if alive else max(0, int(next_retry.get(key, 0) - time.time()))
                _status[key] = {'desc': desc, 'alive': alive, 'retry_in': retry_in}
            for key, (_, _, desc) in pw_desired.items():
                alive = key in pw_tunnels
                retry_in = 0 if alive else max(0, int(pw_next_retry.get(key, 0) - time.time()))
                _status[key] = {'desc': desc, 'alive': alive, 'retry_in': retry_in}
            for key, (port, desc) in locals_.items():
                _status[key] = {'desc': desc, 'alive': check_local_port(port), 'retry_in': 0}

        time.sleep(POLL_SEC)


app = FastAPI()


@app.get('/')
def ui_index():
    return FileResponse(UI_INDEX, media_type='text/html; charset=utf-8')


_PROXY_EXCLUDED_REQUEST_HEADERS = {'host', 'content-length'}
_PROXY_EXCLUDED_RESPONSE_HEADERS = {'content-length', 'transfer-encoding', 'content-encoding', 'connection'}


@app.api_route('/dash/{idx}', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
@app.api_route('/dash/{idx}/{path:path}', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
async def dash_proxy(idx: int, request: Request, path: str = ''):
    """등록된 서버의 대시보드를 이 프로세스(8090) 경유로 중계 — iframe이 다른 포트를 직접 가리키지 않게 함."""
    cfg = read_servers() or {'servers': []}
    servers = cfg.get('servers', [])
    if not 0 <= idx < len(servers):
        return JSONResponse({'error': 'index out of range'}, status_code=404)
    forwards = servers[idx].get('forwards', [])
    if not forwards:
        return JSONResponse({'error': 'no forward configured for this server'}, status_code=404)
    port = forwards[0]['local']
    url = f"http://127.0.0.1:{port}/{path}"
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _PROXY_EXCLUDED_REQUEST_HEADERS}
    try:
        async with httpx.AsyncClient() as client:
            upstream = await client.request(
                request.method, url, params=request.query_params,
                content=body, headers=headers, timeout=15)
    except httpx.RequestError as e:
        return JSONResponse({'error': f'대시보드에 연결할 수 없음: {e!r}'}, status_code=502)
    resp_headers = {k: v for k, v in upstream.headers.items()
                    if k.lower() not in _PROXY_EXCLUDED_RESPONSE_HEADERS}
    return Response(content=upstream.content, status_code=upstream.status_code, headers=resp_headers)


@app.get('/api/status')
def api_status():
    cfg = read_servers() or {'servers': []}
    servers = []
    for i, s in enumerate(cfg.get('servers', [])):
        servers.append({
            'idx': i, 'name': s.get('name', s['host']), 'host': s['host'],
            'user': s.get('user', ''), 'port': s.get('port', 22),
            'enabled': s.get('enabled', True), 'forwards': s.get('forwards', []),
            'local': bool(s.get('local')),
            'auth': 'password' if s.get('password') else 'key',
        })
    with _status_lock:
        tunnels = dict(_status)
    return {'servers': servers, 'tunnels': tunnels}


@app.post('/api/servers')
async def add_server(body: dict):
    is_local = bool(body.get('is_local'))
    if is_local:
        try:
            port = int(body.get('local') or 0)
        except (TypeError, ValueError):
            return JSONResponse({'ok': False, 'error': '포트는 숫자여야 함'}, status_code=400)
        if not port:
            return JSONResponse({'ok': False, 'error': '포트는 필수'}, status_code=400)
        cfg = read_servers() or {'servers': []}
        servers = cfg.setdefault('servers', [])
        servers.append({
            'name': (body.get('name') or '').strip() or f'local:{port}',
            'host': '127.0.0.1',
            'local': True,
            'enabled': True,
            'forwards': [{'local': port, 'remote': port}],
        })
        write_servers(cfg)
        return {'ok': True, 'idx': len(servers) - 1}

    host = (body.get('host') or '').strip()
    local = body.get('local')
    if not host or not local:
        return JSONResponse({'ok': False, 'error': 'host와 local 포트는 필수'}, status_code=400)
    try:
        local = int(local)
        remote = int(body.get('remote') or local)
        port = int(body.get('port') or 22)
    except (TypeError, ValueError):
        return JSONResponse({'ok': False, 'error': '포트는 숫자여야 함'}, status_code=400)
    entry = {
        'name': (body.get('name') or '').strip() or host,
        'host': host,
        'user': (body.get('user') or '').strip(),
        'port': port,
        'enabled': True,
        'forwards': [{'local': local, 'remote': remote}],
    }
    password = (body.get('password') or '').strip()
    if password:
        entry['password'] = password
    cfg = read_servers() or {'servers': []}
    servers = cfg.setdefault('servers', [])
    servers.append(entry)
    write_servers(cfg)
    return {'ok': True, 'idx': len(servers) - 1}


@app.delete('/api/servers/{idx}')
def delete_server(idx: int):
    cfg = read_servers() or {'servers': []}
    servers = cfg.get('servers', [])
    if not 0 <= idx < len(servers):
        return JSONResponse({'ok': False, 'error': 'index out of range'}, status_code=400)
    servers.pop(idx)
    write_servers(cfg)
    return {'ok': True}


@app.post('/api/servers/{idx}/toggle')
def toggle_server(idx: int):
    cfg = read_servers() or {'servers': []}
    servers = cfg.get('servers', [])
    if not 0 <= idx < len(servers):
        return JSONResponse({'ok': False, 'error': 'index out of range'}, status_code=400)
    servers[idx]['enabled'] = not servers[idx].get('enabled', True)
    write_servers(cfg)
    return {'ok': True, 'enabled': servers[idx]['enabled']}


@app.post('/api/servers/{idx}/install')
def install_server(idx: int, body: dict):
    """등록된 원격 서버에 SSH로 접속해 대시보드 코드를 올리고(pip 또는 Docker) 실행까지 한다."""
    cfg = read_servers() or {'servers': []}
    servers = cfg.get('servers', [])
    if not 0 <= idx < len(servers):
        return JSONResponse({'ok': False, 'error': 'index out of range'}, status_code=400)
    s = servers[idx]
    if s.get('local'):
        return JSONResponse({'ok': False, 'error': '로컬 서버는 자동 설치 대상이 아님'}, status_code=400)

    remote_dir = (body.get('remote_dir') or 'ai_dashboard').strip()
    remote_port = s['forwards'][0]['remote'] if s.get('forwards') else 8080
    env = {
        'DASH_CODE_DIR': (body.get('DASH_CODE_DIR') or '').strip(),
        'DASH_EXP_DIR': (body.get('DASH_EXP_DIR') or '').strip(),
        'DASH_TORCHRUN': (body.get('DASH_TORCHRUN') or '').strip(),
        'DASH_GPUS': (body.get('DASH_GPUS') or '').strip(),
        'DASH_PORT': str(remote_port),
    }

    try:
        client = ssh_client_for(s)
    except Exception as e:
        return JSONResponse({'ok': False, 'error': f'SSH 접속 실패: {e!r}'}, status_code=502)

    try:
        avail = detect_remote_tooling(client)
        method = (body.get('method') or 'auto').strip()
        if method == 'auto':
            method = 'pip' if avail['pip'] else ('docker' if avail['docker'] else None)
        if method is None:
            return JSONResponse({'ok': False, 'error': '원격에 pip/docker 둘 다 없음 — 수동 설치 필요'},
                                 status_code=400)
        if method not in ('pip', 'docker'):
            return JSONResponse({'ok': False, 'error': f'알 수 없는 method: {method}'}, status_code=400)
        if not avail[method]:
            return JSONResponse({'ok': False, 'error': f'원격에 {method} 없음 (감지: {avail})'}, status_code=400)

        installer = install_via_pip if method == 'pip' else install_via_docker
        ok, message = installer(client, remote_dir, env)
    except Exception as e:
        return JSONResponse({'ok': False, 'error': f'설치 중 오류: {e!r}'}, status_code=500)
    finally:
        client.close()

    return {'ok': ok, 'method': method, 'message': message}


def main():
    import uvicorn
    threading.Thread(target=monitor_loop, daemon=True).start()
    log(f'등록 UI: http://{UI_BIND}:{UI_PORT}')
    uvicorn.run(app, host=UI_BIND, port=UI_PORT, log_level='warning')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n종료')
        sys.exit(0)
