# PS5 远程串流 · IPv6 → IPv4 中继

家里只有公网 IPv6、PS5 只有 IPv4 时，用一台**和 PS5 在同一局域网的 Linux 设备**做中继，把 PS5 的串流端口从公网 IPv6 转到内网 IPv4，让手机/掌机在外网直连自己的 PS5。

> 想省事：把下面「一、让 AI 帮你装」里那段 prompt 整段复制给 AI，剩下的它自己会做完。

```
手机(IPv6/域名) ──公网 IPv6──▶ 中继主机(Linux, 同局域网) ──内网 IPv4──▶ PS5
```

---

# 一、让 AI 帮你装

把下面这段整段复制给任意 AI 助手（它会读仓库里的 AGENTS.md 并自己执行）：

```text
请在这台机器上部署 PS5 远程串流的 IPv6 → IPv4 中继。

仓库: https://github.com/rakaki/ps5-remote-play-ipv6-relay
怎么读: 先读仓库里的 AGENTS.md，按其中的「平台判断 → 执行步骤 → 验收标准」做；
        读不到 AGENTS.md 就按 README 的「二、部署」做。

环境: 这台 Linux 与 PS5 在同一局域网，PS5 内网 IP 是 <填你的 PS5 IP>。

要求:
1. 先做前置检查（有全局 IPv6、能 ping 通 PS5、PS5 的 9295 能连、systemd 可用），
   任一项不满足就先告诉我，不要硬装
2. 执行 scripts/install.sh <PS5 IP>
3. 跑 scripts/verify.sh，确认 TCP/UDP 9295-9304 与 UDP 987 都在监听
4. 告诉我客户端（PeaSyo / Chiaki）该填哪个 IPv6 地址
5. 每一步说明在做什么；不要用 nftables DNAT，也不要用 socat / netsh / nginx 替代
```

AI 装完你可以按这三条验收：

| 检查 | 命令 | 期望 |
|---|---|---|
| 服务在跑 | `systemctl is-active ps5-relay` | `active` |
| 端口齐全 | `ss -lntu \| grep -E ':(987\|929[5-9]\|930[0-4])'` | TCP 与 UDP 都有 |
| 串流有双向流量 | `journalctl -u ps5-relay -f` | `c->ps5` 与 `ps5->c` 同时增长 |

有两件事 AI 替不了你，需要人工在设备上操作：

1. PS5 上开启「设置 → 系统 → 远程游玩 → 启用远程游玩」，以及待机模式里的「保持与互联网连接」
2. 如果外网连不上，去路由器放行这台主机的 IPv6 入站

---

# 二、部署（在 Linux 上执行）

> 所有命令都在**与 PS5 处于同一局域网的 Linux 设备**上执行，不是 PS5 上，也不是手机上。
> 这台设备需要能上公网 IPv6（入站不被路由器拦截），并能用 IPv4 访问 PS5。

## 0. 准备

先在 PS5 上开启远程游玩：

| 位置 | 设置 |
|---|---|
| 设置 → 系统 → 远程游玩 | 启用远程游玩 |
| 设置 → 系统 → 省电 → 待机模式中的功能 | 开启「保持与互联网连接」「允许从网络开启 PS5」 |

再在路由器里按 MAC 给 PS5 绑定固定内网 IP（示例统一用 `192.168.0.14`）。

## 1. 执行

```bash
git clone https://github.com/rakaki/ps5-remote-play-ipv6-relay.git
cd ps5-remote-play-ipv6-relay
sudo ./scripts/install.sh 192.168.0.14
```

脚本会装好 `/usr/local/bin/ps5-relay.py`、写入并启动 `ps5-relay.service`（已设为开机自启），最后打印监听状态。

确认监听：

```bash
ss -lntu | grep -E ':(987|929[5-9]|930[0-4])\b'
```

## 2. 客户端填写中继主机的地址

在 PeaSyo（或 Chiaki / chiaki-ng）里把主机地址填成**这台 Linux 设备的公网 IPv6，或指向它的域名**：

```bash
ip -6 addr show scope global | grep inet6     # 取一个 2xxx / 240e 开头的地址
```

| 填法 | 示例 | 说明 |
|---|---|---|
| 公网 IPv6 | `240e:3a3:60b:8410:xxxx:xxxx:xxxx:xxxx` | 最直接；前缀变了要改 |
| 域名（推荐长期用） | `ps5.example.com` | 用 DDNS 把 AAAA 记录指向该主机的 IPv6，前缀变化不用改客户端 |

端口不用改，仍按客户端默认（9295/9296/9297）；配对 PIN 与局域网串流完全相同。

## 3. 验证

```bash
./scripts/verify.sh 240e:xxxx::1        # 参数可选，只是回显给人看
journalctl -u ps5-relay -f              # 串流时观察双向流量
```

串流正常时，日志里 `UDP :9296` 的 `c->ps5` 和 `ps5->c` 会同时增长，`ps5->c` 通常大得多（视频流），`max` 约 1200-1300 B。

