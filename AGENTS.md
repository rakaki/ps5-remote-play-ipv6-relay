# AGENTS.md — 给 AI 助手的执行手册

本仓库用于在一台**双栈 Linux 主机**上部署 PS5 远程串流的 IPv6→IPv4 端口中继。
按下面步骤执行即可；遇到与预期不符时，先看「排障决策树」再动手改代码。

## 目标

让外网客户端（PeaSyo / Chiaki / chiaki-ng）通过家里的公网 IPv6 直连内网 IPv4 的 PS5。

## 不可违反的约束

1. **不要试图用 nftables/iptables 做 IPv6→IPv4 DNAT**：Linux 不支持跨地址族 NAT，一定失败。
2. **不要用 socat 代替本程序**：`-T` 会杀掉监听进程，且无法指定 UDP 回包源地址。
3. **UDP 回包必须带 `IPV6_PKTINFO`**，源地址必须等于客户端连接的那个本机地址，否则客户端会静默丢弃回包。
4. 修改正在串流使用的服务前提醒用户；不要在有活动会话时随意重启。
5. 任何"把端口对全网开放"的操作都要向用户说明风险。

## 平台判断（先做这一步）

| 目标主机 | 做法 |
|---|---|
| Linux（推荐） | 按本手册主流程 |
| Windows 11 22H2+ | 先配 WSL2 mirrored 网络（`docs/windows.md` 方案 A），再按主流程 |
| Windows 其他版本 | `windows/ps5-relay-win.py`（实验性，要求本机只有一个公网 IPv6） |
| macOS | 未验证：pktinfo 常量与 Linux 不同，不要直接套用 |

禁止用 `netsh interface portproxy`（只支持 TCP）、禁止用 nginx for Windows（需写死监听地址）。

## 执行步骤

### 0. 前置检查（必须全部满足）

```bash
ip -6 addr show scope global | grep inet6          # 有 2xxx/240e 开头的公网地址
ip -4 addr show | grep inet                        # 有内网 IPv4
ping -c 2 ${PS5_IP:-192.168.0.14}                  # PS5 可达
timeout 2 bash -c 'echo > /dev/tcp/${PS5_IP:-192.168.0.14}/9295' && echo "PS5 remote play OK"
ps -p 1 -o comm=                                    # 应为 systemd
```

若 9295 被拒绝：让用户在 PS5 上开启「设置 → 系统 → 远程游玩 → 启用远程游玩」，
并开启待机模式下的「保持与互联网连接」「允许从网络开启 PS5」。

### 1. 安装

```bash
sudo ./scripts/install.sh <PS5内网IP>
```

幂等：重复执行只更新程序与 unit 并重启服务。

### 2. 自检

```bash
./scripts/verify.sh <本机公网IPv6>
```

### 3. 让用户实测

客户端填本机公网 IPv6（任一地址即可），无需改端口。
在用户实测期间保持：

```bash
journalctl -u ps5-relay -f
```

## 验收标准

| 观察点 | 合格 |
|---|---|
| `ss -lntu` | TCP/UDP 9295-9304 + UDP 987 均在监听 |
| 日志 `UDP :9296` | `c->ps5` 与 `ps5->c` **双向**增长，`ps5->c` 字节数明显大于 `c->ps5` |
| 视频包大小 | `max` 约 1200-1300 B（远小于链路 MTU，正常） |
| 客户端 | 能出画面，不再转圈超时 |

## 排障决策树

```
客户端连不上
├─ 日志里没有任何 TCP 记录
│   ├─ 外部探测 9295 无响应 → 路由器 IPv6 防火墙拦入站，让用户放行该主机
│   └─ 有响应但客户端仍不连 → 客户端填的地址/端口错了
└─ 日志里有 TCP，但客户端十几秒后超时
    ├─ 日志有 "TCP connect ... refused" → PS5 没开远程游玩
    └─ 有 UDP 会话，但 ps5->c 极少或为 0
        ├─ 只有 1 个回包后 PS5 沉默 → 典型"回包源地址不一致"，检查程序是否启用 IPV6_RECVPKTINFO
        ├─ c->ps5 有量、ps5->c 完全为 0 → PS5 侧未识别会话，确认出站绑定端口策略
        └─ 双向都有量但客户端仍黑屏 → 看 p2c_max 是否远超 1400（MTU 黑洞），或带宽不足
```

### 判断"源地址"问题的最快方法

在本机抓包，看同一时刻两个方向的地址：

```bash
sudo tcpdump -i any -nn -s0 -c 20 'udp portrange 9295-9304'
```

若「客户端 → 本机」的目的地址 与「本机 → 客户端」的源地址**不是同一个 IPv6 字面量**，即为该问题。

## 常用运维

```bash
systemctl status ps5-relay
systemctl restart ps5-relay     # 会断开正在进行的串流
journalctl -u ps5-relay -n 100 --no-pager
```

配置项全部通过 systemd `Environment=` 传入：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PS5_IP` | 192.168.0.14 | PS5 内网地址 |
| `LOCAL_IP` | 自动探测 | 出站绑定用的本机内网地址 |
| `TCP_PORTS` | 9295-9304 | 逗号分隔 |
| `UDP_PORTS` | 987,9295-9304 | 逗号分隔 |
| `UDP_IDLE` | 300 | UDP 会话空闲回收秒数 |