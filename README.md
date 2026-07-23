# AI 학습 큐 대시보드

GPU 서버에서 딥러닝 학습(torchrun)을 **순차 큐로 자동 실행**하고, 웹 UI로 모니터링/관리하는 도구.
웹 서버는 FastAPI 기반 — GPU 서버에 `pip install -r requirements.txt` 필요 (큐 러너 자체는 stdlib만 사용).

## 기능
- **큐 러너 데몬**: `queue.json` 맨 앞 항목을 꺼내 `torchrun --nproc_per_node=N train.py --config <cfg>`로 실행.
  외부에서 수동 실행 중인 torchrun이 있으면 끝날 때까지 대기. 한 실험이 실패해도 다음 항목 진행.
  학습 프로세스는 세션 분리(start_new_session) — 러너가 죽어도 학습은 계속됨.
- **웹 대시보드** (5초 폴링):
  - GPU별 메모리/utilization
  - 진행 중 학습: epoch 진행률, mIoU 곡선(7cls + 6cls_ndvi), best 스코어 (로그 파일 파싱)
  - 대기 큐: 추가 / 삭제 / 순서변경(▲▼) / 일시정지·재개
  - 실행 히스토리 (exit code 포함)

## 구성
| 파일 | 역할 |
|------|------|
| `runner.py` | 큐 러너 데몬 |
| `server.py` | 웹 서버 (FastAPI + uvicorn) |
| `index.html` | 대시보드 화면 |
| `qlib.py` | 공용: flock 기반 큐 I/O, torchrun 탐지, 학습 로그 파싱, nvidia-smi |
| `start_ui.example.sh` | 시작 스크립트 템플릿 → `start_ui.sh`로 복사 후 수정 |
| `Dockerfile` / `entrypoint.sh` | 러너+서버 컨테이너 이미지 |
| `docker-compose.example.yml` | 컴포즈 템플릿 → `docker-compose.yml`로 복사 후 수정 |
| `tunnel/` | GPU 학습 콘솔: SSH 자동 포트포워딩 + 서버 등록 + 대시보드 열람 UI (로컬 PC) |
| `mcp/` | 큐 API를 노출하는 MCP 서버 (로컬 PC) |
| `queue.json` | 큐 상태 (런타임 생성, gitignore) |

## 설치 / 실행 (GPU 서버)

### A. 로컬 프로세스로 직접 실행
```bash
pip install -r requirements.txt   # fastapi, uvicorn
cp start_ui.example.sh start_ui.sh
vi start_ui.sh          # DASH_* 환경변수를 서버에 맞게 수정
bash start_ui.sh        # 러너 + 서버 시작 (재부팅 후에도 이거 한 번이면 복구)
```

### B. Docker로 실행
학습 환경(torch/torchrun)은 conda env를 볼륨으로 마운트해서 쓰는 걸 기본으로 한다
(자체 GPU 학습 이미지가 있다면 `Dockerfile`의 `FROM`을 그걸로 바꿔서 확장).
```bash
cp docker-compose.example.yml docker-compose.yml
vi docker-compose.yml   # 볼륨 경로(코드/experiments/conda env), DASH_GPUS 등을 환경에 맞게 수정
docker compose up -d --build
```
- GPU 전달에는 `nvidia-container-toolkit` 설치가 전제 (`nvidia-smi`, torchrun 모두 GPU 필요).
- `restart: unless-stopped`이므로 서버 재부팅 시 Docker 데몬이 자동으로 재기동한다 (수동 재실행 불필요).
- 포트는 `127.0.0.1:8080:8080`처럼 로컬로만 여는 걸 권장 — 외부 접속은 아래 `tunnel/`로 SSH 포워딩.

### 환경변수
| 변수 | 기본값 | 설명 |
|------|--------|------|
| `DASH_CODE_DIR` | cwd | `train.py`가 있는 학습 코드 디렉토리 |
| `DASH_EXP_DIR` | `./experiments` | `training_{N}` 출력 디렉토리 상위 |
| `DASH_TORCHRUN` | `torchrun` | torchrun 실행 파일 경로 |
| `DASH_GPUS` | `0` | CUDA_VISIBLE_DEVICES |
| `DASH_NPROC` | GPU 수 | torchrun nproc_per_node |
| `DASH_PORT` | `8080` | 웹 서버 포트 |
| `DASH_BIND` | `127.0.0.1` | 바인드 주소. LAN 공개 시 `0.0.0.0` (인증 없음 — 신뢰망에서만) |

## GPU 학습 콘솔 — 서버 등록 + 대시보드 통합 (`tunnel/`, 로컬 PC에서 실행)
GPU 서버 대시보드는 localhost 바인딩이므로 원격 서버는 로컬 PC에서 SSH 터널을 열어야 접근할 수 있다.
`tunnel/tunnel.py`가 이 터널 관리 + 서버 등록 + 대시보드 열람을 **하나의 웹앱**으로 제공한다
(대시보드 자체는 여전히 각 GPU 서버에서 돌고, 이 앱은 로컬 PC에서 그걸 모아 보여주는 창구 역할).

