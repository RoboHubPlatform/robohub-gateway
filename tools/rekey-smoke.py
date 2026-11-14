#!/usr/bin/env python3
"""Key epochs and their rotation (REKEY), driven end to end.

A gateway runs with a node manifest beside its key whose epoch field names the
key epoch each board should be at (gateway/keys.rho). Simulated boards — the
board half of robohub-transport's secure.h, written here in Python — and, when
it is built, the real C host node (robohub-devices' gateway_node, which uses
robohub-transport itself) check that:

- a hello under the manifest's epoch is answered, and one under the epoch
  before it (the grace epoch) is too while the board has not confirmed;
- a board in its grace epoch is sent REKEY, sealed under its session, carrying
  the new key wrapped under the old one; its REKEY_ACK with a good proof makes
  the new epoch its confirmed one, said in rekey.txt and GET /api/v1/keys;
- after that a hello under the old epoch is refused, a session another holder
  of the old key opened is dropped, the board's own session goes on, and the
  confirmation outlives a restart;
- a board flashed with its current key opens a session with no REKEY, and that
  hello confirms the epoch; one two epochs behind is refused;
- a board that cannot store a key answers "cannot rotate", which is said and
  kept, it stays in its grace epoch, and it is not asked again;
- a proof that does not check confirms nothing;
- a Linux host node rewrites its key file (`<epoch>:<hex>`, 0600) when rotated,
  and says its next hello with the new key.

Run it after `rho build`:

    python3 tools/rekey-smoke.py
"""
import json
import os
import pty
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smoke  # noqa: E402
from smoke import Board, device, hmac16, parse_frame, wait_ready  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORKSPACE = os.environ.get("ROBOHUB_WORKSPACE") or os.environ.get("RHO_WORKSPACE") or os.path.dirname(ROOT)
NODE_BIN = os.environ.get("NODE_BIN", os.path.join(WORKSPACE, "robohub-devices", "nodes", "gateway-node", "build",
                                                   "output", "gateway_node"))
HOST = "127.0.0.1"
HTTP_PORT = 18098
UPLINK_PORT = 18099
UDP_PORT = 19898
AREA = 7

failures = []


def check(ok: bool, message: str) -> None:
    print(("ok   " if ok else "FAIL ") + message)
    if not ok:
        failures.append(message)


def epoch_key(k_gw: bytes, uid: bytes, epoch: int) -> bytes:
    if epoch == 0:
        return hmac16(k_gw, b"rh-dev1" + uid)[:16]
    return hmac16(k_gw, b"rh-dev2" + uid + struct.pack("<I", epoch))[:16]


class EpochBoard(Board):
    """A board holding its key at an epoch, which answers REKEY as secure.c does."""

    def __init__(self, k_gw: bytes, epoch: int = 0, store: bool = True):
        super().__init__(k_gw)
        self.k_gw = k_gw
        self.epoch = epoch
        self.k_dev = epoch_key(k_gw, self.uid, epoch)
        self.store = store
        self.pending_key = None

    def hello(self, fresh: bool = True) -> bytes:
        # The key a REKEY gave is the one from the next hello on.
        if self.pending_key is not None:
            self.epoch, self.k_dev = self.pending_key
            self.pending_key = None
        return super().hello(fresh)

    def rekey(self, msg_id: int, tx_id: int, sealed: bytes, good_proof: bool = True) -> bytes:
        """Opens a REKEY, keeps the key and answers REKEY_ACK sealed under the
        same session; with no store it answers epoch 0 and a zero proof."""
        inner = self.open(msg_id, tx_id, sealed)
        assert len(inner) == 20, "a REKEY is 20 bytes"
        new_epoch = struct.unpack("<I", inner[:4])[0]
        mask = hmac16(self.k_dev, b"rh-wrap1" + sealed[:6])[:16]
        key = bytes(a ^ b for a, b in zip(inner[4:], mask))
        self.unwrapped = (new_epoch, key)
        if not self.store:
            return self.next_frame(0x0015, b"\x00" * 12, tx_id)
        proof = hmac16(key, b"rh-rekey-ok" + self.uid + struct.pack("<I", new_epoch))[:8]
        if not good_proof:
            return self.next_frame(0x0015, struct.pack("<I", new_epoch) + bytes(7) + b"\x01", tx_id)
        self.pending_key = (new_epoch, key)
        return self.next_frame(0x0015, struct.pack("<I", new_epoch) + proof, tx_id)

    def next_frame(self, msg_id: int, inner: bytes, tx_id: int = 0) -> bytes:
        return self.next(msg_id, inner, tx_id)


