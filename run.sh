#!/bin/bash
# 本地编排:Redis + Python 管理面(:2001) + Rust 热服务(:2002) + Nginx(:8080)
# 用法:bash run.sh   访问 http://127.0.0.1:8080
set -o pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1

NGINX_CONF=/tmp/club_nginx.conf
RUST_BIN="$DIR/club-hot/target/release/club-hot"
PYTHON_PID_FILE=/tmp/club_py.pid
RUST_PID_FILE=/tmp/club_rust.pid

stop_pid_file() {
    PID_FILE=$1
    EXPECTED=$2
    if [ ! -f "$PID_FILE" ]; then
        return
    fi
    PID=""
    read -r PID < "$PID_FILE" || true
    case "$PID" in
        ''|*[!0-9]*) return ;;
    esac
    if kill -0 "$PID" 2>/dev/null; then
        PROCESS_COMMAND=$(ps -p "$PID" -o command= 2>/dev/null || true)
        case "$PROCESS_COMMAND" in
            *"$EXPECTED"*) ;;
            *) return ;;
        esac
        kill "$PID" 2>/dev/null || true
        for _ in $(seq 1 200); do
            kill -0 "$PID" 2>/dev/null || break
            sleep 0.1
        done
    fi
}

wait_port_free() {
    PORT_TO_WAIT=$1
    for _ in $(seq 1 200); do
        lsof -nP -iTCP:"$PORT_TO_WAIT" -sTCP:LISTEN >/dev/null 2>&1 || return 0
        sleep 0.1
    done
    return 1
}

stop_listener() {
    LISTEN_PORT=$1
    EXPECTED=$2
    lsof -nP -tiTCP:"$LISTEN_PORT" -sTCP:LISTEN 2>/dev/null | while read -r LISTENER_PID; do
        LISTENER_COMMAND=$(ps -p "$LISTENER_PID" -o command= 2>/dev/null || true)
        case "$LISTENER_COMMAND" in
            *"$EXPECTED"*) kill "$LISTENER_PID" 2>/dev/null || true ;;
        esac
    done
}

restore_ingress() {
    if [ "$HAD_INGRESS" -ne 1 ]; then
        echo "[nginx] 启动前没有旧入口可恢复"
        return 1
    fi
    if nginx -c "$NGINX_CONF" >/dev/null 2>&1; then
        echo "[nginx] 旧入口已恢复"
        return 0
    fi
    echo "[nginx] 入口恢复失败;请检查 /tmp/club_nginx_error.log"
    return 1
}

# 预检在摘流前完成。构建失败时保留旧站点，不会误用陈旧 Rust 二进制。
if ! command -v cargo >/dev/null 2>&1; then
    echo "[build] 未找到 cargo;旧站点保持不变"
    exit 1
fi
if ! cargo build --release --manifest-path "$DIR/club-hot/Cargo.toml" \
    >/tmp/club_build.log 2>&1; then
    echo "[build] 当前源码构建失败;旧站点保持不变 (log /tmp/club_build.log)"
    exit 1
fi

if ! redis-cli ping >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
        brew services start redis >/dev/null
    fi
fi
REDIS_READY=0
for _ in $(seq 1 100); do
    if redis-cli ping >/dev/null 2>&1; then
        REDIS_READY=1
        break
    fi
    sleep 0.1
done
if [ "$REDIS_READY" -ne 1 ]; then
    echo "[redis] 10 秒内未就绪;旧站点保持不变"
    exit 1
fi
echo "[redis] PONG"

sed "s#__APP_ROOT__#$DIR#g" "$DIR/nginx.conf" > "$NGINX_CONF"
if ! nginx -t -c "$NGINX_CONF" >/dev/null 2>&1; then
    echo "[nginx] 新配置校验失败;旧站点保持不变"
    exit 1
fi

# 停止入口接收新请求，并等待旧请求及跨服务 seat operation 排空。
HAD_INGRESS=0
if [ -f /tmp/club_nginx.pid ]; then
    HAD_INGRESS=1
fi
if [ -f /tmp/club_nginx.pid ]; then
    if ! nginx -s quit -c "$NGINX_CONF" 2>/dev/null; then
        echo "[nginx] 无法确认旧入口已摘流;旧后端保持不变"
        exit 1
    fi
    for _ in $(seq 1 600); do
        [ ! -f /tmp/club_nginx.pid ] && break
        sleep 0.1
    done
    if [ -f /tmp/club_nginx.pid ]; then
        nginx -s stop -c "$NGINX_CONF" 2>/dev/null || true
    fi
