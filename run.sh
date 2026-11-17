#!/usr/bin/env bash
# 一键编排：Redis + Python(:2001) + Rust(:2002) + nginx(:8080)，macOS 与 Linux 通用。
#   bash run.sh                      启动（已在运行时平滑重启），打印访问地址与管理员账号，在本机打开控制台
#   bash run.sh stop                 停止
#   OMMS_PYTHON_ONLY=1 bash run.sh   不编译 Rust，由 Python 服务全部接口
#   TLS_CERT=证书 TLS_KEY=私钥 bash run.sh      在 TLS_PORT（默认 8443）上提供 HTTPS，8080 跳转到 HTTPS
#   REAL_IP_FROM=127.0.0.1 bash run.sh         经内网穿透或反向代理访问时，按 REAL_IP_HEADER 取学生的真实地址
#   HTTP_PORT=80 bash run.sh         改用 80 端口，学生访问地址只写 IP
# 上述参数也可以写在 deploy/omms.env（每行 键=值）；启动命令里给出的同名变量优先。
# cargo 可用且编译成功时同时运行 Rust 热服务，其余情况只运行 Python 服务。
set -o pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
# deploy/omms.env 只读取下列运行参数，其余键留给 systemd 部署使用。
SETTINGS_FILE="${OMMS_SETTINGS:-$DIR/deploy/omms.env}"
if [ -r "$SETTINGS_FILE" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        line=${line%$'\r'}
        key=${line%%=*}
        value=${line#*=}
        [ "$key" = "$line" ] && continue
        case "$key" in
            HTTP_PORT|TLS_CERT|TLS_KEY|TLS_PORT|REAL_IP_FROM|REAL_IP_HEADER|APP_UTC_OFFSET|OMMS_PYTHON_ONLY|\
            OMMS_OPEN_BROWSER|PYTHON|BACKUP_KEEP|MAX_IMPORT_STUDENTS|SESSION_TTL|LOGIN_MAX_FAILS|AUTH_CONCURRENCY|\
            MAX_HTTP_WORKERS|SEATS_CACHE_MS|LOG_LEVEL)
                printenv "$key" >/dev/null 2>&1 || export "$key=$value"
                ;;
        esac
    done < "$SETTINGS_FILE"
fi
RUN_DIR="${OMMS_RUN_DIR:-$DIR/run}"
HTTP_PORT="${HTTP_PORT:-8080}"
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
memory_mb() {
    if [ -r /proc/meminfo ]; then
        awk '/^MemTotal:/ { print int($2 / 1024) }' /proc/meminfo
    else
        sysctl -n hw.memsize 2>/dev/null | awk '{ print int($1 / 1048576) }'
    fi
}
version_at_least() {
    # version_at_least 当前版本 最低版本
    [ "$(printf '%s\n%s\n' "$1" "$2" | sort -t. -k1,1n -k2,2n -k3,3n | head -n 1)" = "$2" ]
}
rcli() { redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" "$@"; }
redis_up() { rcli ping >/dev/null 2>&1; }
port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
http_ok() { curl -fsS -o /dev/null --max-time 2 "$1" 2>/dev/null; }

# 每个学生连接在 nginx 上占 1 至 2 个文件句柄；macOS 终端默认上限只有 256。
raise_open_files() {
    local hard want=65536
    hard=$(ulimit -Hn)
    if [ "$hard" != unlimited ] && [ "$hard" -lt "$want" ]; then
        want=$hard
    fi
    ulimit -n "$want" 2>/dev/null || ulimit -n 10240 2>/dev/null || true
}

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
raise_open_files
case "$HTTP_PORT" in
    ''|*[!0-9]*) say "[nginx] HTTP_PORT 应为 1–65535 的端口号"; exit 1 ;;
esac
if [ "$HTTP_PORT" -lt 1 ] || [ "$HTTP_PORT" -gt 65535 ]; then
    say "[nginx] HTTP_PORT 应为 1–65535 的端口号"
    exit 1
fi
if [ -n "${TLS_CERT:-}${TLS_KEY:-}" ]; then
    if [ ! -r "${TLS_CERT:-}" ] || [ ! -r "${TLS_KEY:-}" ]; then
        say "[nginx] TLS_CERT 与 TLS_KEY 需指向可读的证书与私钥文件"
        exit 1
    fi
    export COOKIE_SECURE=1
fi
if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 7))' 2>/dev/null; then
    say "[python] 需要 Python 3.7 或更高版本（当前：$("$PYTHON" -V 2>&1)），可用 PYTHON=python3.8 指定"
    exit 1
