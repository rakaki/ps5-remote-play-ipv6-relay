#!/usr/bin/env bash
# 部署后自检: ./verify.sh [本机公网IPv6]
set -uo pipefail

PS5_IP="${PS5_IP:-192.168.0.14}"
PUB6="${1:-}"

fail=0
ok()   { printf '  [OK]   %s\n' "$1"; }
bad()  { printf '  [FAIL] %s\n' "$1"; fail=1; }
info() { printf '  [info] %s\n' "$1"; }

echo "== 服务 =="
systemctl is-active --quiet ps5-relay && ok "ps5-relay 运行中" || bad "ps5-relay 未运行"

echo "== 监听 =="
missing=""
for p in 9295 9296 9297 9302; do
    ss -lntu | grep -q ":$p " || missing="$missing $p"
done
ss -lntu | grep -q ":987 " || missing="$missing 987(UDP)"
[ -z "$missing" ] && ok "端口齐全" || bad "缺少:$missing"

echo "== PS5 =="
ping -c 2 -W 1 "$PS5_IP" >/dev/null 2>&1 && ok "ping 通 $PS5_IP" || bad "ping 不通 $PS5_IP"
if timeout 2 bash -c "echo > /dev/tcp/$PS5_IP/9295" 2>/dev/null; then
    ok "PS5 9295 可连接（远程游玩已启用）"
else
    bad "PS5 9295 拒连：PS5 上未启用远程游玩"
fi

echo "== 对外地址 =="
if [ -n "$PUB6" ]; then
    info "客户端请填: $PUB6"
    info "外部可达性可用第三方 IPv6 扫描器验证（需从该主机发起扫描，扫的是请求方的地址）"
else
    info "本机公网 IPv6: $(ip -6 addr show scope global | awk '/inet6 2/{print $2}' | cut -d/ -f1 | tr '\n' ' ')"
fi

echo "== 说明 =="
info "串流时看 journalctl -u ps5-relay -f，应看到 UDP :9296 的 c->ps5 与 ps5->c 双向增长"
exit $fail