fi
if [ -f /tmp/club_nginx.pid ] || lsof -nP -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "[nginx] 旧入口仍在监听;旧后端保持不变"
    exit 1
fi

RESERVATION=""
ACTIVE_OP=""
for _ in $(seq 1 600); do
    if ! redis-cli ping >/dev/null 2>&1; then
        echo "[stop] 排空期间 Redis 不可查询;恢复入口并取消切换"
        restore_ingress || true
        exit 1
    fi
    if ! RESERVATION=$(redis-cli --scan --pattern 'resv:*' 2>/dev/null | sed -n '1p'); then
        echo "[stop] reservation 扫描失败;恢复入口并取消切换"
        restore_ingress || true
        exit 1
    fi
    if ! ACTIVE_OP=$(redis-cli --scan --pattern 'seat:op:*' 2>/dev/null | sed -n '1p'); then
        echo "[stop] operation 扫描失败;恢复入口并取消切换"
        restore_ingress || true
        exit 1
    fi
    [ -z "$RESERVATION" ] && [ -z "$ACTIVE_OP" ] && break
    sleep 0.1
done
if [ -n "$RESERVATION" ] || [ -n "$ACTIVE_OP" ]; then
    echo "[stop] 60 秒后仍有报名最终化任务;恢复入口并取消切换"
    restore_ingress || true
    exit 1
fi

# 停旧后端。优先精确 PID；兼容旧版 run.sh 没有 PID 文件的进程。
stop_pid_file "$PYTHON_PID_FILE" "main.py"
stop_pid_file "$RUST_PID_FILE" "$RUST_BIN"
stop_listener 2001 "main.py"
stop_listener 2002 "club-hot"
if ! wait_port_free 2001 || ! wait_port_free 2002; then
    echo "[stop] 旧后端端口未释放;尝试恢复入口"
    restore_ingress || true
    exit 1
fi

# Python 管理面，同时执行受 maintenance fence 保护的启动对账。
HOST=127.0.0.1 PORT=2001 python3 "$DIR/main.py" > /tmp/club_py.log 2>&1 &
PYTHON_PID=$!
echo "$PYTHON_PID" > "$PYTHON_PID_FILE"
PYTHON_READY=0
for _ in $(seq 1 100); do
    kill -0 "$PYTHON_PID" 2>/dev/null || break
    if curl -fsS http://127.0.0.1:2001/healthz >/dev/null 2>&1; then
        PYTHON_READY=1
        break
    fi
    sleep 0.1
done
if [ "$PYTHON_READY" -ne 1 ]; then
    kill "$PYTHON_PID" 2>/dev/null || true
    echo "[python] 未通过 health check,站点保持停止以避免接入坏实例 (log /tmp/club_py.log)"
    exit 1
fi
echo "[python] 管理面已就绪 http://127.0.0.1:2001"

# Rust 热路径。只有新 PID 存活且 readiness 通过才宣布成功。
BIND=127.0.0.1:2002 DB_PATH="$DIR/club_system.db" REDIS_URL="redis://127.0.0.1:6379" \
    "$RUST_BIN" > /tmp/club_rust.log 2>&1 &
RUST_PID=$!
echo "$RUST_PID" > "$RUST_PID_FILE"
RUST_READY=0
for _ in $(seq 1 100); do
    kill -0 "$RUST_PID" 2>/dev/null || break
    if curl -fsS http://127.0.0.1:2002/readyz >/dev/null 2>&1; then
        RUST_READY=1
        break
    fi
    sleep 0.1
done
if [ "$RUST_READY" -eq 1 ] && kill -0 "$RUST_PID" 2>/dev/null; then
    echo "[rust] 热服务已就绪 http://127.0.0.1:2002"
else
    kill "$RUST_PID" 2>/dev/null || true
    wait_port_free 2002 || true
    echo "[rust] 未通过 readiness;Nginx 将使用低容量 Python backup (log /tmp/club_rust.log)"
fi

if nginx -c "$NGINX_CONF"; then
    echo "[nginx] 边缘已就绪 http://127.0.0.1:8080"
else
    echo "[nginx] 启动失败;可直接访问 Python :2001"
    exit 1
fi

echo "停止:nginx -s quit -c $NGINX_CONF; kill $PYTHON_PID $RUST_PID"
