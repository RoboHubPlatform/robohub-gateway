#!/usr/bin/env python3
"""A gateway restart, as the boards that hold a session see it.

The gateway keeps each session's counters on disk every SECURE_STORE_EVERY
frames and, at start, places a restored session's upward counter twice that
past what it kept, so nothing recorded before the restart can be replayed after
it. A board does not know the gateway restarted and goes on counting where it
was — below that mark — so the gateway asks it, sealed under the restored
session, for a new one (SESSION_RESET). This checks that:

- on the serial link, the request goes down as soon as the link is open,
  sealed under the restored session's K_down with a counter past anything the
  board accepted, and the board's next session is heard at once;
- a frame recorded before the restart is still refused, before and after the
  board's new session, and asks for nothing;
- a frame refused because its counter is within the restored window asks
  again, at most once in ten seconds;
- a board behind a relay is asked inside RELAYED, through the relay's new
  session, and a board on an uplink connection on the connection it dialled
  again — both on their first refused frame;
- the restored session is dropped once the new one carries a frame;
- the real C host node (robohub-devices' gateway_node, built with
  robohub-transport) on the serial link is heard again within seconds of the
  restart, with no counter-window silence (skipped when it is not built).

Run it after `rho build`:

    python3 tools/restart-smoke.py
"""
import json
import os
import pty
import select
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smoke  # noqa: E402
from smoke import Board, device, frame, hmac16, parse_frame, wait_ready  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORKSPACE = os.environ.get("ROBOHUB_WORKSPACE") or os.environ.get("RHO_WORKSPACE") or os.path.dirname(ROOT)
NODE_BIN = os.environ.get("NODE_BIN", os.path.join(WORKSPACE, "robohub-devices", "nodes", "gateway-node", "build",
                                                   "output", "gateway_node"))
HOST = "127.0.0.1"
HTTP_PORT = 18102
UPLINK_PORT = 18103
UDP_PORT = 19902
AREA = 7
SESSION_RESET = 0x0016
RELAYED = 0x0013

failures = []


def check(ok: bool, message: str) -> None:
    print(("ok   " if ok else "FAIL ") + message)
    if not ok:
        failures.append(message)


def telemetry(area: int, node: int, temp_c: float) -> bytes:
    return b"\x01" + device(area, node) + struct.pack("<IfI", 1000, temp_c, 0)