class Wire:
    """A byte stream — the serial pty or an uplink socket — read frame by frame."""

    def __init__(self, read):
        self.read = read
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
            chunk = self.read()
            if chunk:
                self.buf += chunk
            else:
                time.sleep(0.02)


def pty_reader(fd: int):
    def read():
        try:
            return os.read(fd, 4096)
        except (BlockingIOError, OSError):
            return b""
    return read


def sock_reader(sock):
    def read():
        try:
            return sock.recv(4096)
        except (socket.timeout, BlockingIOError):
            return b""
        except OSError:
            return b""
    return read


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


def keys_report(work: str):
    """rekey.txt as {uid: (epoch, status)}."""
    out = {}
    try:
        for line in open(os.path.join(work, "rekey.txt")):
            if line.startswith("#") or not line.strip():
                continue
            uid, epoch, status, _ = line.strip().split("|")
            out[uid] = (int(epoch), status)
    except OSError:
        pass
    return out


def keys_api():
    status, body = smoke.http("GET", "/api/v1/keys")
    try:
        return status, {k["uid"]: k for k in json.loads(body)}
    except ValueError:
        return status, {}


def serial_session(master: int, wire: Wire, board: EpochBoard, timeout: float = 1.5):
    os.write(master, board.hello())
    got = wire.next(timeout, want=0x0012)
    if got is None or got[2][:8] != board.uid:
        return False
    board.accept(got[2])
    return True


def uplink_session(sock, wire: Wire, board: EpochBoard, timeout: float = 2.0) -> bool:
    sock.sendall(board.hello())
    deadline = time.time() + timeout
    while time.time() < deadline:
        smoke.http("POST", "/api/v1/uplink/pump")
        got = wire.next(0.2, want=0x0012)
        if got is not None and got[2][:8] == board.uid:
            board.accept(got[2])
            return True
    return False


def start(env):
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


