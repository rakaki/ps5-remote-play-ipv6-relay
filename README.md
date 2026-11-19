# PS5 远程串流 · IPv6 → IPv4 中继

家里只有公网 IPv6、PS5 只有 IPv4 时，用一台双栈 Linux 主机把 PS5 的串流端口从公网 IPv6 转发到内网 IPv4，让手机/掌机可以在外网直连自己的 PS5（PeaSyo / Chiaki / chiaki-ng 等客户端）。

```
手机(IPv6)  ──公网 IPv6──▶  中继主机(双栈)  ──内网 IPv4──▶  PS5 192.168.0.14
                             TCP/UDP 9295-9304 + UDP 987
```

## 为什么必须中继

PS5 本身不提供任何可直连的 IPv6 入口，实测结论：

| 测试 | 结果 | 含义 |
|---|---|---|
| `ping6` PS5 的链路本地地址 | 有回应 | IPv6 协议栈存在 |
| `ping6` 按 MAC 推导的全局地址 | 无回应，邻居表 FAILED | 没有全局 IPv6 地址 |
| `TCP [fe80::…]:9295` | Connection refused（RST） | Remote Play 服务不监听 IPv6 |
| `TCP 192.168.0.14:9295` | 连接成功 | 服务只监听 IPv4 |

而且 Linux 的 nftables/iptables **不支持跨地址族 DNAT**（IPv6 → IPv4），所以不能靠一条 NAT 规则解决，必须用用户态中继程序在中间搬数据。

## 端口

| 端口 | 协议 | 用途 |
|---|---|---|
| 9295 | TCP | 注册与会话控制 |
| 9296 | UDP | 音视频流 + 带宽探测（Senkusha） |
| 9297 | UDP | 反馈 / 拥塞控制 |
| 9302 | UDP | PS5 发现 |
| 987 | UDP | 唤醒（WoL） |
| 9295-9304 | TCP+UDP | 兜底整段（不确定时全开） |

## 前置条件

1. 一台**双栈** Linux（本机直连公网 IPv6，入站未被路由器拦截）
2. PS5 已开启：设置 → 系统 → 远程游玩 → 启用远程游玩
3. PS5 建议：设置 → 系统 → 省电 → 待机模式中的功能 → 开启「保持与互联网连接」「允许从网络开启 PS5」
4. PS5 使用固定内网 IP（路由器里按 MAC 做 DHCP 绑定）
5. 客户端支持手动填写主机地址（PeaSyo、Chiaki、chiaki-ng 可以；官方 PS Remote Play App 不支持自定义 IPv6 地址）

## 部署

```bash
sudo ./scripts/install.sh 192.168.0.14
```

脚本会：把 `relay/ps5-relay.py` 装到 `/usr/local/bin`、写入 `/etc/systemd/system/ps5-relay.service`、`enable + restart`，然后打印监听状态。

手动部署等价于：

```bash
sudo install -m 755 relay/ps5-relay.py /usr/local/bin/ps5-relay.py
sudo install -m 644 systemd/ps5-relay.service /etc/systemd/system/ps5-relay.service
# 按需修改 unit 里的 Environment=PS5_IP=...
sudo systemctl daemon-reload
sudo systemctl enable --now ps5-relay
```

## 客户端怎么填

填**本机的一个公网 IPv6 地址**，端口不用改（协议本身固定用 9295/9296/9297）。

查看本机地址：

```bash
ip -6 addr show scope global | grep inet6
```

多个地址都能用（见下文「源地址」）。前缀由运营商下发，PPPoE 重连后可能变化，长期使用建议配 DDNS。

## 验证

```bash
./scripts/verify.sh 240e:xxxx::1
```

或手动：

| 检查 | 命令 | 期望 |
|---|---|---|
| 端口在听 | `ss -lntu \| grep -E ':(987\|929[5-9]\|930[0-4])'` | TCP/UDP 均有监听 |
| PS5 可达 | `ping -c2 192.168.0.14` | 通 |
| PS5 服务在跑 | `timeout 2 bash -c 'echo > /dev/tcp/192.168.0.14/9295'` | 不报 refused |
| 转发日志 | `journalctl -u ps5-relay -f` | 串流时 `c->ps5` 与 `ps5->c` 双向增长 |

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 客户端转圈十几秒后失败、无错误码 | UDP 回包源地址与客户端连接的目标地址不一致，被客户端内核丢弃 | 见下文「源地址」，确认程序用了 `IPV6_PKTINFO` |
| 日志里 PS5 只回 1 个包后完全沉默 | 同上（包到了但客户端当噪音丢了，于是不停重发） | 同上 |
| 日志有 `TCP connect ... failed: refused` | PS5 没开远程游玩 / 不在待机联网状态 | 到 PS5 上开启 |
| 能连但黑屏 | 上行带宽不足 / 视频包被路径 MTU 黑洞 | 看日志 `max` 字节数，正常应 ≤1252 |
| 外网完全连不上 | 路由器 IPv6 防火墙拦截入站 | 在路由器放行该主机，或关闭 IPv6 防火墙 |
| 换网络后连不上 | 公网 IPv6 前缀变了 | 更新客户端地址，或上 DDNS |

## 设计要点（踩过的坑）

### 1. 回包必须指定源地址

本机通常有多个公网 IPv6（EUI-64 + stable-privacy）。UDP 是无连接的，内核默认用「首选源地址」回包，于是：

```
客户端 →  240e:...:7e83:34ff:febe:b5d3:9296     （它连的是这个地址）
本机   ←  240e:...:f567:2190:5d8c:17a0:9296     （回包却用另一个）
```

客户端如果用连接型 UDP socket（`connect` + `recv`），内核只交付「源地址 = 连接目的地址」的包，其余全部丢弃。现象就是 PS5 明明回了包，客户端却像没收到。

因此程序对监听 socket 打开 `IPV6_RECVPKTINFO`，记录每个包发往本机哪个地址，回包时用 `sendmsg` + `IPV6_PKTINFO` 强制以该地址为源地址。TCP 没有这个问题（连接自带本地地址）。

### 2. 出站 socket 绑定客户端源端口

转发给 PS5 的 UDP socket 优先绑定「客户端的源端口」，让 PS5 侧看到的端口关系更接近局域网直连；被占用时依次回退到服务端口、临时端口。

### 3. 不用 nftables / socat

| 方案 | 问题 |
|---|---|
| nftables DNAT | 不支持 IPv6 → IPv4 跨地址族 |
| socat UDP-LISTEN,fork | `-T` 超时会杀掉监听进程；不指定源地址；每次 fork 复制开销 |
| TAYGA/NAT64 | 需要改路由与地址规划，重 |

## 文件

```
relay/ps5-relay.py      中继程序（纯标准库，无第三方依赖）
systemd/ps5-relay.service
scripts/install.sh      幂等安装
scripts/verify.sh       部署后自检
AGENTS.md               给 AI 助手的执行手册
```

## 限制

- 只做单台 PS5、固定端口；多台需要改造成按目标地址区分
- 不做访问控制：端口对全网开放，不串流时可 `systemctl stop ps5-relay`
- 客户端必须支持手动填 IPv6 主机地址