```bash
cd tunnel
pip install -r requirements.txt   # fastapi, uvicorn, paramiko
python tunnel.py                  # http://localhost:8090
```
`http://localhost:8090` 접속 → **서버 관리** 탭에서 등록:
- **원격 GPU 서버**: 이름/Host/SSH 계정/포트/로컬·원격 포트 입력 후 추가 → 5초 내 자동으로 SSH 터널이 열림.
  - 기본은 **SSH 키 인증** (`ssh-copy-id user@서버` 한 번 필요) — 외부 `ssh` 프로세스로 터널을 띄운다.
  - 키를 못 쓰는 경우 **비밀번호**를 입력하면 그걸로 접속 (paramiko로 순수 파이썬 구현, 외부 sshpass/plink 불필요).
    단, `servers.json`에 **평문 저장**되므로(gitignore되지만 로컬 디스크 파일) 신뢰 가능한 개인 PC에서만 사용할 것.
- **이 PC 자체**: "이 PC (로컬)" 체크 후 포트만 입력 — 로컬 PC에 GPU가 있어서 대시보드가 이미 `localhost:포트`에
  떠 있는 경우, SSH/터널 없이 포트 응답만 주기적으로 확인해서 연결 상태를 표시.

**대시보드** 탭에서는 연결된(원격이든 로컬이든) 서버를 버튼으로 골라 그 학습 큐 대시보드를 iframe으로 바로 볼 수 있다
— 서버마다 브라우저 탭을 따로 열 필요 없음.

`servers.json`을 텍스트로 직접 수정해도 되며(UI와 동일 파일, gitignore됨), 여러 대 등록 시 로컬 포트만 겹치지 않게 다르게 설정.
- 전제(SSH 키 인증 사용 시): 대상 서버에 **SSH 키 인증** 설정 (`ssh-copy-id user@서버` 한 번)
- UI 포트는 `TUNNEL_UI_PORT`(기본 8090) / `TUNNEL_UI_BIND`(기본 127.0.0.1) 환경변수로 변경 가능

### 부팅 시 자동 시작 (로컬 PC)
- **Windows**: `Win+R` → `shell:startup` → 바로가기 생성, 대상: `pythonw.exe C:\...\tunnel\tunnel.py`
- **macOS/Linux**: `crontab -e` → `@reboot python3 /path/to/tunnel/tunnel.py >> /tmp/tunnel.log 2>&1`
- 수동 대안: `~/.ssh/config`에 `LocalForward 8080 localhost:8080` 등록 시 일반 ssh 접속만으로도 포워딩됨
  (VS Code Remote-SSH는 포트 자동 포워딩이라 설정 불필요)

## 큐 API
```bash
curl http://localhost:8080/api/status
curl -X POST http://localhost:8080/api/action -d '{"action":"add","num":70,"config":"configs/train_70.json"}'
curl -X POST http://localhost:8080/api/action -d '{"action":"pause"}'   # resume, remove, move_up, move_down
```

## MCP 도구 (`mcp/`, 로컬 PC에서 실행)
curl 없이 Claude Code/Desktop 대화 중에 "지금 몇 epoch야?", "training_71 큐에 추가해줘" 같은 요청을
바로 처리할 수 있게 큐 API를 MCP 툴로 노출한다. `tunnel/tunnel.py`가 열어둔 로컬 포워딩 포트로 접근하므로
**tunnel.py가 먼저 실행 중이어야** 한다. Python 3.10+ 필요.

```bash
cd mcp
pip install -r requirements.txt        # mcp, httpx
claude mcp add gpu-queue -- python "$(pwd)/server.py"
```
등록 후 Claude Code에서 `list_gpu_servers`, `get_queue_status`, `add_to_training_queue`,
`pause_training_queue` / `resume_training_queue`, `remove_from_training_queue`,
`move_training_queue_item` 도구를 사용할 수 있다. 등록된 GPU 서버가 하나뿐이면 `server` 인자는 생략 가능,
여러 대면 `list_gpu_servers`로 확인한 이름을 지정.

## 주의
- 진행 중 학습을 중단하는 버튼은 의도적으로 없음 (실수 방지) — 중단은 터미널에서.
- 서버 재부팅 시 러너/서버/진행 중 학습 모두 죽음 → 부팅 후 `bash start_ui.sh` 재실행 (Docker는 `restart: unless-stopped`로 자동 복구).
  서버 crontab에 `@reboot ... start_ui.sh boot` 등록하면 자동 복구 — boot 모드는 큐를 **일시정지**로 올리므로
  재부팅으로 죽은 학습을 확인하고 UI에서 재개하면 된다.
- 로그 파싱 정규식(`qlib.parse_training_log`)은 `Epoch N | val mIoU=...` 형식 기준 — 다른 로그 포맷이면 수정 필요.
