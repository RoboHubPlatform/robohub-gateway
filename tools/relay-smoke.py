#!/usr/bin/env python3
"""The whole RoboHub tree on one machine, sealed end to end.

A master (robohub-gateway), the C relay (relay-host, on robohub-linux) and
boards (a pseudo-terminal) meet over the same wire:

  board  --uart1/pty-->  relay  --TCP uplink-->  master

The relay holds no key of a board's. A board says hello and seals its frames
under its own session with the master; the relay carries them whole inside
RELAYED, sealed under its own session; the master opens them with the board's
key and keeps each reading under the board's id. This checks:

- the relay opens a session with the master and announces itself;
- a board behind it gets a session, and its reading reaches the master as that
  board's, for a board in the relay's own area (an in-mesh relay) and for one
  in an area the manifest gives the relay;
- a command for each board goes down, sealed for it, through the relay, and its
  answer comes back;
- a board the relay may not speak for gets no session;
- a plain command written to the relay's USB port (its console) reaches no
  board;
- a relay cannot forge a board's reading: a frame changed after the board
  sealed it, a board's frame carried by another relay, and a plain reading
  inside RELAYED are all refused;
- a gateway in relay mode carries a board its parent planned the same way;
- after the master restarts the relay dials again, opens a new session,
  announces again, and the board is heard under the session it held.

It builds the relay when it is not built and a C toolchain is there, and says
SKIP and exits 0 when it cannot be built.

    python3 tools/relay-smoke.py
"""
import json
import os
import pty
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smoke  # noqa: E402
from smoke import Board, control_response, device, frame, hmac16, parse_frame, uplink_session, wait_ready  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# The workspace is where the sibling checkouts sit: ROBOHUB_WORKSPACE (or
# RHO_WORKSPACE) when set, otherwise the directory that holds this repository.
WORKSPACE = (
    os.environ.get("ROBOHUB_WORKSPACE")
    or os.environ.get("RHO_WORKSPACE")
    or os.path.dirname(ROOT)
)
RELAY_DIR = os.path.join(WORKSPACE, "robohub-drivers", "examples", "gateway", "c", "relay-host")
RELAY_BIN = os.environ.get("RELAY_BIN", os.path.join(RELAY_DIR, "build", "output", "relay_host"))
HOST = "127.0.0.1"
REST_PORT = 18084
UPLINK_PORT = 18085
CHILD_PORT = 18086
UDP_PORT = 19884
AREA = 7          # the master's own area, and the in-mesh relay's
RELAY_AREA = 9    # an area the manifest gives the relay

failures = []


def check(ok: bool, message: str) -> None:
    print(("ok   " if ok else "FAIL ") + message)
    if not ok:
        failures.append(message)


def telemetry(area: int, node: int, temp_c: float, ts_ms: int = 1000) -> bytes:
    return b"\x01" + device(area, node) + struct.pack("<IfI", ts_ms, temp_c, 0)


def control_request(area: int, node: int, opcode: int, argument: int) -> bytes:
    return b"\x01" + device(area, node) + struct.pack("<HI", opcode, argument)


def relayed(port: int, msg_id: int, tx_id: int, payload: bytes) -> bytes:
    return struct.pack("<BHH", port, msg_id, tx_id) + payload


class Wire:
    """One end of a pseudo-terminal, read frame by frame: what one read brings
    past the first frame is kept for the next."""

    def __init__(self, fd: int):
        self.fd = fd
        self.buf = bytearray()

    def drain(self) -> None:
        self.buf = bytearray()
        while True:
            try:
                if not os.read(self.fd, 4096):
                    return
            except (BlockingIOError, OSError):
                return

    def next(self, timeout: float = 3.0):
        deadline = time.time() + timeout
        while True:
            if 0 in self.buf:
                at = self.buf.index(0)
                raw = bytes(self.buf[:at])
                del self.buf[:at + 1]
                if raw:
                    try:
                        return parse_frame(raw)
                    except (AssertionError, IndexError, struct.error):
                        # A torn frame: a reader of a byte stream drops it.
                        continue
                continue
            if time.time() >= deadline:
                return None
            try:
                self.buf += os.read(self.fd, 4096)
            except (BlockingIOError, OSError):
                time.sleep(0.01)

    def write(self, data: bytes) -> None:
        os.write(self.fd, data)