---

# 三、为什么必须中继

PS5 本身不提供任何可直连的 IPv6 入口，实测结论：

| 测试 | 结果 | 含义 |
|---|---|---|
| `ping6` PS5 的链路本地地址 | 有回应 | IPv6 协议栈存在 |
| `ping6` 按 MAC 推导的全局地址 | 无回应，邻居表 FAILED | 没有全局 IPv6 地址 |
| `TCP [fe80::…]:9295` | Connection refused（RST） | Remote Play 服务不监听 IPv6 |
| `TCP 192.168.0.14:9295` | 连接成功 | 服务只监听 IPv4 |

而 Linux 的 nftables/iptables **不支持跨地址族 DNAT**（IPv6 → IPv4），所以不能靠一条 NAT 规则解决，只能由用户态程序在中间搬数据。

## 端口

| 端口 | 协议 | 用途 |
|---|---|---|
| 9295 | TCP | 注册与会话控制 |
| 9296 | UDP | 音视频流 + 带宽探测（Senkusha） |
| 9297 | UDP | 反馈 / 拥塞控制 |
| 9302 | UDP | PS5 发现 |
| 987 | UDP | 唤醒（WoL） |
| 9295-9304 | TCP+UDP | 兜底整段（不确定时全开） |

---

# 四、故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 客户端转圈十几秒后失败、无错误码 | UDP 回包源地址与客户端连接的目标地址不一致，被客户端内核丢弃 | 见「设计要点 1」，确认用的是本仓库的程序 |
| 日志里 PS5 只回 1 个包后完全沉默 | 同上（包到了但客户端当噪音丢了，于是不停重发） | 同上 |
| 日志有 `TCP connect ... failed: refused` | PS5 没开远程游玩 / 不在待机联网状态 | 回 PS5 开启 |
| 能连但黑屏 | 上行带宽不足 / 视频包被路径 MTU 黑洞 | 看日志 `max`，正常应 ≤1252 |
| 外网完全连不上 | 路由器 IPv6 防火墙拦截入站 | 在路由器放行该主机，或关闭 IPv6 防火墙 |
| 换网络后连不上 | 公网 IPv6 前缀变了 | 更新客户端地址，或上 DDNS |
| 提示地址被占用 | 同一台机器上已有进程占了端口 | `ss -lntup \| grep 9296` 查占用 |

---

# 五、设计要点（踩过的坑）

## 1. 回包必须指定源地址

本机通常有多个公网 IPv6（EUI-64 + stable-privacy）。UDP 是无连接的，内核默认用「首选源地址」回包，于是：

```
客户端 →  240e:...:7e83:34ff:febe:b5d3:9296     （它连的是这个地址）
本机   ←  240e:...:f567:2190:5d8c:17a0:9296     （回包却用另一个）
```

客户端如果用连接型 UDP socket（`connect` + `recv`），内核只交付「源地址 = 连接目的地址」的包，其余全部丢弃。现象就是 PS5 明明回了包，客户端却像没收到，最后超时。

因此程序对监听 socket 打开 `IPV6_RECVPKTINFO`，记录每个包发往本机哪个地址，回包时用 `sendmsg` + `IPV6_PKTINFO` 强制以该地址为源地址。TCP 没有这个问题（连接自带本地地址）。

## 2. 出站 socket 绑定客户端源端口

转发给 PS5 的 UDP socket 优先绑定「客户端的源端口」，让 PS5 侧看到的端口关系更接近局域网直连；被占用时依次回退到服务端口、临时端口。

## 3. 不用 nftables / socat

| 方案 | 问题 |
|---|---|
| nftables DNAT | 不支持 IPv6 → IPv4 跨地址族 |
| socat UDP-LISTEN,fork | `-T` 超时会杀掉监听进程；不指定源地址；每次 fork 复制开销 |
| TAYGA/NAT64 | 需要改路由与地址规划，重 |

---

# 六、Windows 上能不能做

可以做，但没有 Linux 顺手，主要卡在 UDP 的源地址与端口转发工具。详见 [docs/windows.md](docs/windows.md)，结论：

| 方案 | UDP | 源地址可控 | 结论 |
|---|---|---|---|
| `netsh interface portproxy` | ❌ 只支持 TCP | — | 不可用 |
| Windows 原生 Python（`windows/ps5-relay-win.py`） | ✅ | 依赖系统选择（单一公网地址时正确） | 可用，需关闭临时 IPv6 地址 |
| WSL2 + 本仓库脚本 | ✅ | ✅ | 推荐，需 Win11 22H2+ mirrored 网络 |
| nginx for Windows | 部分 | 需写死监听地址 | 不推荐（beta、地址变化要改配置） |

---

# 七、其它方案对比与致谢

## 同类方案：Wfass/ps5-ipv6-socat

