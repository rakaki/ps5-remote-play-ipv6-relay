# Windows 上做这台中继

**结论：能做，但比 Linux 麻烦。** 卡点不在"转发"，而在两件事：

1. Windows 自带的端口转发工具 `netsh interface portproxy` **只转发 TCP，不支持 UDP**，而 PS5 串流的画面全靠 UDP。
2. Windows 的 Python 没有 `recvmsg`/`sendmsg`，拿不到"数据包发往本机哪个 IPv6"，也就无法像 Linux 版那样强制回包源地址（详见根目录 README「设计要点 1」）。

推荐度排序：

| 方案 | UDP | 源地址可控 | 需要什么 | 推荐度 |
|---|---|---|---|---|
| A. WSL2 + 本仓库 Linux 脚本 | ✅ | ✅ | Win11 22H2+ | ★★★★ |
| B. Windows 原生 Python（`windows/ps5-relay-win.py`） | ✅ | 依赖系统（需单一公网地址） | Python 3.8+ | ★★★ |
| C. nginx for Windows | 部分 | 需写死监听地址 | 装 nginx | ★ |
| D. `netsh portproxy` | ❌ 只 TCP | — | — | ✗ 不可用 |

前置条件（三个方案都一样）：

- Windows 机器与 PS5 在同一局域网，且能拿到**公网 IPv6**
- 路由器没有拦截入站 IPv6
- PS5 已开启远程游玩（见 README 第一节）

---

## 方案 A：WSL2 + 本仓库脚本（推荐）

适用：Windows 11 22H2（build 22621）及以上。mirrored 网络模式下 WSL 与宿主机共享网络栈和 IP，入站 IPv6 直接进 WSL。

1. 启用 mirrored 网络。编辑 `%UserProfile%\.wslconfig`：

```ini
[wsl2]
networkingMode=mirrored
```

2. 重启 WSL：

```powershell
wsl --shutdown
```

3. 在 WSL 里按 README 第一节部署（WSL 就是 Linux）：

```bash
git clone https://github.com/rakaki/ps5-remote-play-ipv6-relay.git
cd ps5-remote-play-ipv6-relay
sudo ./scripts/install.sh 192.168.0.14
```

4. 放行 Hyper-V 防火墙（mirrored 模式下入站流量由 Hyper-V Firewall 管，默认拒绝）。管理员 PowerShell：

```powershell
Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow
```

5. 客户端填 Windows 机器的公网 IPv6。在 Windows 上看：

```powershell
Get-NetIPAddress -AddressFamily IPv6 | Where-Object { $_.PrefixOrigin -ne 'WellKnown' }
```

注意：mirrored 模式下 WSL 与 Windows 共用端口，宿主机不能同时占着 9295。

WSL 里的 systemd 默认可能未启用，若 `systemctl` 不可用，在 `/etc/wsl.conf` 里加 `[boot]\nsystemd=true` 后 `wsl --shutdown` 重启，或直接 `sudo python3 /usr/local/bin/ps5-relay.py` 前台运行。

---

## 方案 B：Windows 原生 Python

适用：只有一个公网 IPv6 的 Windows。若系统同时存在多个全局地址（常见于开着临时地址），回包源地址可能选错，客户端会表现为"连上但转圈超时"。

1. 安装 Python 3.8+：

```powershell
winget install Python.Python.3.12
```

2. 关掉临时 IPv6 地址，保证公网地址唯一（管理员 CMD，执行后重启网卡或重启系统）：

```bat
netsh interface ipv6 set privacy state=disabled store=persistent
ipconfig /release6
ipconfig /renew6
```

3. 放行防火墙（管理员 PowerShell）：

```powershell
New-NetFirewallRule -DisplayName "PS5 Relay TCP" -Direction Inbound -Protocol TCP -LocalPort 9295-9304 -Action Allow -Profile Any
New-NetFirewallRule -DisplayName "PS5 Relay UDP 9295-9304" -Direction Inbound -Protocol UDP -LocalPort 9295-9304 -Action Allow -Profile Any
New-NetFirewallRule -DisplayName "PS5 Relay UDP 987" -Direction Inbound -Protocol UDP -LocalPort 987 -Action Allow -Profile Any
```

4. 运行：

```bat
set PS5_IP=192.168.0.14
python windows\ps5-relay-win.py
```

5. 开机自启（可选）。建一个 `start-relay.bat`：

```bat
@echo off
set PS5_IP=192.168.0.14
cd /d %~dp0
python ps5-relay-win.py >> relay.log 2>&1
```

再注册计划任务（管理员 CMD）：

```bat
schtasks /create /tn "PS5 Relay" /tr "C:\\path\\to\\start-relay.bat" /sc onstart /ru SYSTEM /rl highest
```

6. 验证：

```powershell
netstat -ano | findstr :9295
Get-NetIPAddress -AddressFamily IPv6 | Where-Object { $_.PrefixOrigin -ne 'WellKnown' }
```

第二条第输出如果有多于一个全局地址，就会踩源地址问题——要么再关掉多余的地址，要么改用方案 A。

---

## 方案 C：nginx for Windows（不推荐）

nginx 的 stream 模块支持 UDP 代理：

```nginx
stream {
    server {
        listen [::]:9296 udp;
        proxy_pass 192.168.0.14:9296;
    }
}
```

三个问题让它不划算：

1. `listen [::]` 时回包源地址同样由系统选，多地址会出错；要可靠必须写死 `listen [240e:...]:9296 udp`，地址一变就得改配置
2. nginx for Windows 是 beta，基于 select，UDP 并发表现一般
3. 每个端口一节配置，11 个 UDP 端口要写一堆

---

## 方案 D：netsh interface portproxy（不可用）

```bat
netsh interface portproxy add v6tov4 listenaddress=:: listenport=9295 connectaddress=192.168.0.14 connectport=9295
```

语法上支持 v6tov4，但**仅限 TCP**。9295 的握手能通，UDP 的 9296/9297 完全不过去，结果就是"连上了但一直黑屏/超时"。

---

## 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| 客户端转圈十几秒失败 | 回包源地址与客户端连接地址不一致（多全局地址） | 关临时地址，或改用方案 A |
| 外部完全连不上 | Windows 防火墙 / Hyper-V 防火墙 / 路由器 IPv6 防火墙 | 逐层放行 |
| 端口已被占用 | 其他程序占用 | `netstat -ano \| findstr :9296` 找 PID |
| 连上后立刻断 | PS5 未开远程游玩，或待机未保持联网 | 回 PS5 设置 |
| 家里重启后失效 | 公网 IPv6 前缀变化 | 客户端改地址，或用 DDNS 指向 Windows 主机 |

## 一句话建议

如果这台机器本来就是 Windows，又恰好是 Win11 22H2+，直接上 **WSL2**——脚本一行不用改，源地址问题由 Linux 网络栈正确处理。