def board_session(wire: Wire, board: Board, timeout: float = 3.0) -> bool:
    """Says a board's hello on its link and takes the session that comes back."""
    wire.write(board.hello(fresh=True))
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = wire.next(max(0.05, deadline - time.time()))
        if got is None:
            return False
        msg_id, _, payload = got
        if msg_id == 0x0012 and payload[:8] == board.uid:
            board.accept(payload)
            return True
    return False


def node_temperature(area: int, node: int):
    smoke.http("POST", "/api/v1/uplink/pump")
    return smoke.node_temperature(area, node)


def children() -> int:
    try:
        _, body = smoke.http("GET", "/api/v1/diagnostics")
        return json.loads(body).get("children", -1)
    except (ValueError, OSError):
        return -1


def outbox() -> str:
    _, body = smoke.http("GET", "/api/v1/telemetry/outbox?limit=500")
    return body


def command(wire: Wire, board: Board, area: int, node: int, interval: int):
    """Sends a sampling-rate command for a board through the master; the board
    opens it, checks it, and answers. Returns (status, body, what it heard)."""
    seen = {}

    def responder():
        got = wire.next(4.0)
        if got is None:
            seen["error"] = "nothing"
            return
        msg_id, tx_id, payload = got
        try:
            inner = board.open(msg_id, tx_id, payload)
        except AssertionError as exc:
            seen["error"] = str(exc)
            return
        seen["msg_id"] = msg_id & 0x7FFF
        seen["inner"] = inner
        a, n = struct.unpack("<HH", inner[2:6])
        opcode, argument = struct.unpack("<HI", inner[6:12])
        wire.write(board.next(0x0302, control_response(a, n, opcode, 0, argument - 20), tx_id))

    t = threading.Thread(target=responder)
    t.start()
    status, body = smoke.http("POST", f"/api/v1/nodes/{area}/{node}/sampling-rate", json.dumps({"interval_ms": interval}))
    t.join(timeout=6)
    return status, body, seen


def build_relay() -> bool:
    """Builds relay-host when it is not built. The build is the native one, so
    it needs a C compiler and CMake; without them the smoke is skipped."""
    if os.path.exists(RELAY_BIN):
        return True
    if not os.path.isdir(RELAY_DIR) or not shutil.which("cmake") or not (shutil.which("cc") or shutil.which("gcc")):
        return False
    rho = smoke.RHO if os.path.exists(smoke.RHO) else "rho"
    lock = os.path.join(RELAY_DIR, "rho.lock")
    kept = open(lock, "rb").read() if os.path.exists(lock) else None
    try:
        done = subprocess.run([rho, "build", "-target", "native", "."], cwd=RELAY_DIR, capture_output=True, text=True)
    finally:
        # The build records what it resolved; the checkout's lock stays as it was.
        if kept is not None:
            with open(lock, "wb") as f:
                f.write(kept)
    if done.returncode != 0:
        print(done.stdout[-2000:] + done.stderr[-2000:])
    return done.returncode == 0 and os.path.exists(RELAY_BIN)


def write_secret(path: str, text: str) -> None:
    with open(path, "w") as f:
        f.write(text + "\n")
    os.chmod(path, 0o600)


