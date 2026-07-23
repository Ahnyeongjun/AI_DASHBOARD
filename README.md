# AI 학습 큐 대시보드

GPU 서버에서 딥러닝 학습(torchrun)을 **순차 큐로 자동 실행**하고, 웹 UI로 모니터링/관리하는 도구.
외부 의존성 없이 Python 표준 라이브러리만 사용한다 (학습 환경에 pip 설치 불필요).

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
| `server.py` | 웹 서버 (stdlib `http.server`) |
| `index.html` | 대시보드 화면 |
| `qlib.py` | 공용: flock 기반 큐 I/O, torchrun 탐지, 학습 로그 파싱, nvidia-smi |
| `start_ui.example.sh` | 시작 스크립트 템플릿 → `start_ui.sh`로 복사 후 수정 |
| `queue.json` | 큐 상태 (런타임 생성, gitignore) |

## 설치 / 실행
```bash
cp start_ui.example.sh start_ui.sh
vi start_ui.sh          # DASH_* 환경변수를 서버에 맞게 수정
bash start_ui.sh        # 러너 + 서버 시작 (재부팅 후에도 이거 한 번이면 복구)
```

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

## 접속
기본은 localhost 바인딩이라 SSH 포트포워딩으로 접속:
```bash
ssh -L 8080:localhost:8080 user@<서버>
# 브라우저에서 http://localhost:8080
```

매번 `-L` 치기 귀찮으면 로컬 PC의 `~/.ssh/config`에 저장해두면 **접속만 해도 자동 포워딩**된다:
```
Host gpu-server
    HostName <서버IP>
    User <계정>
    LocalForward 8080 localhost:8080
```
이후 `ssh gpu-server`만 하면 http://localhost:8080 이 바로 열린다.
(VS Code Remote-SSH 사용 시엔 포트가 자동 포워딩되므로 설정 불필요)

## 큐 API
```bash
curl http://localhost:8080/api/status
curl -X POST http://localhost:8080/api/action -d '{"action":"add","num":70,"config":"configs/train_70.json"}'
curl -X POST http://localhost:8080/api/action -d '{"action":"pause"}'   # resume, remove, move_up, move_down
```

## 주의
- 진행 중 학습을 중단하는 버튼은 의도적으로 없음 (실수 방지) — 중단은 터미널에서.
- 서버 재부팅 시 러너/서버/진행 중 학습 모두 죽음 → 부팅 후 `bash start_ui.sh` 재실행.
- 로그 파싱 정규식(`qlib.parse_training_log`)은 `Epoch N | val mIoU=...` 형식 기준 — 다른 로그 포맷이면 수정 필요.
