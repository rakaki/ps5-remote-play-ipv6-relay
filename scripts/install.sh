#!/usr/bin/env bash
# 幂等安装: ./install.sh <PS5内网IP> [本机内网IP]
set -euo pipefail

PS5_IP="${1:-}"
LOCAL_IP="${2:-}"

if [ -z "$PS5_IP" ]; then
    echo "用法: $0 <PS5内网IP> [本机内网IP]" >&2
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "需要 root 权限" >&2
    exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v python3 >/dev/null || { echo "缺少 python3" >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || {
    echo "需要 Python 3.8+" >&2; exit 1; }

install -m 755 "$SRC_DIR/relay/ps5-relay.py" /usr/local/bin/ps5-relay.py
python3 -m py_compile /usr/local/bin/ps5-relay.py

TMP_UNIT="$(mktemp)"
sed -e "s|^Environment=PS5_IP=.*|Environment=PS5_IP=$PS5_IP|" "$SRC_DIR/systemd/ps5-relay.service" > "$TMP_UNIT"
if [ -n "$LOCAL_IP" ]; then
    sed -i "2i Environment=LOCAL_IP=$LOCAL_IP" "$TMP_UNIT"
fi
install -m 644 "$TMP_UNIT" /etc/systemd/system/ps5-relay.service
rm -f "$TMP_UNIT"

systemctl daemon-reload
systemctl enable ps5-relay >/dev/null 2>&1 || true
systemctl restart ps5-relay
sleep 1

echo "--- 状态 ---"
systemctl is-active ps5-relay
echo "--- 监听 ---"
ss -lntu | grep -E ':(987|929[5-9]|930[0-4])\b' | sort -k5 || echo "未发现监听，请查看 journalctl -u ps5-relay -n 50"
echo "--- 提示 ---"
echo "客户端填本机公网 IPv6: $(ip -6 addr show scope global | awk '/inet6 2/{print $2}' | cut -d/ -f1 | head -1)"
