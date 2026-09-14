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
# 기본 경로가 /tmp 가 아닌 이유: macOS 는 /private/tmp 를 주기적으로 비운다
# (periodic daily — 며칠 손대지 않은 파일을 지운다). 이 로그는 정확히 "며칠 전
# 한 번 이상했는데" 를 되짚을 때 꺼내는 물건이라 그 청소에 걸리면 안 된다.
# ~/Library/Logs 는 macOS 가 사용자 로그용으로 두는 자리라 재부팅·청소에도
# 남고, 저장소 안에 두는 것과 달리 .gitignore 항목도 체크아웃마다 따로 쌓이는
# 사본도 필요 없다. MODEL_LOG 로 어디로든 옮기는 통로는 그대로다.
LOG="${MODEL_LOG:-$HOME/Library/Logs/docs_rag/models.log}"
LOG_MAX_BYTES="${MODEL_LOG_MAX_BYTES:-5242880}"  # 5MB

model_up() { curl -sf -m 3 "http://127.0.0.1:$MODEL_PORT/health" >/dev/null 2>&1; }

# 로그를 지우지 않고 이어 쓰므로(아래 start_models 참고) 상한이 필요하다.
# 기동할 때 딱 한 번 크기를 보고, 넘었으면 한 세대($LOG.1)만 굴린다.
# 실행 중에는 보지 않는다 — 한 번의 실행이 폭주하면 5MB 를 넘길 수 있다는
# 뜻이고, 그건 감수한다. 로컬 편의 스크립트에 로테이션 감시자를 두는 건
# 과하고, 실제로 새는 경로는 "기동을 반복하며 쌓이는" 쪽이지 "한 번에
# 터지는" 쪽이 아니다. 세대를 하나만 두는 이유도 같다: 이어 쓰기라 직전
# 기동의 로그는 보통 $LOG 안에 그대로 있고, $LOG.1 은 넘침 받이일 뿐이다.
rotate_log() {
    [ -f "$LOG" ] || return 0
    local size
    size=$(wc -c < "$LOG" | tr -d '[:space:]') || return 0
    [ "${size:-0}" -ge "$LOG_MAX_BYTES" ] || return 0
    # 회전이 실패해도 기동은 막지 않는다. set -e 아래라 여기서 죽으면 로그
    # 파일 하나 때문에 스택 전체가 안 뜬다 — 상한은 기동보다 덜 중요하다.
    mv -f "$LOG" "$LOG.1" || echo "  로그 회전 실패 — 그대로 이어 씀" >&2
    return 0
}

start_models() {
    if model_up; then
        echo "모델 서버 이미 떠 있음 (:$MODEL_PORT)"
        return
    fi
    mkdir -p "$(dirname "$LOG")"
    rotate_log
    echo "모델 서버 기동 중 (MPS, 로그: $LOG)"
    # `>` 가 아니라 `>>` 다. 예전엔 기동할 때마다 truncate 해서, 서버가 죽어
    # 다시 띄우는 순간 죽은 이유가 같이 사라졌다 — 사후분석이 필요한 바로 그
    # 시점에. 아래 실패 안내가 가리키는 파일도 이 파일이다. ee4518c 가
    # "모델 서버가 액세스 로그를 찍지 않아 요청이 도착했는지조차 알 수
    # 없었다"를 고치려고 이 로그를 살려놨는데, 재기동이 그걸 매번 지웠다.
    # 이어 붙는 만큼 실행 경계가 안 보이므로 기동마다 구분선을 하나 넣는다.
    printf '\n===== %s  모델 서버 기동 (:%s) =====\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "$MODEL_PORT" >> "$LOG"
    PYTHONUNBUFFERED=1 nohup "$PY" -m scripts.local_model_server \
        --port "$MODEL_PORT" >> "$LOG" 2>&1 &
    # 캐시된 가중치로도 로드에 15초 안팎 걸린다.
    for _ in $(seq 1 60); do
        model_up && { echo "  준비됨"; return; }
        sleep 2
    done
    # 파일에 과거 기동까지 남아 있으니 "확인" 이 아니라 끝부분을 짚어준다.
    echo "모델 서버가 뜨지 않음 — tail -n 50 $LOG" >&2
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
