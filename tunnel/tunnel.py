"""SSH 자동 포트포워딩 매니저 (로컬 PC에서 실행).

servers.json의 서버 목록을 5초마다 읽어:
- 목록에 있는 서버는 ssh -N -L 터널을 유지 (죽으면 자동 재접속, 백오프 30초)
- 목록에서 빠지거나 enabled=false가 되면 터널 종료
- 파일에 서버를 추가하면 재시작 없이 자동으로 새 터널이 열림

전제: 대상 서버에 SSH 키 인증이 설정돼 있어야 함 (BatchMode라 비밀번호 입력 불가).
Windows(OpenSSH)/macOS/Linux 공통, Python 3.7+ 표준 라이브러리만 사용.

사용:
    cp servers.example.json servers.json   # 서버 목록 작성
    python tunnel.py                        # 포그라운드 실행 (로그 stdout)
부팅 자동시작 등록은 README 참고.
"""
import datetime
import json
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, 'servers.json')
POLL_SEC = 5
RETRY_SEC = 30  # 접속 실패 시 재시도 간격


def log(msg):
    ts = datetime.datetime.now().strftime('%H:%M:%S')
    print(f'[{ts}] {msg}', flush=True)


def load_config():
    try:
        with open(CFG_PATH, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        log(f'설정 파일 없음: {CFG_PATH} (servers.example.json을 복사해 작성하세요)')
        return {'servers': []}
    except json.JSONDecodeError as e:
        log(f'servers.json 파싱 오류: {e} — 이전 상태 유지')
        return None


def desired_tunnels(cfg):
    """설정 → {키: (ssh 커맨드, 설명)} 매핑."""
    out = {}
    for s in cfg.get('servers', []):
        if not s.get('enabled', True):
            continue
        target = (s['user'] + '@' if s.get('user') else '') + s['host']
        for f in s.get('forwards', []):
            key = f"{target}:{s.get('port', 22)} L{f['local']}->{f['remote']}"
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


def main():
    procs = {}        # key -> Popen
    next_retry = {}   # key -> timestamp
    last_desired = {}
    log(f'터널 매니저 시작 — 설정: {CFG_PATH}')
    while True:
        cfg = load_config()
        if cfg is not None:
            last_desired = desired_tunnels(cfg)
        desired = last_desired

        # 죽은 터널 정리
        for key in list(procs):
            if procs[key].poll() is not None:
                rc = procs[key].returncode
                log(f'터널 끊김 (exit {rc}): {key} — {RETRY_SEC}초 후 재시도')
                del procs[key]
                next_retry[key] = time.time() + RETRY_SEC

        # 설정에서 빠진 터널 종료
        for key in list(procs):
            if key not in desired:
                log(f'터널 종료 (목록에서 제거됨): {key}')
                procs[key].terminate()
                del procs[key]

        # 새 터널 시작
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

        time.sleep(POLL_SEC)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n종료')
        sys.exit(0)