fi
if ! "$PYTHON" -c 'import redis, argon2' 2>/dev/null; then
    say "[python] 缺少依赖，请先运行：$PYTHON -m pip install redis argon2-cffi pypinyin Pillow"
    exit 1
fi

if [ "$PYTHON_ONLY" != "1" ]; then
    if ! command -v cargo >/dev/null 2>&1; then
        say "[build] 未找到 cargo，改用只运行 Python 的模式（OMMS_PYTHON_ONLY=1）"
        PYTHON_ONLY=1
    else
        # 每个 rustc 进程峰值约 700 MB，按内存决定并行数，内存小的机器不至于换页卡死。
        build_jobs=$(( $(memory_mb || echo 1024) / 1024 ))
        cores=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)
        [ "$build_jobs" -gt "$cores" ] && build_jobs=$cores
        [ "$build_jobs" -lt 1 ] && build_jobs=1
        say "[build] 编译 Rust 热服务（并行 ${build_jobs}；首次编译在旧电脑上需要几分钟）"
        if ! cargo build --release --jobs "$build_jobs" --manifest-path "$DIR/club-hot/Cargo.toml" > "$RUN_DIR/build.log" 2>&1; then
            if [ -x "$RUST_BIN" ]; then
                say "[build] Rust 构建失败，旧服务保持不变（日志 $RUN_DIR/build.log）"
                exit 1
            fi
            say "[build] Rust 构建失败，改用只运行 Python 的模式（日志 $RUN_DIR/build.log）"
            PYTHON_ONLY=1
        fi
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

if ! command -v nginx >/dev/null 2>&1; then
    say "[nginx] 未找到 nginx，请先安装（Debian/Ubuntu：apt install nginx；CentOS：yum install nginx）"
    exit 1
fi
sed -e "s#__APP_ROOT__#$DIR#g" -e "s#__RUN_DIR__#$RUN_DIR#g" "$DIR/nginx.conf" > "$NGINX_CONF.new"
nginx_version=$(nginx -v 2>&1 | sed -n 's#^.*nginx/\([0-9][0-9.]*\).*$#\1#p')
if ! version_at_least "${nginx_version:-0}" 1.17.6; then
    # 1.17.6 之前没有 $limit_req_status、$limit_conn_status 变量。
    # shellcheck disable=SC2016
    sed -i.bak -e 's/ limit_req=\$limit_req_status limit_conn=\$limit_conn_status//' "$NGINX_CONF.new"
fi
if [ "$PYTHON_ONLY" = "1" ]; then
    # Python 每个连接占一个线程，不保留空闲的上游长连接。
    sed -i.bak -e '/server 127.0.0.1:2002 /d' -e 's/server 127.0.0.1:2001 backup;/server 127.0.0.1:2001;/' \
        -e '/^ *keepalive 64;$/d' "$NGINX_CONF.new"
fi
rm -f "$NGINX_CONF.new.bak"
if ! "$PYTHON" - "$NGINX_CONF.new" <<'PY'
import os
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as source:
    text = source.read()
sources = [item.strip() for item in os.environ.get("REAL_IP_FROM", "").split(",") if item.strip()]
if sources:
    lines = "".join("    set_real_ip_from {};\n".format(item) for item in sources)
    lines += "    real_ip_header {};\n    real_ip_recursive on;\n".format(os.environ.get("REAL_IP_HEADER") or "X-Forwarded-For")
    anchor = "    server_tokens off;\n"
    if anchor not in text:
        sys.exit("server_tokens 行缺失")
    text = text.replace(anchor, anchor + lines, 1)