[Wfass/ps5-ipv6-socat](https://github.com/Wfass/ps5-ipv6-socat) 解决的是同一个问题，思路也一致（同局域网 Linux 主机做 IPv6 → IPv4 中继），实现用 `socat` + systemd + `while` 循环自恢复。它不是无脑转发——作者的排障表里记录了"源地址漂移导致连上就断"，并做了处理。

| 维度 | ps5-ipv6-socat | 本仓库 |
|---|---|---|
| 依赖 | 仅 `socat`（apt/yum/apk 安装） | Python 3.8+（无第三方库） |
| 端口 | 9295 / 9296 / 9297 / 9302 | 9295-9304 + UDP 987 |
| 远程唤醒（UDP 987） | 未转发，外面唤不醒待机的 PS5 | 支持 |
| 源地址处理 | 禁用 IPv6 临时地址 | 按客户端连接的目标地址强制回包源地址 |
| UDP 多对端 | `UDP6-LISTEN` 未加 `fork`，同一端口只服务一个对端 | 按 (客户端地址, 端口) 建立独立会话 |
| 出站源端口 | 随机 | 优先绑定客户端源端口 |
| 可观测性 | 无日志，靠 `pgrep` / `ss` / `tcpdump` | 每 10 秒打印双向包数、字节、最大包长 |
| Windows | 无 | 有方案与脚本 |

两个会实际影响使用的点：

1. **"禁用临时地址"不足以保证只有一个全局地址。** 有些系统（例如同时保留 stable-privacy 与 EUI-64 的情况）关掉临时地址后仍有两条全局地址，此时回包源地址仍可能不匹配。判断方法：`ip -6 addr show scope global | grep -c inet6`，结果大于 1 就有风险。
2. **UDP 未加 `fork` 时语义上是单对端。** 客户端（手机 App）每次重开的源端口不同，换客户端后可能收不到包，要等 `-T` 空闲超时重建，表现为"第一次能连，重连连不上"。

反过来，如果你的环境是**只有一个全局 IPv6、单台客户端、不需要远程唤醒**，对方的方案更轻——不用装 Python，两条命令就能跑起来。

## 其它替代路线

| 路线 | 前提 | 局限 |
|---|---|---|
| 公网 IPv4 + 端口映射 / DMZ | 宽带能拿到公网 IPv4 | CGNAT 下完全不可行；DMZ 把整台 PS5 暴露到公网 |
| Tailscale / ZeroTier 等组网 | 客户端能装 App | PS5 装不了客户端；需要在 Linux 上做子网路由，且客户端要能加虚拟网卡 |
| PSN 打洞（官方直连） | NAT 类型较好 | 经常打不通或绕中转，延迟和稳定性都不稳 |
| 官方云串流 | Plus 会员 | 依赖订阅与服务器，不是"连自己家主机" |
| 本仓库（IPv6 → IPv4 中继） | 家宽有公网 IPv6 + 一台常开 Linux | 需要客户端支持手动填 IPv6/域名 |

## 怎么选

| 你的情况 | 建议 |
|---|---|
| 只有一个全局 IPv6、单设备、不需要唤醒 | ps5-ipv6-socat 就够，部署最轻 |
| 多个全局 IPv6，或经常换设备重连 | 本仓库 |
| 需要在外面把 PS5 从待机唤醒 | 本仓库（含 UDP 987） |
| 完全连不上，想先确认原因 | 本仓库的日志能直接看出卡在哪一步 |
| 有公网 IPv4 | 路由器端口映射即可，不必上中继 |

## 致谢

- [Wfass/ps5-ipv6-socat](https://github.com/Wfass/ps5-ipv6-socat)：同类方案的先行参考，验证了"公网 IPv6 打洞进门 + 本地翻译成 PS5 的 IPv4"这条路线可行；其排障表记录的"源地址漂移"现象，也是本项目定位问题的旁证。
- [Chiaki](https://github.com/thestr4ng3r/chiaki) / [chiaki-ng](https://github.com/streetpea/chiaki-ng)：开源串流客户端的协议实现，Senkusha 端口与流程参考自此。
- [socat](http://www.dest-unreach.org/socat/)：很多同类方案的基础工具。
- PS5 远程串流社区的前人踩坑记录。

---

# 八、文件

```
relay/ps5-relay.py          中继程序（Linux/Unix，纯标准库）
windows/ps5-relay-win.py    Windows 版（去掉 pktinfo，实验性）
systemd/ps5-relay.service
scripts/install.sh          幂等安装
scripts/verify.sh           部署后自检
docs/windows.md             Windows 方案与步骤
AGENTS.md                   给 AI 助手的执行手册
```

# 九、限制

- 只做单台 PS5、固定端口；多台需要按目标地址区分改造
- 不做访问控制：端口对全网开放，不串流时可 `systemctl stop ps5-relay`
- 客户端必须支持手动填 IPv6 地址或域名（PeaSyo / Chiaki / chiaki-ng 可以，官方 PS Remote Play App 不可以）
- 中继主机必须在 PS5 所在局域网内；跨网段需要额外路由