def main():
    if not os.path.exists(smoke.BIN):
        print(f"SKIP: the gateway is not built ({smoke.BIN})")
        return
    work = tempfile.mkdtemp(prefix="gateway-rekey-")
    smoke.HTTP_PORT = HTTP_PORT
    smoke.TOKEN = smoke.mint_token(work)
    k_gw = os.urandom(32)
    key_file = os.path.join(work, "gateway.key")
    with open(key_file, "w") as f:
        f.write(k_gw.hex() + "\n")
    os.chmod(key_file, 0o600)
    nodes = os.path.join(work, "nodes.txt")

    rotated = EpochBoard(k_gw, 0)           # serial, 7/11, manifest epoch 1: rotated with REKEY
    fresh = EpochBoard(k_gw, 2)             # uplink, 7/12, manifest epoch 2, flashed at 2
    behind = EpochBoard(k_gw, 0)            # uplink, 7/13, manifest epoch 2, flashed at 0: refused
    nostore = EpochBoard(k_gw, 0, False)    # uplink, 7/14, manifest epoch 1, cannot store a key
    liar = EpochBoard(k_gw, 0)              # uplink, 7/15, manifest epoch 1, answers a bad proof
    rows = [
        f"{rotated.uid.hex()}|7|11|board|1|0|",
        f"{fresh.uid.hex()}|7|12|board|2|0|",
        f"{behind.uid.hex()}|7|13|board|2|0|",
        f"{nostore.uid.hex()}|7|14|board|1|0|",
        f"{liar.uid.hex()}|7|15|board|1|0|",
    ]
    write_manifest(nodes, rows)

    master, slave = pty.openpty()
    os.set_blocking(master, False)
    serial = Wire(pty_reader(master))
    env = dict(os.environ)
    env.pop("ALLOW_PLAIN_FRAMES", None)
    env.update({
        "SERIAL_PORT": os.ttyname(slave),
        "REST_PORT": str(HTTP_PORT),
        "RHO_HTTP_ADDR": f"{HOST}:{HTTP_PORT}",
        "UPLINK_PORT": str(UPLINK_PORT),
        "PLOTJUGGLER_ADDR": f"{HOST}:{UDP_PORT}",
        "GATEWAY_MODE": "master",
        "GATEWAY_AREA": str(AREA),
        "HOME": work,
        "GATEWAY_KEY_FILE": key_file,
    })
    proc = start(env)
    log = ""
    try:
        # --- The grace epoch, and a REKEY on the serial link ----------------
        check(serial_session(master, serial, rotated), "a hello under the grace epoch (manifest 1, board 0) is answered")
        os.write(master, rotated.next(0x0201, telemetry(AREA, 11, 21.0)))
        got = serial.next(3.0, want=0x8014)
        check(got is not None, "the board in its grace epoch is sent REKEY, sealed under its session")
        if got is not None:
            ack = rotated.rekey(*got)
            check(rotated.unwrapped == (1, epoch_key(k_gw, rotated.uid, 1)),
                  "the REKEY unwraps to the board's epoch-1 key")
            check(keys_report(work).get(rotated.uid.hex()) is None, "nothing is confirmed before the board answers")
            os.write(master, ack)
            time.sleep(0.4)
        check(keys_report(work).get(rotated.uid.hex()) == (1, "confirmed"),
              "a good REKEY_ACK confirms epoch 1 in rekey.txt")
        status, listed = keys_api()
        entry = listed.get(rotated.uid.hex(), {})
        check(status == 200 and entry.get("confirmed") == 1 and entry.get("manifest_epoch") == 1,
              f"GET /api/v1/keys says so ({status} {entry})")
        os.write(master, rotated.next(0x0201, telemetry(AREA, 11, 22.0)))
        time.sleep(0.3)
        check(smoke.node_temperature(AREA, 11) == 22.0, "the board's session goes on under the old key after the REKEY")
        check(serial.next(0.8, want=0x8014) is None, "a board that confirmed is not sent REKEY again")

        # After confirmation the old epoch opens nothing.
        stale = EpochBoard(k_gw, 0)
        stale.uid = rotated.uid
        stale.k_dev = epoch_key(k_gw, rotated.uid, 0)
        check(not serial_session(master, serial, stale, 1.0), "after confirmation a hello under the old epoch is refused")
        check(serial_session(master, serial, rotated), "the board's next hello, under epoch 1, is answered")
        os.write(master, rotated.next(0x0201, telemetry(AREA, 11, 23.0)))
        time.sleep(0.3)
        check(smoke.node_temperature(AREA, 11) == 23.0 and serial.next(0.5, want=0x8014) is None,
              "and its frames are taken with no REKEY")

        # --- A board flashed with its current key, and one too far behind ----
        conn = socket.create_connection((HOST, UPLINK_PORT), timeout=5)
        conn.settimeout(0.2)
        wire = Wire(sock_reader(conn))
        check(uplink_session(conn, wire, fresh), "a board flashed with the manifest's epoch (2) is answered")
        conn.sendall(fresh.next(0x0201, telemetry(AREA, 12, 31.0)))
        smoke.http("POST", "/api/v1/uplink/pump")
        check(wire.next(0.8, want=0x8014) is None, "and is sent no REKEY")
        check(keys_report(work).get(fresh.uid.hex()) == (2, "confirmed"), "its hello confirms epoch 2")
        conn2 = socket.create_connection((HOST, UPLINK_PORT), timeout=5)
        conn2.settimeout(0.2)
        check(not uplink_session(conn2, Wire(sock_reader(conn2)), behind, 1.0),
              "a board two epochs behind the manifest is refused")
        conn2.close()

        # --- A board that cannot store a key --------------------------------
        conn3 = socket.create_connection((HOST, UPLINK_PORT), timeout=5)
        conn3.settimeout(0.2)
        wire3 = Wire(sock_reader(conn3))
        check(uplink_session(conn3, wire3, nostore), "a board with no key store opens a session in its grace epoch")
        conn3.sendall(nostore.next(0x0201, telemetry(AREA, 14, 41.0)))
        smoke.http("POST", "/api/v1/uplink/pump")
        got = wire3.next(2.0, want=0x8014)
        check(got is not None, "it is sent REKEY")
        if got is not None:
            conn3.sendall(nostore.rekey(*got))
            smoke.http("POST", "/api/v1/uplink/pump")
            time.sleep(0.3)
        check(keys_report(work).get(nostore.uid.hex()) == (1, "cannot"), "its answer is kept as cannot rotate")
        conn3.sendall(nostore.next(0x0201, telemetry(AREA, 14, 42.0)))
        smoke.http("POST", "/api/v1/uplink/pump")
        check(wire3.next(0.8, want=0x8014) is None, "it is not asked again in that session")
        check(uplink_session(conn3, wire3, nostore), "its next hello, still under the old key, is answered")
        conn3.sendall(nostore.next(0x0201, telemetry(AREA, 14, 43.0)))
        smoke.http("POST", "/api/v1/uplink/pump")
        check(wire3.next(0.8, want=0x8014) is None, "nor in its next session")
        check(smoke.node_temperature(AREA, 14) == 43.0, "and it is heard")
        conn3.close()

        # --- A proof that does not check -----------------------------------
        conn4 = socket.create_connection((HOST, UPLINK_PORT), timeout=5)
        conn4.settimeout(0.2)
        wire4 = Wire(sock_reader(conn4))
        check(uplink_session(conn4, wire4, liar), "another board opens a session in its grace epoch")
        conn4.sendall(liar.next(0x0201, telemetry(AREA, 15, 51.0)))
        smoke.http("POST", "/api/v1/uplink/pump")
        got = wire4.next(2.0, want=0x8014)
        if got is not None:
            conn4.sendall(liar.rekey(*got, good_proof=False))
            smoke.http("POST", "/api/v1/uplink/pump")
            time.sleep(0.3)
        check(got is not None and keys_report(work).get(liar.uid.hex()) is None,
              "a REKEY_ACK whose proof does not check confirms nothing")
        conn4.close()

        # --- A restart -------------------------------------------------------
        log += stop(proc)
        proc = start(env)
        check(keys_report(work).get(rotated.uid.hex()) == (1, "confirmed"), "the confirmation outlives a restart")
        check(not serial_session(master, serial, stale, 1.0), "and after the restart the old epoch is still refused")
        conn.close()
    finally:
        log += stop(proc)

    check("is sent its key for epoch 1" in log and "is at epoch 1: it stored the new key" in log,
          "the gateway says what it sent and what was confirmed")
    check("cannot rotate its key" in log, "the gateway says a board cannot rotate")

    thief_test(k_gw, env, nodes, rows, work)
    host_node_test(k_gw, env, nodes, rows, work)

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("\nOK: keys rotate, and an old key stops opening sessions once the new one is confirmed")