cert, key = os.environ.get("TLS_CERT"), os.environ.get("TLS_KEY")
if cert and key:
    port = os.environ.get("TLS_PORT") or "8443"
    anchor = "    server {\n        listen 8080 backlog=2048;\n"
    if anchor not in text:
        sys.exit("listen 8080 行缺失")
    text = text.replace(anchor, (
        "    server {\n        listen 8080;\n        server_name _;\n"
        "        return 301 https://$host:" + port + "$request_uri;\n    }\n\n"
        "    server {\n        listen " + port + " ssl backlog=2048;\n"
        "        ssl_certificate     " + cert + ";\n        ssl_certificate_key " + key + ";\n"
        "        ssl_protocols TLSv1.2 TLSv1.3;\n"
        "        add_header Strict-Transport-Security \"max-age=31536000\" always;\n"), 1)
with open(path, "w", encoding="utf-8") as target:
    target.write(text)
PY
then
    say "[nginx] 生成 HTTPS 或来源地址配置失败，旧服务保持不变"
    exit 1
fi
if [ "$HTTP_PORT" != 8080 ]; then
    sed -i.bak -e "s/listen 8080/listen $HTTP_PORT/" "$NGINX_CONF.new"
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
# 上一次的服务日志留一份（首次启动的管理员密码在里面）；访问日志超过 64 MB 时换新文件。
for log_file in "$RUN_DIR/python.log" "$RUN_DIR/rust.log"; do
    if [ -s "$log_file" ]; then
        mv -f "$log_file" "$log_file.1"
    fi
done
if [ -f "$NGINX_ACCESS_LOG" ] && [ "$(wc -c < "$NGINX_ACCESS_LOG")" -gt 67108864 ]; then
    mv -f "$NGINX_ACCESS_LOG" "$NGINX_ACCESS_LOG.1"
fi

HOST=127.0.0.1 PORT=2001 nohup "$PYTHON" "$DIR/main.py" > "$RUN_DIR/python.log" 2>&1 &
echo $! > "$RUN_DIR/python.pid"
if ! wait_until 30 http_ok "http://127.0.0.1:2001/healthz"; then
    say "[python] 未通过健康检查（日志 $RUN_DIR/python.log）"
    exit 1
fi
say "[python] http://127.0.0.1:2001 就绪"
if command -v caffeinate >/dev/null 2>&1; then
    # macOS：服务运行期间阻止系统闲置睡眠，接通电源时同时阻止系统睡眠；Python 服务退出后自动解除。
    nohup caffeinate -i -s -w "$(cat "$RUN_DIR/python.pid")" >/dev/null 2>&1 &
fi

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

if ! nginx -p "$RUN_DIR" -c "$NGINX_CONF"; then
    say "[nginx] 启动失败，可直接访问 http://127.0.0.1:2001"
    exit 1
fi

# ---- 访问地址与管理员账号 ----
scheme=http
port=$HTTP_PORT
if [ -n "${TLS_CERT:-}" ]; then
    scheme=https
    port=${TLS_PORT:-8443}
fi
case "$scheme:$port" in
    http:80|https:443) port_suffix="" ;;
    *) port_suffix=":$port" ;;
esac
lan_ip=""
case "$(uname -s)" in
    Darwin)
        iface=$(route -n get default 2>/dev/null | awk '/interface:/ {print $2}')
        [ -n "$iface" ] && lan_ip=$(ipconfig getifaddr "$iface" 2>/dev/null)
        ;;
    Linux)
        lan_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
        ;;
esac
local_base="$scheme://127.0.0.1$port_suffix"
public_base="$scheme://${lan_ip:-127.0.0.1}$port_suffix"
admin_user=""
admin_password=""
login_url=""
while IFS='=' read -r key value; do
    case "$key" in
        ADMIN_USER) admin_user=$value ;;
        ADMIN_PASSWORD) admin_password=$value ;;
        LOGIN_URL) login_url=$value ;;
    esac
done < <("$PYTHON" "$DIR/main.py" admin-login "$local_base" 2>/dev/null)
say ""
say "学生访问     $public_base"
say "管理控制台   $public_base/admin/dashboard"
if [ -n "$admin_password" ]; then
    say "管理员账号   ${admin_user:-admin}"
    say "管理员密码   $admin_password"
fi
if [ "${OMMS_OPEN_BROWSER:-1}" = "1" ]; then
    target=${login_url:-$local_base/}
    if [ "$(uname -s)" = Darwin ] && command -v open >/dev/null 2>&1; then
        open "$target" >/dev/null 2>&1 || true
    elif command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$target" >/dev/null 2>&1 || true
    fi
fi
