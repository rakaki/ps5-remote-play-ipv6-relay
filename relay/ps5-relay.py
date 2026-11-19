#!/usr/bin/env python3
"""PS5 远程串流: 公网 IPv6 -> 局域网 PS5(IPv4) 端口中继.

关键点: UDP 是无连接的, 回包必须用"客户端当初连接的那个目的地址"作为源地址,
否则客户端(连接型 UDP socket)会把回包当成别的对端而丢弃。
为此监听 socket 打开 IPV6_RECVPKTINFO, 收到包时记下目的地址, 回包用 sendmsg 指定源地址。
"""
import asyncio
import logging
import os
import signal
import socket
import sys
import time

PS5_IP = os.environ.get("PS5_IP", "192.168.0.14")


def _local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((PS5_IP, 9295))
        return s.getsockname()[0]
    finally:
        s.close()


LOCAL_IP = os.environ.get("LOCAL_IP") or _local_ip()
TCP_PORTS = [int(p) for p in os.environ.get("TCP_PORTS", "9295,9296,9297,9298,9299,9300,9301,9302,9303,9304").split(",") if p.strip()]
UDP_PORTS = [int(p) for p in os.environ.get("UDP_PORTS", "987,9295,9296,9297,9298,9299,9300,9301,9302,9303,9304").split(",") if p.strip()]
UDP_IDLE = float(os.environ.get("UDP_IDLE", "300"))

IPV6_RECVPKTINFO = getattr(socket, "IPV6_RECVPKTINFO", 49)
IPV6_PKTINFO = getattr(socket, "IPV6_PKTINFO", 50)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
log = logging.getLogger("ps5-relay")

CLOSE_ERRORS = (ConnectionResetError, BrokenPipeError, ConnectionAbortedError,
                asyncio.IncompleteReadError, TimeoutError, OSError)


async def _pump(reader, writer):
    """单向搬运; 对端 EOF 时只做半关闭, 由调用方在双向都结束后再整体关闭。"""
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
    local = cwriter.get_extra_info("sockname")
    log.info("TCP %s -> %s:%d (via %s)", peer, PS5_IP, port, local[0] if local else "?")
    try:
        rread, rwrite = await asyncio.wait_for(asyncio.open_connection(PS5_IP, port), 10)
    except Exception as exc:
        log.warning("TCP connect %s:%d failed: %s", PS5_IP, port, exc)
        cwriter.close()
        return
    for w in (cwriter, rwrite):
        sock = w.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
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


class UdpSession:
    """一个客户端地址对应一个会话: 一条到 PS5 的 IPv4 UDP socket。"""

    def __init__(self, listener, client, dest, port):
        self.listener = listener
        self.client = client          # (host, port, flowinfo, scope_id)
        self.dest = dest              # 客户端发来时用的目的地址(本机某个 IPv6)
        self.port = port
        self.sock = None
        self.last = time.monotonic()
        self.c2p = [0, 0]
        self.p2c = [0, 0]
        self.p2c_max = 0
        self.local = None

    def start(self, first):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for bind_port in (self.client[1], self.port, 0):
            try:
                sock.bind((LOCAL_IP, bind_port))
                break
            except OSError:
                if bind_port == 0:
                    sock.close()
                    log.warning("UDP %s:%d bind failed", PS5_IP, self.port)
                    return False
        sock.setblocking(False)
        self.sock = sock
        self.local = sock.getsockname()[1]
        self.listener.loop.add_reader(sock.fileno(), self._on_ps5)
        self._send(first)
        log.info("UDP %s:%d -> %s:%d (local %d, dest %s)", self.client[0], self.client[1],
                 PS5_IP, self.port, self.local, self.dest)
        return True

    def _send(self, data):
        self.last = time.monotonic()
        self.c2p[0] += 1
        self.c2p[1] += len(data)
        try:
            self.sock.sendto(data, (PS5_IP, self.port))
        except OSError:
            pass

    def _on_ps5(self):
        try:
            data, _ = self.sock.recvfrom(65535)
        except (BlockingIOError, InterruptedError, OSError):
            return
        self.last = time.monotonic()
        self.p2c[0] += 1
        self.p2c[1] += len(data)
        self.p2c_max = max(self.p2c_max, len(data))
        self.listener.send_to_client(data, self.client, self.dest)

    def close(self):
        if self.sock is not None:
            try:
                self.listener.loop.remove_reader(self.sock.fileno())
            except Exception:
                pass
            self.sock.close()
            self.sock = None