def thief_test(k_gw, env, nodes, rows, work):
    """A second holder of a board's old key loses its session when the board
    confirms the new key."""
    board = EpochBoard(k_gw, 0)
    rows = rows + [f"{board.uid.hex()}|7|16|board|1|0|"]
    write_manifest(nodes, rows)
    proc = start(env)
    try:
        thief = EpochBoard(k_gw, 0)
        thief.uid = board.uid
        thief.k_dev = epoch_key(k_gw, board.uid, 0)
        a = socket.create_connection((HOST, UPLINK_PORT), timeout=5)
        b = socket.create_connection((HOST, UPLINK_PORT), timeout=5)
        a.settimeout(0.2)
        b.settimeout(0.2)
        wa, wb = Wire(sock_reader(a)), Wire(sock_reader(b))
        check(uplink_session(b, wb, thief), "a thief holding the old key opens a session before the rotation")
        check(uplink_session(a, wa, board), "the board opens its own")
        a.sendall(board.next(0x0201, telemetry(AREA, 16, 61.0)))
        smoke.http("POST", "/api/v1/uplink/pump")
        got = wa.next(2.0, want=0x8014)
        if got is not None:
            a.sendall(board.rekey(*got))
            smoke.http("POST", "/api/v1/uplink/pump")
            time.sleep(0.3)
        check(keys_report(work).get(board.uid.hex()) == (1, "confirmed"), "the board confirms epoch 1")
        b.sendall(thief.next(0x0201, telemetry(AREA, 16, 66.0)))
        smoke.http("POST", "/api/v1/uplink/pump")
        time.sleep(0.2)
        check(smoke.node_temperature(AREA, 16) == 61.0, "the thief's session, under the old key, is dropped")
        a.sendall(board.next(0x0201, telemetry(AREA, 16, 62.0)))
        smoke.http("POST", "/api/v1/uplink/pump")
        time.sleep(0.2)
        check(smoke.node_temperature(AREA, 16) == 62.0, "the board's own is not")
        a.close()
        b.close()
    finally:
        out = stop(proc)
    check("dropped: it was opened under an older key" in out, "the dropped session is said")


