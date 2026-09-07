#!/usr/bin/env bash
# 로컬 스택 한 번에 올리고 내리기 (macOS / Apple Silicon).
#
#   scripts/stack.sh up      호스트 모델 서버(MPS) + 컨테이너 스택
#   scripts/stack.sh down    둘 다 내림 (볼륨은 남긴다)
#   scripts/stack.sh status
#
# 모델 서버가 왜 컨테이너 밖에 있나: macOS Docker 는 리눅스 VM 에서 돌고
# Metal 이 전달되지 않는다. 컨테이너 CPU 로 리랭킹하면 실제 크기 청크 50개에
# 184초가 걸려 클라이언트 타임아웃(120초)을 넘겨 503 이 된다. 같은 작업이
# 호스트 MPS 에서는 10.6초다. 그래서 이 한 조각만 호스트에 남는다.
set -euo pipefail

cd "$(dirname "$0")/.."

PY="${PY:-.venv/bin/python}"
MODEL_PORT="${MODEL_PORT:-8081}"
HTTP_PORT="${HTTP_PORT:-8088}"
LOG="${MODEL_LOG:-/tmp/docs_rag_models.log}"

model_up() { curl -sf -m 3 "http://127.0.0.1:$MODEL_PORT/health" >/dev/null 2>&1; }

start_models() {
    if model_up; then
        echo "모델 서버 이미 떠 있음 (:$MODEL_PORT)"
        return
    fi
    echo "모델 서버 기동 중 (MPS, 로그: $LOG)"
    PYTHONUNBUFFERED=1 nohup "$PY" -m scripts.local_model_server \
        --port "$MODEL_PORT" > "$LOG" 2>&1 &
    # 캐시된 가중치로도 로드에 15초 안팎 걸린다.
    for _ in $(seq 1 60); do
        model_up && { echo "  준비됨"; return; }
        sleep 2
    done
    echo "모델 서버가 뜨지 않음 — $LOG 확인" >&2
    exit 1
}

case "${1:-up}" in
up)
    start_models
    echo "컨테이너 스택 기동"
    docker compose up -d
    for _ in $(seq 1 90); do
        curl -sf -m 3 "http://localhost:$HTTP_PORT/api/health" >/dev/null 2>&1 && break
        sleep 2
    done
    echo
    echo "  http://localhost:$HTTP_PORT"
    docker compose ps --format "  {{.Service}}  {{.Status}}"
    ;;
down)
    echo "컨테이너 스택 정지"
    # -v 는 절대 붙이지 않는다: pgdata(DB) · hf-cache(가중치 6.9GB) ·
    # uploads(업로드 원본) 가 함께 사라진다.
    docker compose down
    if pgrep -f "scripts.local_model_server" >/dev/null 2>&1; then
        echo "모델 서버 정지"
        pkill -f "scripts.local_model_server" || true
        # 신호만 보내고 끝내면 안 된다. 토치 정리에 시간이 걸려 프로세스가
        # 잠시 남고, 곧바로 status 를 보면 아직 떠 있는 것처럼 보이며,
        # 곧바로 up 을 하면 포트를 두고 새 서버와 경합한다.
        for _ in $(seq 1 30); do
            pgrep -f "scripts.local_model_server" >/dev/null 2>&1 || break
            sleep 1
        done
        if pgrep -f "scripts.local_model_server" >/dev/null 2>&1; then
            echo "  정상 종료하지 않아 강제 종료"
            pkill -9 -f "scripts.local_model_server" || true
        fi
    fi
    echo "완료 — 볼륨은 그대로. 다시 올리려면 scripts/stack.sh up"
    ;;
status)
    if model_up; then
        echo "모델 서버(:$MODEL_PORT)  UP"
    elif pgrep -f "scripts.local_model_server" >/dev/null 2>&1; then
        # 포트는 닫혔는데 프로세스가 남아 있는 구간이 실제로 존재한다.
        echo "모델 서버(:$MODEL_PORT)  종료 중"
    else
        echo "모델 서버(:$MODEL_PORT)  DOWN"
    fi
    docker compose ps --format "  {{.Service}}  {{.Status}}"
    curl -sf -m 3 "http://localhost:$HTTP_PORT/api/health" >/dev/null 2>&1 \
        && echo "앱(:$HTTP_PORT)  UP" || echo "앱(:$HTTP_PORT)  DOWN"
    ;;
*)
    echo "usage: scripts/stack.sh [up|down|status]" >&2
    exit 2
    ;;
esac