def write_manifest(path: str, rows) -> None:
    temporary = path + ".tmp"
    with open(temporary, "w") as f:
        f.write("# uid|area|node|role|epoch|revoked|areas\n")
        for row in rows:
            f.write(row + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def start(env):
    proc = subprocess.Popen([smoke.BIN], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    wait_ready(proc)
    return proc


def stop(proc) -> str:
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    return out or ""


def pump() -> None:
    time.sleep(0.15)
    smoke.http("POST", "/api/v1/uplink/pump")


def open_down(board: Board, msg_id: int, tx_id: int, payload: bytes):
    """A frame the gateway sealed for a board, opened as the board does: its
    session id, counter and inner payload, or None when it does not verify."""
    if not msg_id & 0x8000 or len(payload) < 14:
        return None
    sid_counter, inner, tag = payload[:6], payload[6:-8], payload[-8:]
    want = hmac16(board.k_down, struct.pack("<HH", msg_id, tx_id) + sid_counter + inner)[:8]
    if sid_counter[:2] != board.sid or tag != want:
        return None
    return sid_counter[:2], struct.unpack("<I", sid_counter[2:6])[0], inner


class Wire:
    """A byte stream read frame by frame, keeping what one read brings past the
    first frame for the next: a pseudo-terminal or a socket."""

    def __init__(self, fd=None, sock=None):
        self.fd = fd
        self.sock = sock
        self.buf = bytearray()

    def _read(self) -> bytes:
        if self.sock is not None:
            self.sock.settimeout(0.05)
            try:
                return self.sock.recv(4096)
            except socket.timeout:
                return b""
        try:
            return os.read(self.fd, 4096)
        except (BlockingIOError, OSError):
            time.sleep(0.01)
            return b""

    def drain(self) -> None:
        deadline = time.time() + 0.2
        while time.time() < deadline:
            if not self._read():
                break
        self.buf = bytearray()

    def next(self, timeout: float = 3.0, want=None):
        deadline = time.time() + timeout
        while True:
            while 0 in self.buf:
                at = self.buf.index(0)
                raw = bytes(self.buf[:at])
                del self.buf[:at + 1]
                if not raw:
                    continue
                try:
                    got = parse_frame(raw)
                except (AssertionError, IndexError, struct.error):
                    continue
                if want is None or got[0] == want:
                    return got
            if time.time() >= deadline:
                return None
            self.buf += self._read()

    def write(self, data: bytes) -> None:
        if self.sock is not None:
            self.sock.sendall(data)
        else:
            os.write(self.fd, data)


def session_on(wire: Wire, board: Board, timeout: float = 3.0, pumped: bool = False) -> bool:
    """Says a fresh hello on a wire and takes the session that answers it."""
    wire.write(board.hello(fresh=True))
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pumped:
            smoke.http("POST", "/api/v1/uplink/pump")
        got = wire.next(0.3, want=0x0012)
        if got is not None and got[2][:8] == board.uid:
            board.accept(got[2])
            return True
    return False


def command(wire: Wire, board: Board, area: int, node: int, interval: int):
    """Asks the gateway to change a board's sampling rate and answers as the
    board: the frame the gateway sent opened (session id, counter, payload), or
    None, and the HTTP status."""
    result = {}

    def ask():
        result["status"], _ = smoke.http("POST", f"/api/v1/nodes/{area}/{node}/sampling-rate",
                                         json.dumps({"interval_ms": interval}))

    t = threading.Thread(target=ask)
    t.start()
    got = wire.next(3.0, want=0x8301)
    opened = open_down(board, *got) if got is not None else None
    if opened is not None:
        wire.write(board.next(0x0302, smoke.control_response(area, node, 1, 0, interval), got[1]))
    t.join(timeout=6)
    return opened, result.get("status")


def resets_in(log: str, uid: bytes) -> int:
    return log.count("asked board " + uid.hex() + " to start a new session")


def serial_test(k_gw, env, nodes, rows, work):
    """A Python board on the serial link, every step of the exchange seen."""
    board = Board(k_gw)
    write_manifest(nodes, rows + [f"{board.uid.hex()}|7|11|board|0|0|"])
    master, slave = pty.openpty()
    os.set_blocking(master, False)
    env = dict(env, SERIAL_PORT=os.ttyname(slave))
    wire = Wire(fd=master)
    proc = start(env)
    log = ""
    try:
        wire.drain()
        check(session_on(wire, board), "a board on the serial link opens a session")
        recorded = []
        for i in range(5):
            data = board.next(0x0201, telemetry(AREA, 11, 20.0 + i))
            recorded.append(data)
            wire.write(data)
        time.sleep(0.4)
        check(smoke.node_temperature(AREA, 11) == 24.0, "its readings are taken")
        # A command, so the board has accepted a downward counter to stay above.
        opened, status = command(wire, board, AREA, 11, 900)
        last_down = opened[1] if opened is not None else 0
        check(last_down >= 1 and status == 200, f"a command for it goes down sealed ({status})")

        log += stop(proc)
        old_sid = board.sid
        restarted = time.time()
        proc = start(env)
        got = wire.next(3.0, want=SESSION_RESET | 0x8000)
        opened = open_down(board, *got) if got is not None else None
        check(opened is not None, "after the restart the gateway asks the board for a new session, sealed under the restored one")
        if opened is not None:
            check(opened[0] == old_sid and opened[1] > last_down,
                  f"its counter ({opened[1]}) is past the last the board accepted ({last_down})")
            check(opened[2] == b"\x01", "and it says why: the gateway restarted")
        # The board goes on where it was: in the restored window, refused.
        wire.write(board.next(0x0201, telemetry(AREA, 11, 30.0)))
        time.sleep(0.3)
        check(smoke.node_temperature(AREA, 11) is None, "a frame within the restored window is still refused")
        # A frame recorded before the restart is refused, and asks for nothing.
        wire.write(recorded[2])
        time.sleep(0.3)
        check(smoke.node_temperature(AREA, 11) is None, "a frame recorded before the restart is refused")
        check(wire.next(0.5, want=SESSION_RESET | 0x8000) is None,
              "and neither asks again within ten seconds")
        # The board takes the request: a new session, heard at once.
        check(session_on(wire, board), "the board's new hello is answered")
        wire.write(board.next(0x0201, telemetry(AREA, 11, 31.0)))
        time.sleep(0.3)
        heard = smoke.node_temperature(AREA, 11) == 31.0
        check(heard, f"its reading under the new session is taken ({time.time() - restarted:.1f} s after the restart)")
        wire.write(recorded[4])
        time.sleep(0.3)
        check(smoke.node_temperature(AREA, 11) == 31.0, "the frame recorded before the restart is still refused")
        opened, status = command(wire, board, AREA, 11, 800)
        check(opened is not None and opened[0] == board.sid and status == 200,
              f"a command now goes under the new session ({status})")
    finally:
        log += stop(proc)
        os.close(master)
        os.close(slave)
    check(resets_in(log, board.uid) == 1, "the request is said once in the log")
    check(" restored after restart is dropped: board " + board.uid.hex() in log,
          "the restored session is dropped once the new one carries a frame")


def relay_and_uplink_test(k_gw, env, nodes, rows, work):
    """A board behind a relay on the uplink, and a board on an uplink
    connection of its own: each asked on its first refused frame."""
    relay = Board(k_gw)
    behind = Board(k_gw)
    wifi = Board(k_gw)
    write_manifest(nodes, rows + [
        f"{relay.uid.hex()}|7|20|relay|0|0|9",
        f"{behind.uid.hex()}|9|7|board|0|0|",
        f"{wifi.uid.hex()}|7|12|board|0|0|",
    ])
    proc = start(env)
    log = ""
    socks = []

    def dial():
        s = socket.create_connection((HOST, UPLINK_PORT), timeout=5)
        socks.append(s)
        return s, Wire(sock=s)

    def relayed_session(wire: Wire, timeout: float = 3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            smoke.http("POST", "/api/v1/uplink/pump")
            got = wire.next(0.3, want=RELAYED | 0x8000)
            if got is None:
                continue
            opened = open_down(relay, *got)
            if opened is None:
                continue
            port, msg_id, tx_id = struct.unpack("<BHH", opened[2][:5])
            return port, msg_id, tx_id, opened[2][5:]
        return None

    def behind_frame(temp: float) -> bytes:
        sealed = behind.next(0x0201, telemetry(9, 7, temp))
        _, _, payload = parse_frame(sealed[:-1])
        return relay.next(RELAYED, struct.pack("<BHH", 1, 0x8201, 0) + payload)

    try:
        rs, rw = dial()
        check(session_on(rw, relay, pumped=True), "a relay on the uplink opens a session")
        rw.write(relay.next(0x0101, smoke.announce_payload(7, 2, 9)))
        pump()
        rw.write(relay.next(RELAYED, struct.pack("<BHH", 1, 0x0011, 0) + smoke_hello(behind)))
        got = relayed_session(rw)
        check(got is not None and got[1] == 0x0012, "a board behind it gets a session through it")
        if got is not None:
            behind.accept(got[3])
        rw.write(behind_frame(41.0))
        pump()
        check(smoke.node_temperature(9, 7) == 41.0, "its reading is taken")

        ws, ww = dial()
        check(session_on(ww, wifi, pumped=True), "a board on an uplink connection opens a session")
        ww.write(wifi.next(0x0201, telemetry(AREA, 12, 51.0)))
        pump()
        check(smoke.node_temperature(AREA, 12) == 51.0, "its reading is taken")

        log += stop(proc)
        for s in socks:
            s.close()
        socks.clear()
        proc = start(env)

        # The relay dials again and opens a new session of its own, as the C
        # relay does; the board behind it goes on under its restored one.
        rs, rw = dial()
        check(session_on(rw, relay, pumped=True), "after the restart the relay opens a new session")
        rw.write(relay.next(0x0101, smoke.announce_payload(7, 2, 9)))
        pump()
        rw.write(behind_frame(42.0))
        got = relayed_session(rw)
        check(smoke.node_temperature(9, 7) is None, "the board's frame within the restored window is refused")
        check(got is not None and got[1] == SESSION_RESET | 0x8000,
              "and the board is asked for a new session inside RELAYED, through the relay's new session")
        if got is not None:
            check(got[0] == 1 and open_down(behind, got[1], got[2], got[3]) is not None,
                  "on its port, sealed under the board's restored session")
        rw.write(relay.next(RELAYED, struct.pack("<BHH", 1, 0x0011, 0) + smoke_hello(behind)))
        got = relayed_session(rw)
        check(got is not None and got[1] == 0x0012, "the board's new hello through the relay is answered")
        if got is not None:
            behind.accept(got[3])
        rw.write(behind_frame(43.0))
        pump()
        check(smoke.node_temperature(9, 7) == 43.0, "and its reading under the new session is taken")

        # The board on the uplink dials again and goes on under its session.
        ws, ww = dial()
        ww.write(wifi.next(0x0201, telemetry(AREA, 12, 52.0)))
        reset = None
        deadline = time.time() + 3.0
        while reset is None and time.time() < deadline:
            smoke.http("POST", "/api/v1/uplink/pump")
            reset = ww.next(0.3, want=SESSION_RESET | 0x8000)
        check(smoke.node_temperature(AREA, 12) is None, "a board's frame on a new uplink connection is refused")
        check(reset is not None and open_down(wifi, *reset) is not None,
              "and it is asked for a new session on the connection it came on")
        check(session_on(ww, wifi, pumped=True), "its new hello is answered")
        ww.write(wifi.next(0x0201, telemetry(AREA, 12, 53.0)))
        pump()
        check(smoke.node_temperature(AREA, 12) == 53.0, "and its reading under the new session is taken")
    finally:
        for s in socks:
            s.close()
        log += stop(proc)
    check(resets_in(log, behind.uid) == 1 and resets_in(log, wifi.uid) == 1, "each request is said once in the log")


def smoke_hello(board: Board) -> bytes:
    board.nonce_b = os.urandom(8)
    return board.uid + board.nonce_b + hmac16(board.k_dev, b"rh-hello1" + board.uid + board.nonce_b)[:8]


def host_node_test(k_gw, env, nodes, rows, work):
    """The real C host node on the serial link: heard again within seconds."""
    if not os.path.exists(NODE_BIN):
        print(f"SKIP: the host node is not built ({NODE_BIN}); the C board's side is not checked")
        return
    uid = os.urandom(8)
    node_number = 30
    write_manifest(nodes, rows + [f"{uid.hex()}|7|{node_number}|board|0|0|"])
    node_key = os.path.join(work, "node.key")
    with open(node_key, "w") as f:
        f.write(hmac16(k_gw, b"rh-dev1" + uid)[:16].hex() + "\n")
    os.chmod(node_key, 0o600)
    # The gateway's serial link and the node's UART are two pseudo-terminals
    # joined here, as a cable joins them.
    gw_master, gw_slave = pty.openpty()
    node_master, node_slave = pty.openpty()
    for fd in (gw_master, node_master):
        os.set_blocking(fd, False)
    stop_bridge = threading.Event()

    def bridge():
        while not stop_bridge.is_set():
            ready, _, _ = select.select([gw_master, node_master], [], [], 0.05)
            for fd in ready:
                try:
                    data = os.read(fd, 4096)
                except (BlockingIOError, OSError):
                    continue
                other = node_master if fd == gw_master else gw_master
                try:
                    os.write(other, data)
                except OSError:
                    pass

    threading.Thread(target=bridge, daemon=True).start()
    env = dict(env, SERIAL_PORT=os.ttyname(gw_slave))
    from http.server import BaseHTTPRequestHandler, HTTPServer

    body = (b'{"inverters":[{"serial":"1161716xxxxx","name":"HMS-800","order":0,"data_age":3,'
            b'"poll_enabled":true,"reachable":true,"producing":true,"limit_relative":100,"limit_absolute":-1}],'
            b'"total":{"Power":{"v":823.5,"u":"W","d":1},"YieldDay":{"v":1750,"u":"Wh","d":0},'
            b'"YieldTotal":{"v":4321.125,"u":"kWh","d":3}}}')

    class OpenDTU(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    opendtu = HTTPServer((HOST, 0), OpenDTU)
    threading.Thread(target=opendtu.serve_forever, daemon=True).start()
    node_env = dict(os.environ)
    node_env.update({
        "RH_DEVICE": "hoymiles",
        "RH_OPENDTU_HOST": HOST,
        "RH_OPENDTU_PORT": str(opendtu.server_address[1]),
        "RH_NODE_LINK": "uart",
        "RH_NODE_UART": "uart0",
        "RHO_UART0": os.ttyname(node_slave),
        "AREA": str(AREA),
        "NODE": str(node_number),
        "INTERVAL_MS": "300",
        "RH_SECURE_UID": uid.hex(),
        "RH_SECURE_KEY_FILE": node_key,
    })
    for name in ("RH_KEYSTORE_FILE", "RH_STATE_DIR"):
        node_env.pop(name, None)

    def heard_since(since_ms: int) -> bool:
        _, text = smoke.http("GET", "/api/v1/telemetry/outbox?after=0&limit=5000")
        try:
            records = json.loads(text)
        except ValueError:
            return False
        return any(r.get("source") == uid.hex() and r.get("received_ms", 0) > since_ms for r in records or [])

    def wait(predicate, timeout: float) -> float:
        began = time.time()
        while time.time() - began < timeout:
            if predicate():
                return time.time() - began
            time.sleep(0.1)
        return -1.0

    proc = start(env)
    node = None
    log = ""
    try:
        node = subprocess.Popen([NODE_BIN], env=node_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        check(wait(lambda: heard_since(0), 8.0) >= 0, "the C host node on the serial link is heard")
        time.sleep(1.5)   # a few readings, so its counter is past what was kept
        log += stop(proc)
        stopped_ms = int(time.time() * 1000)
        proc = start(env)
        took = wait(lambda: heard_since(stopped_ms), 10.0)
        check(0 <= took <= 5.0, f"after the restart it is heard again within seconds ({took:.1f} s), not after the counter window")
    finally:
        if node is not None and node.poll() is None:
            node.terminate()
            node.wait(timeout=5)
        log += stop(proc)
        stop_bridge.set()
        opendtu.shutdown()
        for fd in (gw_master, gw_slave, node_master, node_slave):
            os.close(fd)
    check(resets_in(log, uid) >= 1, "the gateway said it asked the node for a new session")
    check(log.count("opened for board " + uid.hex()) >= 2, "and the node opened a new one")


def main():
    work = tempfile.mkdtemp(prefix="gateway-restart-")
    smoke.HTTP_PORT = HTTP_PORT
    smoke.TOKEN = smoke.mint_token(work)
    k_gw = os.urandom(32)
    key_file = os.path.join(work, "gateway.key")
    with open(key_file, "w") as f:
        f.write(k_gw.hex() + "\n")
    os.chmod(key_file, 0o600)
    nodes = os.path.join(work, "nodes.txt")
    env = dict(os.environ)
    env.pop("ALLOW_PLAIN_FRAMES", None)
    env.update({
        "SERIAL_PORT": os.path.join(work, "no-tty"),
        "REST_PORT": str(HTTP_PORT),
        "RHO_HTTP_ADDR": f"{HOST}:{HTTP_PORT}",
        "UPLINK_PORT": str(UPLINK_PORT),
        "PLOTJUGGLER_ADDR": f"{HOST}:{UDP_PORT}",
        "GATEWAY_MODE": "master",
        "GATEWAY_AREA": str(AREA),
        "HOME": work,
        "GATEWAY_KEY_FILE": key_file,
    })
    rows = []
    serial_test(k_gw, env, nodes, rows, work)
    relay_and_uplink_test(k_gw, env, nodes, rows, work)
    host_node_test(k_gw, env, nodes, rows, work)

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("\nOK: boards are heard again after a gateway restart, and nothing recorded before it is")


if __name__ == "__main__":
    main()
