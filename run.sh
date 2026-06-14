#!/bin/bash
# 本地编排:redis + Python 管理面(:2001)+ Rust 热服务(:2002,若已构建)+ nginx 边缘(:8080)
# 用法:bash run.sh   访问 http://127.0.0.1:8080
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1

# 1) redis
redis-cli ping >/dev/null 2>&1 || brew services start redis
echo "[redis] $(redis-cli ping 2>/dev/null || echo down)"

# 2) Python 管理面 :2001
pkill -f "python3 main.py" 2>/dev/null
HOST=127.0.0.1 PORT=2001 python3 main.py > /tmp/club_py.log 2>&1 &
echo "[python] 管理面 http://127.0.0.1:2001  (log /tmp/club_py.log)"

# 3) Rust 热服务 :2002(若已构建,否则热路径由 nginx 回落到 Python backup)
RUST_BIN="club-hot/target/release/club-hot"
if [ -x "$RUST_BIN" ]; then
    pkill -f "$RUST_BIN" 2>/dev/null
    BIND=127.0.0.1:2002 DB_PATH="$DIR/club_system.db" REDIS_URL="redis://127.0.0.1:6379" \
        "$RUST_BIN" > /tmp/club_rust.log 2>&1 &
    echo "[rust] 热服务 http://127.0.0.1:2002  (log /tmp/club_rust.log)"
else
    echo "[rust] 未构建($RUST_BIN 不存在);热路径将由 nginx 回落 Python backup"
fi

# 4) nginx 边缘 :8080(把仓库路径注入临时配置,免去手改 nginx.conf)
NGINX_CONF=/tmp/club_nginx.conf
sed "s#__APP_ROOT__#$DIR#g" "$DIR/nginx.conf" > "$NGINX_CONF"
if nginx -t -c "$NGINX_CONF" 2>/dev/null; then
    nginx -s stop 2>/dev/null || pkill nginx 2>/dev/null
    nginx -c "$NGINX_CONF"
    echo "[nginx] 边缘 http://127.0.0.1:8080"
else
    echo "[nginx] 配置校验失败,跳过(直接用 :2001)"
fi

echo "停止:pkill -f 'python3 main.py'; pkill -f club-hot; nginx -s stop"
