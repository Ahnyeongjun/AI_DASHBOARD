"""GPU 학습 큐 대시보드용 MCP 서버 (로컬 PC에서 실행).

tunnel/tunnel.py가 열어둔 SSH 포트포워딩을 통해 대시보드의 /api/status, /api/action에 접근한다.
Claude Code/Desktop에 등록하면 curl 없이 "지금 몇 epoch야?", "training_71 큐에 추가해줘" 같은
요청을 대화 중에 바로 처리할 수 있다.

전제: tunnel/tunnel.py가 이미 실행 중이어야 함 (등록된 서버로의 로컬 포트포워딩이 열려 있어야 조회 가능).

설치 / 등록:
    pip install -r requirements.txt
    claude mcp add gpu-queue -- python /absolute/path/to/mcp/server.py
"""
import json
import os
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

BASE = os.path.dirname(os.path.abspath(__file__))
SERVERS_PATH = os.path.join(BASE, '..', 'tunnel', 'servers.json')

mcp = FastMCP("gpu-queue")


def _load_servers():
    try:
        with open(SERVERS_PATH, encoding='utf-8') as f:
            return json.load(f).get('servers', [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _resolve_port(server: Optional[str]):
    """server 이름/host로 로컬 포워딩 포트를 찾는다. 생략 시 등록된 서버가 하나뿐이면 그걸 사용."""
    servers = [s for s in _load_servers() if s.get('enabled', True) and s.get('forwards')]
    if server:
        for s in servers:
            if s.get('name') == server or s['host'] == server:
                return s['forwards'][0]['local'], s.get('name', s['host'])
        raise ValueError(f"등록된 서버 없음: {server} (list_gpu_servers로 확인)")
    if len(servers) == 1:
        s = servers[0]
        return s['forwards'][0]['local'], s.get('name', s['host'])
    if not servers:
        raise ValueError(
            "등록된 GPU 서버가 없음 — tunnel.py의 등록 UI(기본 http://localhost:8090)에서 먼저 추가하세요")
    names = ', '.join(s.get('name', s['host']) for s in servers)
    raise ValueError(f"서버가 여러 개 등록됨 — server 인자로 이름 지정 필요: {names}")


def _get(server, path):
    port, name = _resolve_port(server)
    r = httpx.get(f"http://localhost:{port}{path}", timeout=10)
    r.raise_for_status()
    return r.json(), name


def _post(server, path, body):
    port, name = _resolve_port(server)
    r = httpx.post(f"http://localhost:{port}{path}", json=body, timeout=10)
    r.raise_for_status()
    return r.json(), name


@mcp.tool()
def list_gpu_servers() -> list:
    """등록된 GPU 서버 목록(이름, host, 로컬 포워딩 포트, 활성 여부)을 반환.
    서버가 여러 대 등록돼 있을 때 다른 도구의 server 인자에 뭘 넘길지 확인하는 용도."""
    servers = _load_servers()
    return [{
        'name': s.get('name', s['host']), 'host': s['host'],
        'enabled': s.get('enabled', True),
        'local_ports': [f['local'] for f in s.get('forwards', [])],
    } for s in servers]


@mcp.tool()
def get_queue_status(server: Optional[str] = None) -> dict:
    """GPU별 메모리/사용률, 진행 중 학습(epoch, mIoU, best 스코어), 대기 큐, 실행 히스토리,
    큐 러너 상태를 조회한다.
    server: 등록된 서버 이름 또는 host. 등록된 서버가 하나뿐이면 생략 가능 (list_gpu_servers로 확인)."""
    data, name = _get(server, '/api/status')
    return {'server': name, **data}


@mcp.tool()
def pause_training_queue(server: Optional[str] = None) -> dict:
    """대기 큐를 일시정지한다. 진행 중인 학습은 그대로 계속되고, 다음 대기 항목만 시작되지 않는다."""
    data, name = _post(server, '/api/action', {'action': 'pause'})
    return {'server': name, **data}


@mcp.tool()
def resume_training_queue(server: Optional[str] = None) -> dict:
    """일시정지된 대기 큐를 재개한다."""
    data, name = _post(server, '/api/action', {'action': 'resume'})
    return {'server': name, **data}


@mcp.tool()
def add_to_training_queue(num: int, config: str, server: Optional[str] = None) -> dict:
    """대기 큐에 학습 실험을 추가한다.
    num: training_{num} 출력 디렉토리 번호. config: config 파일 경로 (학습 코드 디렉토리 기준 상대경로 가능)."""
    data, name = _post(server, '/api/action', {'action': 'add', 'num': num, 'config': config})
    return {'server': name, **data}


@mcp.tool()
def remove_from_training_queue(index: int, server: Optional[str] = None) -> dict:
    """대기 큐에서 index번째(0부터 시작) 항목을 제거한다. get_queue_status의 queue 배열 순서 기준."""
    data, name = _post(server, '/api/action', {'action': 'remove', 'index': index})
    return {'server': name, **data}


@mcp.tool()
def move_training_queue_item(index: int, direction: str, server: Optional[str] = None) -> dict:
    """대기 큐 항목의 순서를 변경한다. direction은 'up' 또는 'down'."""
    action = {'up': 'move_up', 'down': 'move_down'}.get(direction)
    if action is None:
        return {'ok': False, 'error': "direction은 'up' 또는 'down'이어야 함"}
    data, name = _post(server, '/api/action', {'action': action, 'index': index})
    return {'server': name, **data}


if __name__ == '__main__':
    mcp.run()