OPENDTU_BODY = (
    b'{"inverters":[{"serial":"1161716xxxxx","name":"HMS-800","order":0,"data_age":3,'
    b'"poll_enabled":true,"reachable":true,"producing":true,"limit_relative":100,"limit_absolute":-1}],'
    b'"total":{"Power":{"v":823.5,"u":"W","d":1},"YieldDay":{"v":1750,"u":"Wh","d":0},'
    b'"YieldTotal":{"v":4321.125,"u":"kWh","d":3}},'
    b'"hints":{"time_sync":false,"radio_problem":false,"default_password":false}}'
)


class OpenDTU(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(OPENDTU_BODY)))
        self.end_headers()
        self.wfile.write(OPENDTU_BODY)

    def log_message(self, *args):
        pass


def host_node_test(k_gw, env, nodes, rows, work):
    """The C host node, rotated: its key file is rewritten and its next hello is
    under the new key."""
    if not os.path.exists(NODE_BIN):
        print(f"SKIP: the host node is not built ({NODE_BIN}); its key file rotation is not checked")
        return
    uid = os.urandom(8)
    node_number = 30
    write_manifest(nodes, rows + [f"{uid.hex()}|7|{node_number}|board|1|0|"])
    node_key = os.path.join(work, "node.key")
    with open(node_key, "w") as f:
        f.write(epoch_key(k_gw, uid, 0).hex() + "\n")   # the plain format: epoch 0
    os.chmod(node_key, 0o600)
    opendtu = HTTPServer((HOST, 0), OpenDTU)
    threading.Thread(target=opendtu.serve_forever, daemon=True).start()
    node_env = dict(os.environ)
    node_env.update({
        "RH_DEVICE": "hoymiles",
        "RH_OPENDTU_HOST": HOST,
        "RH_OPENDTU_PORT": str(opendtu.server_address[1]),
        "RH_NODE_LINK": "tcp",
        "RH_GATEWAY_HOST": HOST,
        "RH_GATEWAY_PORT": str(UPLINK_PORT),
        "AREA": str(AREA),
        "NODE": str(node_number),
        "INTERVAL_MS": "300",
        "RH_SECURE_UID": uid.hex(),
        "RH_SECURE_KEY_FILE": node_key,
    })
    for name in ("RH_KEYSTORE_FILE", "RH_STATE_DIR"):
        node_env.pop(name, None)
    proc = start(env)
    node = None
    log = ""
    try:
        def pumped_until(predicate, timeout=8.0):
            deadline = time.time() + timeout
            while time.time() < deadline:
                smoke.http("POST", "/api/v1/uplink/pump")
                if predicate():
                    return True
                time.sleep(0.2)
            return False

        node = subprocess.Popen([NODE_BIN], env=node_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        check(pumped_until(lambda: keys_report(work).get(uid.hex()) == (1, "confirmed")),
              "the host node, in its grace epoch, is rotated and confirms epoch 1")
        text = open(node_key).read().strip()
        check(text == "1:" + epoch_key(k_gw, uid, 1).hex(), "its key file holds `1:<the epoch-1 key>`")
        check((os.stat(node_key).st_mode & 0o777) == 0o600, "and is readable by its user alone")
        check(not os.path.exists(node_key + ".tmp"), "and no temporary file is left beside it")
        node.terminate()
        node.wait(timeout=5)
        node = subprocess.Popen([NODE_BIN], env=node_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        def listed():
            _, body = smoke.http("GET", "/api/v1/nodes")
            try:
                return any(n["area_id"] == AREA and n["node_id"] == node_number for n in json.loads(body))
            except ValueError:
                return False
        time.sleep(1.0)
        check(pumped_until(listed), "after a restart the node reads its new key file and is heard")
    finally:
        if node is not None and node.poll() is None:
            node.terminate()
            node.wait(timeout=5)
        log = stop(proc)
        opendtu.shutdown()
    check(f"opened for board {uid.hex()} under key epoch 1" in log,
          "its next session is opened under key epoch 1")


if __name__ == "__main__":
    main()
