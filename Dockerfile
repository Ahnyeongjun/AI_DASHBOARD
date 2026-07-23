# 큐 러너 + 대시보드 컨테이너.
# torchrun/torch 자체는 포함하지 않음 — 학습 환경(conda/venv)을 볼륨으로 마운트해서 사용한다.
# 이미 GPU 학습용 베이스 이미지(nvcr.io/nvidia/pytorch, pytorch/pytorch 등)를 쓴다면
# 이 이미지의 FROM 줄만 그걸로 바꿔서 확장해도 된다.
FROM python:3.11-slim

# stdout 버퍼링 끔 — 안 하면 docker logs에 print 출력이 안 보임
ENV PYTHONUNBUFFERED=1

# pgrep으로 torchrun 프로세스를 찾으므로 procps 필요
RUN apt-get update && apt-get install -y --no-install-recommends procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY qlib.py runner.py server.py index.html entrypoint.sh ./
RUN chmod +x entrypoint.sh

# 컨테이너 내부는 0.0.0.0으로 바인딩하고, 호스트 노출은 docker-compose의 ports에서 제어한다
# (127.0.0.1:PORT:8080 처럼 로컬로만 열어서 기존 tunnel.py 보안 모델 유지 권장)
ENV DASH_BIND=0.0.0.0
EXPOSE 8080

ENTRYPOINT ["./entrypoint.sh"]