class UdpListener:
    def __init__(self, port):
        self.port = port
        self.loop = asyncio.get_running_loop()
        self.sessions = {}
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.setsockopt(socket.IPPROTO_IPV6, IPV6_RECVPKTINFO, 1)
        sock.bind(("::", port))
        sock.setblocking(False)
        self.sock = sock
        self.loop.add_reader(sock.fileno(), self._on_readable)
        self._gc = asyncio.ensure_future(self._reaper())

    async def _reaper(self):
        while True:
            await asyncio.sleep(10)
            now = time.monotonic()
            for s in list(self.sessions.values()):
                if s.c2p[0] or s.p2c[0]:
                    log.info("UDP :%d c->ps5 %d pkt/%dB | ps5->c %d pkt/%dB (max %dB, local %s)",
                             self.port, s.c2p[0], s.c2p[1], s.p2c[0], s.p2c[1], s.p2c_max, s.local)
            dead = [k for k, s in self.sessions.items() if now - s.last > UDP_IDLE]
            for key in dead:
                self.sessions.pop(key).close()
                log.info("UDP session %s:%d idle, closed", key[0], self.port)

    def _on_readable(self):
        try:
            data, ancdata, flags, addr = self.sock.recvmsg(65535, socket.CMSG_SPACE(20))
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            return
        dest = None
        for level, ctype, cdata in ancdata:
            if level == socket.IPPROTO_IPV6 and ctype == IPV6_PKTINFO and len(cdata) >= 16:
                dest = socket.inet_ntop(socket.AF_INET6, cdata[:16])
                break
        key = (addr[0], addr[1])
        session = self.sessions.get(key)
        if session is not None:
            session._send(data)
            return
        session = UdpSession(self, addr, dest, self.port)
        self.sessions[key] = session
        if not session.start(data):
            self.sessions.pop(key, None)

    def send_to_client(self, data, client, dest):
        if dest:
            pktinfo = socket.inet_pton(socket.AF_INET6, dest) + b"\x00\x00\x00\x00"
            try:
                self.sock.sendmsg([data], [(socket.IPPROTO_IPV6, IPV6_PKTINFO, pktinfo)], 0, client)
                return
            except OSError:
                pass
        try:
            self.sock.sendto(data, client)
        except OSError:
            pass

    def close(self):
        try:
            self.loop.remove_reader(self.sock.fileno())
        except Exception:
            pass
        self.sock.close()
        for s in list(self.sessions.values()):
            s.close()


def _tcp_listen(port):
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("::", port))
    sock.listen(64)
    sock.setblocking(False)
    return sock


async def main():
    loop = asyncio.get_running_loop()
    keep = []
    for port in sorted(TCP_PORTS):
        server = await asyncio.start_server(
            lambda r, w, p=port: tcp_session(r, w, p), sock=_tcp_listen(port))
        keep.append(server)
        log.info("listen TCP [::]:%d -> %s:%d", port, PS5_IP, port)
    for port in sorted(UDP_PORTS):
        keep.append(UdpListener(port))
        log.info("listen UDP [::]:%d -> %s:%d", port, PS5_IP, port)

    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    log.info("ready: %d tcp / %d udp ports", len(TCP_PORTS), len(UDP_PORTS))
    await stop.wait()

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