def start_master(env):
    proc = subprocess.Popen([smoke.BIN], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    wait_ready(proc)
    return proc


def stop(proc) -> str:
    if proc is None or proc.poll() is not None:
        return ""
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    return out or ""


class Pump:
    """Drains the master's uplink every tenth of a second, as a supervisor does:
    a gateway built with a gateway port whose serial read parks on a quiet link
    drains its uplink only when it is asked (POST /api/v1/uplink/pump). A gateway
    in relay mode has no such route, so its serial link is given an empty frame
    (a lone delimiter, which a reader skips) as often, which turns its loop."""

    def __init__(self):
        self.running = True
        self.links = []
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while self.running:
            try:
                smoke.http("POST", "/api/v1/uplink/pump")
            except OSError:
                pass
            for fd in list(self.links):
                try:
                    os.write(fd, b"\x00")
                except OSError:
                    pass
            time.sleep(0.1)

    def stop(self):
        self.running = False
        self.thread.join(timeout=2)


def wait_for(predicate, timeout: float = 6.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def hello_payload(board: Board) -> bytes:
    board.nonce_b = os.urandom(8)
    tag = hmac16(board.k_dev, b"rh-hello1" + board.uid + board.nonce_b)[:8]
    return board.uid + board.nonce_b + tag


def sealed_payload(board: Board, msg_id: int, inner: bytes):
    """A board's sealed frame as (flagged id, payload), to be carried."""
    board.counter += 1
    flagged = msg_id | 0x8000
    sid_counter = board.sid + struct.pack("<I", board.counter)
    tag = hmac16(board.k_up, struct.pack("<HH", flagged, 0) + sid_counter + inner)[:8]
    return flagged, sid_counter + inner + tag


def relayed_answer(conn, relay_board: Board, timeout: float = 3.0):
    """The board's SESSION payload the master sent inside RELAYED to a relay."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            msg_id, tx_id, payload = parse_frame(smoke.read_socket_frame(conn, max(0.1, deadline - time.time())))
        except (TimeoutError, socket.timeout, OSError):
            return None
        if msg_id != (0x0013 | 0x8000):
            continue
        inner = relay_board.open(msg_id, tx_id, payload)
        port, inner_id, _ = struct.unpack("<BHH", inner[:5])
        if port == 1 and inner_id == 0x0012:
            return inner[5:]
    return None


def main():
    if not os.path.exists(smoke.BIN):
        print(f"SKIP: the gateway is not built ({smoke.BIN})")
        return
    if not build_relay():
        print("SKIP: the C relay (relay-host) is not built and cannot be built here")
        return

    work = tempfile.mkdtemp(prefix="relay-smoke-")
    smoke.HTTP_PORT = REST_PORT
    smoke.TOKEN = smoke.mint_token(work)
    k_gw = os.urandom(32)
    key_file = os.path.join(work, "gateway.key")
    write_secret(key_file, k_gw.hex())

    relay = Board(k_gw)      # the C relay: in the master's area, and given area 9
    near = Board(k_gw)       # behind the relay, in the relay's own area: 7/3
    far = Board(k_gw)        # behind the relay, in area 9: 9/5
    stray = Board(k_gw)      # listed, in area 11, which the relay may not speak for
    rogue = Board(k_gw)      # another relay, which tries to forge
    honest = Board(k_gw)     # a board behind the rogue relay: 9/8
    child = Board(k_gw)      # a gateway in relay mode for area 12
    planned = Board(k_gw)    # a board the master planned, on the child's link: 12/7
    write_secret(os.path.join(work, "relay.key"), relay.k_dev.hex())
    write_secret(os.path.join(work, "nodes.txt"), "\n".join([
        "# uid|area|node|role|epoch|revoked|areas",
        f"{relay.uid.hex()}|{AREA}|20|relay|0|0|{RELAY_AREA}",
        f"{near.uid.hex()}|{AREA}|3|board|0|0|",
        f"{far.uid.hex()}|{RELAY_AREA}|5|board|0|0|",
        f"{stray.uid.hex()}|11|1|board|0|0|",
        f"{rogue.uid.hex()}|{AREA}|21|relay|0|0|{RELAY_AREA}",
        f"{honest.uid.hex()}|{RELAY_AREA}|8|board|0|0|",
        f"{child.uid.hex()}|12|0|relay|0|0|12",
        f"{planned.uid.hex()}|12|7|board|0|0|",
    ]))

    # The master's own serial: nothing writes to it, so the relay is the only
    # way its boards reach it.
    _gw_master, gw_slave = pty.openpty()
    env = dict(os.environ)
    env.pop("ALLOW_PLAIN_FRAMES", None)
    env.update({
        "SERIAL_PORT": os.ttyname(gw_slave),
        "REST_PORT": str(REST_PORT),
        "RHO_HTTP_ADDR": f"{HOST}:{REST_PORT}",
        "UPLINK_PORT": str(UPLINK_PORT),
        "PLOTJUGGLER_ADDR": f"{HOST}:{UDP_PORT}",
        "GATEWAY_MODE": "master",
        "GATEWAY_AREA": str(AREA),
        "GATEWAY_KEY_FILE": key_file,
        "OUTBOX_PATH": os.path.join(work, "outbox.jsonl"),
        "HOME": work,
    })
    master = start_master(env)
    relay_proc = None
    child_proc = None
    log = ""

    # The boards' link: the relay opens the slave, this is the boards' end.
    board_fd, board_slave = pty.openpty()
    os.set_blocking(board_fd, False)
    boards = Wire(board_fd)
    # The relay's console — a board's USB CDC — is a pty too, so a frame can be
    # written to it.
    usb_fd, usb_slave = pty.openpty()
    relay_log = open(os.path.join(work, "relay.log"), "w")
    relay_env = dict(os.environ)
    relay_env.update({
        "RHO_UART1": os.ttyname(board_slave),
        "RHO_PARENT_HOST": HOST,
        "RHO_PARENT_PORT": str(UPLINK_PORT),
        "RH_SECURE_UID": relay.uid.hex(),
        "RH_SECURE_KEY_FILE": os.path.join(work, "relay.key"),
    })

    pump = Pump()
    try:
        relay_proc = subprocess.Popen([RELAY_BIN], env=relay_env, stdin=usb_slave, stdout=relay_log,
                                      stderr=subprocess.STDOUT)

        # --- The relay's own session ----------------------------------------
        check(wait_for(lambda: children() >= 1), "the relay opens a session with the master and announces itself")

        # --- A board in the relay's own area --------------------------------
        boards.drain()
        check(board_session(boards, near), "a board behind the relay gets a session from the master, through it")
        boards.write(near.next(0x0201, telemetry(AREA, 3, 21.5)))
        check(wait_for(lambda: node_temperature(AREA, 3) == 21.5),
              "its sealed reading reaches the master (a board in the in-mesh relay's own area)")
        kept = outbox()
        check(near.uid.hex() in kept, "the reading is kept under the board's id")
        check(relay.uid.hex() not in kept, "nothing is kept under the relay's id")

        status, body, seen = command(boards, near, AREA, 3, 300)
        check(seen.get("msg_id") == 0x0301 and seen.get("inner", b"")[2:6] == struct.pack("<HH", AREA, 3),
              f"a command for the board reaches it, sealed under its own session ({seen.get('error', 'ok')})")
        check(status == 200 and json.loads(body).get("actual") == 280,
              f"the board's sealed answer comes back through the relay ({status} {body})")

        # --- A board in an area the manifest gives the relay ----------------
        boards.drain()
        check(board_session(boards, far), "a board in one of the relay's areas gets a session")
        boards.write(far.next(0x0201, telemetry(RELAY_AREA, 5, 22.5)))
        check(wait_for(lambda: node_temperature(RELAY_AREA, 5) == 22.5), "its reading reaches the master")
        status, body, seen = command(boards, far, RELAY_AREA, 5, 500)
        check(status == 200 and seen.get("msg_id") == 0x0301,
              f"a command for it goes to its own session, not to the area's announce ({status} {body})")

        # --- A board the relay may not speak for -----------------------------
        boards.drain()
        check(not board_session(boards, stray, 1.5), "a board in an area the relay may not speak for gets no session")

        # --- A plain command on the relay's USB port -------------------------
        boards.drain()
        os.write(usb_fd, frame(0x0301, 77, control_request(AREA, 3, 1, 100)))
        os.write(usb_fd, frame(0x0013, 0, relayed(1, 0x0301, 78, control_request(AREA, 3, 1, 100))))
        check(boards.next(1.5) is None, "a plain command written to the relay's USB port reaches no board")

        # --- A relay that forges ---------------------------------------------
        conn = socket.create_connection((HOST, UPLINK_PORT), timeout=5)
        check(uplink_session(conn, rogue) is not None, "another relay opens a session")
        conn.sendall(rogue.next(0x0013, relayed(1, 0x0011, 0, hello_payload(honest))))
        smoke.http("POST", "/api/v1/uplink/pump")
        answer = relayed_answer(conn, rogue)
        check(answer is not None, "a board behind that relay gets a session, inside RELAYED")
        if answer is not None:
            honest.accept(answer)
        sealed = sealed_payload(honest, 0x0201, telemetry(RELAY_AREA, 8, 30.0))
        tampered = bytearray(sealed[1])
        tampered[6 + 11] ^= 0x01   # a byte of the temperature, after the board sealed it
        conn.sendall(rogue.next(0x0013, relayed(1, sealed[0], 0, bytes(tampered))))
        check(not wait_for(lambda: node_temperature(RELAY_AREA, 8) is not None, 1.0),
              "a reading the relay changed after the board sealed it is refused")
        stolen = sealed_payload(near, 0x0201, telemetry(AREA, 3, 66.0))
        conn.sendall(rogue.next(0x0013, relayed(1, stolen[0], 0, stolen[1])))
        check(not wait_for(lambda: node_temperature(AREA, 3) == 66.0, 1.0),
              "a board's frame carried by a relay it did not open its session through is refused")
        conn.sendall(rogue.next(0x0013, relayed(1, 0x0201, 0, telemetry(RELAY_AREA, 9, 67.0))))
        check(not wait_for(lambda: node_temperature(RELAY_AREA, 9) is not None, 1.0),
              "a plain reading inside RELAYED is refused")
        conn.sendall(rogue.next(0x0013, relayed(1, sealed[0], 0, sealed[1])))
        check(wait_for(lambda: node_temperature(RELAY_AREA, 8) == 30.0, 2.0),
              "the same reading as the board sealed it is taken")
        conn.close()

        # --- A gateway in relay mode, carrying a board its parent planned ----
        child_fd, child_slave = pty.openpty()
        os.set_blocking(child_fd, False)
        child_wire = Wire(child_fd)
        pump.links.append(child_fd)
        child_dir = os.path.join(work, "child")
        os.makedirs(child_dir, exist_ok=True)
        write_secret(os.path.join(child_dir, "gateway.key"), os.urandom(32).hex())
        write_secret(os.path.join(child_dir, "nodes.txt"), "# this gateway keys no board of its own")
        write_secret(os.path.join(work, "child-relay.key"), child.k_dev.hex())
        child_env = dict(env)
        child_env.pop("UPLINK_PORT", None)
        child_env.pop("OUTBOX_PATH", None)
        child_env.update({
            "SERIAL_PORT": os.ttyname(child_slave),
            "REST_PORT": str(CHILD_PORT),
            "RHO_HTTP_ADDR": f"{HOST}:{CHILD_PORT}",
            "PLOTJUGGLER_ADDR": f"{HOST}:{UDP_PORT + 1}",
            "GATEWAY_MODE": "relay",
            "GATEWAY_AREA": "12",
            "GATEWAY_KEY_FILE": os.path.join(child_dir, "gateway.key"),
            "PARENT_UPLINK": f"{HOST}:{UPLINK_PORT}",
            "RELAY_UID": child.uid.hex(),
            "RELAY_KEY_FILE": os.path.join(work, "child-relay.key"),
        })
        before = children()
        child_proc = subprocess.Popen([smoke.BIN], env=child_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True)
        check(wait_for(lambda: children() > before), "a gateway in relay mode opens a session and announces")
        check(board_session(child_wire, planned), "a board the master planned gets a session through the relay gateway")
        child_wire.write(planned.next(0x0201, telemetry(12, 7, 23.5)))
        check(wait_for(lambda: node_temperature(12, 7) == 23.5),
              "its reading reaches the master, opened with the board's own key")
        check(planned.uid.hex() in outbox(), "and is kept under the board's id")
        status, body, seen = command(child_wire, planned, 12, 7, 700)
        check(status == 200 and seen.get("msg_id") == 0x0301,
              f"a command for it goes down through the relay gateway and is answered ({status} {body})")
        pump.links.remove(child_fd)
        log += stop(child_proc)
        child_proc = None

        # --- The master restarts ---------------------------------------------
        log += stop(master)
        master = start_master(env)
        check(wait_for(lambda: children() >= 1, 10.0),
              "after the master restarts, the relay dials again, opens a session and announces")
        near.counter += 1000
        boards.write(near.next(0x0201, telemetry(AREA, 3, 24.5)))
        check(wait_for(lambda: node_temperature(AREA, 3) == 24.5),
              "the board is heard again under the session it held, through the relay's new session")
        status, body, seen = command(boards, near, AREA, 3, 900)
        check(status == 200, f"and a command reaches it after the restart ({status} {body})")
    finally:
        pump.stop()
        log += stop(master)
        log += stop(child_proc)
        if relay_proc is not None and relay_proc.poll() is None:
            relay_proc.terminate()
            try:
                relay_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                relay_proc.kill()
        relay_log.close()

    if failures:
        if os.environ.get("RELAY_DEBUG"):
            print("--- relay ---\n" + open(os.path.join(work, "relay.log"), errors="replace").read()[-3000:])
            print("--- gateway ---\n" + "\n".join(line for line in log.splitlines() if "robohub-gateway:" in line)[-6000:])
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("\nOK: the tree works sealed end to end — a board behind a relay is its own")


if __name__ == "__main__":
    main()
