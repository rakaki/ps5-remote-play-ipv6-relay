#!/usr/bin/env python3
"""PS5 串流: 公网 IPv6 -> 内网 PS5(IPv4) 中继 —— Windows 版（实验性）.

与 Linux 版的区别: Windows 的 Python 没有 recvmsg/sendmsg, 拿不到数据包的目的地址,
因此无法像 Linux 版那样强制回包源地址。这里依赖系统的 IPv6 源地址选择:
只要本机公网 IPv6 只有一个, 回包源地址就会与客户端连接的目标一致, 工作正常。
若本机有多个公网 IPv6(含临时地址), 请先关闭临时地址, 或改用 WSL2 + Linux 版脚本。

用法:
    set PS5_IP=192.168.0.14
    python ps5-relay-win.py
"""
import asyncio
import logging
import os
import signal
import socket
import sys
import time

PS5_IP = os.environ.get("PS5_IP", "192.168.0.14")
TCP_PORTS = [int(p) for p in os.environ.get("TCP_PORTS", "9295,9296,9297,9298,9299,9300,9301,9302,9303,9304").split(",") if p.strip()]
UDP_PORTS = [int(p) for p in os.environ.get("UDP_PORTS", "987,9295,9296,9297,9298,9299,9300,9301,9302,9303,9304").split(",") if p.strip()]
UDP_IDLE = float(os.environ.get("UDP_IDLE", "300"))


def _local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((PS5_IP, 9295))
        return s.getsockname()[0]
    finally:
        s.close()


try:
    LOCAL_IP = os.environ.get("LOCAL_IP") or _local_ip()
except OSError:
    LOCAL_IP = os.environ.get("LOCAL_IP", "0.0.0.0")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
log = logging.getLogger("ps5-relay-win")

CLOSE_ERRORS = (ConnectionResetError, ConnectionAbortedError, asyncio.IncompleteReadError, OSError)


async def _pump(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except CLOSE_ERRORS:
        pass
    try:
        if writer.can_write_eof():
            writer.write_eof()
    except Exception:
        pass


async def tcp_session(creader, cwriter, port):
    peer = cwriter.get_extra_info("peername")
    log.info("TCP %s -> %s:%d", peer, PS5_IP, port)
    try:
        rread, rwrite = await asyncio.wait_for(asyncio.open_connection(PS5_IP, port), 10)
    except Exception as exc:
        log.warning("TCP connect %s:%d failed: %s", PS5_IP, port, exc)
        cwriter.close()
        return
    for w in (cwriter, rwrite):
        s = w.get_extra_info("socket")
        if s is not None:
            try:
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
    try:
        await asyncio.gather(_pump(creader, rwrite), _pump(rread, cwriter))
    finally:
        for w in (cwriter, rwrite):
            try:
                w.close()
            except Exception:
                pass
        log.info("TCP %s closed", peer)


class UdpSession(asyncio.DatagramProtocol):
    def __init__(self, listener, client, port):
        self.listener = listener
        self.client = client
        self.port = port
        self.transport = None
        self.pending = []
        self.last = time.monotonic()
        self.c2p = [0, 0]
        self.p2c = [0, 0]
        self.p2c_max = 0
        self.local = None

    def connection_made(self, transport):
        self.transport = transport
        self.local = transport.get_extra_info("sockname")[1]
        for data in self.pending:
            transport.sendto(data, (PS5_IP, self.port))
        self.pending.clear()
        log.info("UDP %s:%d -> %s:%d (local %d)", self.client[0], self.client[1], PS5_IP, self.port, self.local)

    def send(self, data):
        self.last = time.monotonic()
        self.c2p[0] += 1
        self.c2p[1] += len(data)
        if self.transport is not None:
            try:
                self.transport.sendto(data, (PS5_IP, self.port))
            except OSError:
                pass
        else:
            self.pending.append(data)

    def datagram_received(self, data, addr):
        self.last = time.monotonic()
        self.p2c[0] += 1
        self.p2c[1] += len(data)
        self.p2c_max = max(self.p2c_max, len(data))
        try:
            self.listener.transport.sendto(data, self.client)
        except OSError:
            pass

    def error_received(self, exc):
        pass

    def close(self):
        if self.transport is not None:
            self.transport.close()
            self.transport = None


class UdpListener(asyncio.DatagramProtocol):
    def __init__(self, port):
        self.port = port
        self.transport = None
        self.sessions = {}

    def connection_made(self, transport):
        self.transport = transport
        asyncio.ensure_future(self._reaper())

    async def _reaper(self):
        while True:
            await asyncio.sleep(10)
            now = time.monotonic()
            for s in list(self.sessions.values()):
                if s.c2p[0] or s.p2c[0]:
                    log.info("UDP :%d c->ps5 %d pkt/%dB | ps5->c %d pkt/%dB (max %dB)",
                             self.port, s.c2p[0], s.c2p[1], s.p2c[0], s.p2c[1], s.p2c_max)
            dead = [a for a, s in self.sessions.items() if now - s.last > UDP_IDLE]
            for addr in dead:
                self.sessions.pop(addr).close()
                log.info("UDP session %s:%d idle, closed", addr[0], self.port)

    def datagram_received(self, data, addr):
        key = (addr[0], addr[1])
        session = self.sessions.get(key)
        if session is not None:
            session.send(data)
            return
        session = UdpSession(self, addr, self.port)
        self.sessions[key] = session
        asyncio.ensure_future(self._open(session, data))

    async def _open(self, session, first):
        loop = asyncio.get_running_loop()
        for bind_port in (session.client[1], session.port, 0):
            try:
                await loop.create_datagram_endpoint(
                    lambda: session, local_addr=(LOCAL_IP, bind_port), family=socket.AF_INET)
                session.send(first)
                return
            except OSError:
                if bind_port == 0:
                    log.warning("UDP open %s:%d failed", PS5_IP, session.port)
                    self.sessions.pop((session.client[0], session.client[1]), None)
                    return


def _v6_socket(kind, port):
    sock = socket.socket(socket.AF_INET6, kind)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except OSError:
        pass
    try:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    except OSError:
        pass
    sock.bind(("::", port))
    return sock


async def main():
    loop = asyncio.get_running_loop()
    keep = []
    for port in sorted(TCP_PORTS):
        sock = _v6_socket(socket.SOCK_STREAM, port)
        sock.listen(64)
        sock.setblocking(False)
        server = await asyncio.start_server(lambda r, w, p=port: tcp_session(r, w, p), sock=sock)
        keep.append(server)
        log.info("listen TCP [::]:%d -> %s:%d", port, PS5_IP, port)
    for port in sorted(UDP_PORTS):
        transport, _ = await loop.create_datagram_endpoint(
            lambda p=port: UdpListener(p), sock=_v6_socket(socket.SOCK_DGRAM, port))
        keep.append(transport)
        log.info("listen UDP [::]:%d -> %s:%d", port, PS5_IP, port)

    log.info("ready: %d tcp / %d udp ports (local %s)", len(TCP_PORTS), len(UDP_PORTS), LOCAL_IP)
    if sys.platform == "win32":
        log.warning("Windows 版不做源地址修正: 请确保本机公网 IPv6 只有一个(禁用临时地址)")

    try:
        await asyncio.Event().wait()
    finally:
        for item in keep:
            try:
                item.close()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
