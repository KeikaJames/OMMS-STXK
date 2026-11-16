#!/usr/bin/env bash
# 一键编排：Redis + Python(:2001) + Rust(:2002) + nginx(:8080)，macOS 与 Linux 通用。
#   bash run.sh                      启动；已在运行时平滑重启
#   bash run.sh stop                 停止
#   OMMS_PYTHON_ONLY=1 bash run.sh   不编译 Rust，由 Python 服务全部接口
set -o pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_DIR="${OMMS_RUN_DIR:-$DIR/run}"
PYTHON="${PYTHON:-python3}"
PYTHON_ONLY="${OMMS_PYTHON_ONLY:-0}"
export DB_PATH="${DB_PATH:-$DIR/club_system.db}"
export REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
export REDIS_PREFIX="${REDIS_PREFIX:-omms:}"
export APP_UTC_OFFSET="${APP_UTC_OFFSET:-+08:00}"
export NGINX_ACCESS_LOG="$RUN_DIR/nginx-access.log"
NGINX_CONF="$RUN_DIR/nginx.conf"
RUST_BIN="$DIR/club-hot/target/release/club-hot"

redis_address="${REDIS_URL#redis://}"
redis_address="${redis_address%%/*}"
redis_address="${redis_address##*@}"
REDIS_HOST="${redis_address%:*}"
REDIS_PORT="${redis_address##*:}"
[ "$REDIS_HOST" = "$redis_address" ] && REDIS_PORT=6379

mkdir -p "$RUN_DIR"

say() { printf '%s\n' "$*"; }
rcli() { redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" "$@"; }
redis_up() { rcli ping >/dev/null 2>&1; }
port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
http_ok() { curl -fsS -o /dev/null --max-time 2 "$1" 2>/dev/null; }

wait_until() {
    # wait_until 秒数 命令...：在限定时间内等待命令成功
    local limit=$1
    shift
    local deadline=$((SECONDS + limit))
    while [ "$SECONDS" -lt "$deadline" ]; do
        "$@" && return 0
        sleep 0.2
    done
    return 1
}

stop_pid() {
    local pid_file=$1 expected=$2 pid=""
    [ -f "$pid_file" ] || return 0
    read -r pid < "$pid_file" || true
    case "$pid" in ''|*[!0-9]*) rm -f "$pid_file"; return 0 ;; esac
    if kill -0 "$pid" 2>/dev/null && ps -p "$pid" -o command= 2>/dev/null | grep -q "$expected"; then
        kill "$pid" 2>/dev/null || true
        wait_until 20 bash -c "! kill -0 $pid 2>/dev/null" || kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$pid_file"
}

nginx_running() {
    local pid=""
    [ -f "$RUN_DIR/nginx.pid" ] && read -r pid < "$RUN_DIR/nginx.pid" && kill -0 "$pid" 2>/dev/null
}

no_operations() {
    [ -z "$(rcli --scan --pattern "${REDIS_PREFIX}op:*" 2>/dev/null | head -n 1)" ]
}

stop_all() {
    if nginx_running; then
        nginx -p "$RUN_DIR" -c "$NGINX_CONF" -s quit 2>/dev/null || true
        wait_until 30 bash -c "[ ! -f '$RUN_DIR/nginx.pid' ]" || true
    fi
    if rcli ping >/dev/null 2>&1; then
        wait_until 60 no_operations || say "[stop] 仍有报名操作未完成，继续停止"
    fi
    stop_pid "$RUN_DIR/python.pid" "main.py"
    stop_pid "$RUN_DIR/rust.pid" "club-hot"
}

if [ "${1:-}" = "stop" ]; then
    stop_all
    say "[stop] 已停止"
    exit 0
fi

# ---- 预检：在旧服务仍在线时完成，失败不影响旧服务 ----
if [ "$PYTHON_ONLY" != "1" ]; then
    if ! command -v cargo >/dev/null 2>&1; then
        say "[build] 未找到 cargo，改用只运行 Python 的模式（OMMS_PYTHON_ONLY=1）"
        PYTHON_ONLY=1
    elif ! cargo build --release --manifest-path "$DIR/club-hot/Cargo.toml" > "$RUN_DIR/build.log" 2>&1; then
        say "[build] Rust 构建失败，旧服务保持不变（日志 $RUN_DIR/build.log）"
        exit 1
    fi
fi

if ! rcli ping >/dev/null 2>&1; then
    if ! command -v redis-server >/dev/null 2>&1; then
        say "[redis] 未找到 redis-server，请先安装 Redis 3.2 或更高版本"
        exit 1
    fi
    redis-server "$DIR/deploy/redis.conf" --port "$REDIS_PORT" --dir "$RUN_DIR" \
        --daemonize yes --pidfile "$RUN_DIR/redis.pid" --logfile "$RUN_DIR/redis.log"
    if ! wait_until 10 redis_up; then
        say "[redis] 10 秒内未就绪（日志 $RUN_DIR/redis.log）"
        exit 1
    fi
fi
say "[redis] $REDIS_HOST:$REDIS_PORT 就绪"

sed -e "s#__APP_ROOT__#$DIR#g" -e "s#__RUN_DIR__#$RUN_DIR#g" "$DIR/nginx.conf" > "$NGINX_CONF.new"
if [ "$PYTHON_ONLY" = "1" ]; then
    sed -i.bak -e '/server 127.0.0.1:2002 /d' -e 's/server 127.0.0.1:2001 backup;/server 127.0.0.1:2001;/' "$NGINX_CONF.new"
    rm -f "$NGINX_CONF.new.bak"
fi
if ! nginx -t -p "$RUN_DIR" -c "$NGINX_CONF.new" >/dev/null 2>&1; then
    nginx -t -p "$RUN_DIR" -c "$NGINX_CONF.new"
    say "[nginx] 新配置校验失败，旧服务保持不变"
    exit 1
fi

# ---- 切换：摘流、等在途报名完成、重启后端 ----
stop_all
mv "$NGINX_CONF.new" "$NGINX_CONF"

HOST=127.0.0.1 PORT=2001 nohup "$PYTHON" "$DIR/main.py" > "$RUN_DIR/python.log" 2>&1 &
echo $! > "$RUN_DIR/python.pid"
if ! wait_until 30 http_ok "http://127.0.0.1:2001/healthz"; then
    say "[python] 未通过健康检查（日志 $RUN_DIR/python.log）"
    exit 1
fi
say "[python] http://127.0.0.1:2001 就绪"

if [ "$PYTHON_ONLY" != "1" ]; then
    BIND=127.0.0.1:2002 nohup "$RUST_BIN" > "$RUN_DIR/rust.log" 2>&1 &
    echo $! > "$RUN_DIR/rust.pid"
    if wait_until 30 http_ok "http://127.0.0.1:2002/readyz"; then
        say "[rust] http://127.0.0.1:2002 就绪"
    else
        stop_pid "$RUN_DIR/rust.pid" "club-hot"
        say "[rust] 未就绪，热接口由 Python 承接（日志 $RUN_DIR/rust.log）"
    fi
fi

if nginx -p "$RUN_DIR" -c "$NGINX_CONF"; then
    say "[nginx] http://127.0.0.1:8080 就绪"
else
    say "[nginx] 启动失败，可直接访问 http://127.0.0.1:2001"
    exit 1
fi
say "停止：bash run.sh stop"
