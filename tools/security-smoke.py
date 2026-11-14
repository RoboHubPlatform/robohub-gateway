#!/usr/bin/env python3
"""The gateway's trust rules, driven end to end.

A gateway runs with a node manifest beside its key (`nodes.txt`, as Mesh writes
it), a pseudo-terminal as its serial link and boards dialling its uplink, and
this checks every rule the manifest and the sessions carry:

- a hello from a board the manifest does not list, or lists as revoked, is not
  answered; revoking a board drops the sessions it holds, with no traffic;
- a board's readings are taken for its own area and node only, a relay's for
  its own place and its areas only;
- an announce is taken only from a relay, for its own areas, and never for the
  gateway's own area — the last also without a manifest;
- a session is bound to the link it was opened on;
- a diagnostic request is sealed under the board's session, and refused when
  the board has none;
- a recorded hello is not answered again, does not rewrite the sessions file,
  and the serial link moves to a new session only once a frame comes under it;
- a counter restored after a restart starts past anything used before it.

Run it after `rho build`:

    python3 tools/security-smoke.py
"""
import copy
import os
import pty
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smoke  # noqa: E402
from smoke import (Board, announce_payload, control_response, device, frame, node_temperature,  # noqa: E402
                   parse_frame, read_frame, reading_payload, uplink_session, wait_ready)

HOST = "127.0.0.1"
HTTP_PORT = 18094
UPLINK_PORT = 18095
UDP_PORT = 19894
AREA = 7

failures = []


def check(ok: bool, message: str) -> None:
    print(("ok   " if ok else "FAIL ") + message)
    if not ok:
        failures.append(message)


def telemetry(area: int, node: int, temp_c: float) -> bytes:
    return b"\x01" + device(area, node) + struct.pack("<IfI", 1000, temp_c, 0)


def write_manifest(path: str, lines) -> None:
    # Written as Mesh writes it: a temporary file, then a rename.
    temporary = path + ".tmp"
    with open(temporary, "w") as f:
        f.write("# uid|area|node|role|epoch|revoked|areas\n")
        for line in lines:
            f.write(line + "\n")
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
    time.sleep(0.2)
    smoke.http("POST", "/api/v1/uplink/pump")


def children() -> int:
    _, body = smoke.http("GET", "/api/v1/diagnostics")
    return json.loads(body).get("children", -1)


def serial_session(master: int, board: Board, timeout: float = 1.5):
    """Says a board's hello on the serial link; the session payload, or None."""
    os.write(master, board.hello())
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            msg_id, _, payload = parse_frame(read_frame(master, max(0.1, deadline - time.time())))
        except TimeoutError:
            return None
        if msg_id == 18:
            return payload
    return None


def drain(master: int) -> None:
    while True:
        try:
            os.read(master, 4096)
        except BlockingIOError:
            return


def command_sid(master: int, area: int, node: int, answer: Board):
    """Sends a switch command to a board on the serial link and returns the
    session id the gateway sealed it under; `answer` acknowledges it."""
    seen = {}

    def responder():
        try:
            msg_id, tx_id, sealed = parse_frame(read_frame(master))
            seen["sid"] = sealed[:2]
            inner = answer.open(msg_id, tx_id, sealed)
            os.write(master, answer.next(0x0302, control_response(area, node, 2, 0, 1), tx_id))
            seen["inner"] = inner
        except Exception as exc:  # noqa: BLE001
            seen["error"] = str(exc)

    t = threading.Thread(target=responder)
    t.start()
    status, body = smoke.http("POST", f"/api/v1/nodes/{area}/{node}/switch", json.dumps({"on": True}))
    t.join(timeout=5)
    return seen.get("sid"), status


def main():
    work = tempfile.mkdtemp(prefix="gateway-security-")
    smoke.HTTP_PORT = HTTP_PORT
    smoke.TOKEN = smoke.mint_token(work)
    k_gw = os.urandom(32)
    key_file = os.path.join(work, "gateway.key")
    with open(key_file, "w") as f:
        f.write(k_gw.hex() + "\n")
    os.chmod(key_file, 0o600)
    nodes = os.path.join(work, "nodes.txt")
    sessions = os.path.join(work, "sessions")

    board = Board(k_gw)      # on the serial link, placed at 7/11
    relay = Board(k_gw)      # on the uplink, a relay for areas 9 and 10
    wifi = Board(k_gw)       # on the uplink, a board at 7/12
    revoked = Board(k_gw)    # listed, revoked
    unknown = Board(k_gw)    # its key derives, but it is not listed
    posted = Board(k_gw)     # a board whose frames a bridge posts over HTTP, at 7/14
    rows = [
        f"{board.uid.hex()}|7|11|board|0|0|",
        f"{relay.uid.hex().upper()}|7|20|relay|0|0|9,10",
        f"{wifi.uid.hex()}|7|12|board|0|0|",
        f"{revoked.uid.hex()}|7|30|board|0|1|",
        f"{posted.uid.hex()}|7|14|board|0|0|",
        "not a line of the manifest",
    ]
    write_manifest(nodes, rows)

    master, slave = pty.openpty()
    os.set_blocking(master, False)
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
        # --- Hellos the manifest refuses ----------------------------------
        conn = socket.create_connection((HOST, UPLINK_PORT), timeout=5)
        check(uplink_session(conn, unknown, 1.0) is None, "a hello from a board not in the manifest is not answered")
        check(uplink_session(conn, revoked, 1.0) is None, "a hello from a revoked board is not answered")
        conn.close()

        # --- A board on the serial link ------------------------------------
        drain(master)
        session = serial_session(master, board)
        check(session is not None, "a listed board's hello on the serial link is answered")
        board.accept(session)
        os.write(master, board.next(0x0201, telemetry(AREA, 11, 21.0)))
        os.write(master, board.next(0x0201, telemetry(AREA, 13, 31.0)))
        os.write(master, board.next(0x0201, telemetry(9, 11, 32.0)))
        time.sleep(0.4)
        # The board as it is under its first session, to answer for it later.
        first = copy.deepcopy(board)
        check(node_temperature(AREA, 11) == 21.0, "a board's reading for its own place is taken")
        check(node_temperature(AREA, 13) is None, "a board's reading for another node is dropped")
        check(node_temperature(9, 11) is None, "a board's reading for another area is dropped")

        # --- A recorded hello ----------------------------------------------
        kept = open(sessions, "rb").read()
        kept_at = os.stat(sessions).st_mtime_ns
        time.sleep(0.05)
        check(serial_session(master, board, 1.0) is None, "a replayed hello is not answered")
        check(open(sessions, "rb").read() == kept and os.stat(sessions).st_mtime_ns == kept_at,
              "a replayed hello does not rewrite the sessions file")

        # --- The serial link changes session only once a frame comes under it
        session = serial_session(master, _fresh(board))
        check(session is not None, "a board's new hello is answered")
        board.accept(session)
        sid, status = command_sid(master, AREA, 11, first)
        check(sid == first.sid and status == 200,
              "until a frame comes under the new session, the link stays under the one it held")
        os.write(master, board.next(0x0201, telemetry(AREA, 11, 22.0)))
        time.sleep(0.3)
        sid, status = command_sid(master, AREA, 11, board)
        check(sid == board.sid and status == 200, "the first frame under the new session makes it the link's")

        # --- Diagnostics are sealed under the board's session ---------------
        status, inner = diag_exchange(master, AREA, 11, board)
        check(inner is not None and inner[2:6] == struct.pack("<HH", AREA, 11),
              "a diagnostic request reaches the board sealed under its session")
        check(status == 200, f"and its sealed answer is the request's ({status})")

        # --- Frames posted over HTTP (POST /api/v1/frames) ------------------
        drain(master)
        status, body = post_frame(posted.hello(fresh=True))
        answer = json.loads(body).get("session", "") if status == 200 else ""
        check(answer != "", f"a hello posted over HTTP is answered in the response ({status})")
        check(not _serial_has_frame(master), "a hello posted over HTTP writes nothing to the serial link")
        if answer:
            _, _, payload = parse_frame(bytes.fromhex(answer))
            posted.accept(payload)
        status, _ = post_frame(posted.next(0x0201, telemetry(AREA, 14, 25.0)))
        check(status == 200 and node_temperature(AREA, 14) == 25.0, "a frame sealed under that session is taken")
        status, _ = post_frame(frame(0x0201, 0, telemetry(AREA, 14, 26.0)))
        check(status == 403 and node_temperature(AREA, 14) == 25.0, "a plain frame posted over HTTP is refused")
        status, _ = post_frame(board.next(0x0201, telemetry(AREA, 11, 27.0)))
        check(status == 403 and node_temperature(AREA, 11) != 27.0,
              "a frame sealed under the serial link's session is refused over HTTP")
        time.sleep(0.2)
        drain(master)

        # --- Sessions on the uplink, and which link they belong to ----------
        relay_conn = socket.create_connection((HOST, UPLINK_PORT), timeout=5)
        wifi_conn = socket.create_connection((HOST, UPLINK_PORT), timeout=5)
        check(uplink_session(relay_conn, relay) is not None, "a relay's hello is answered (uid case does not matter)")
        check(uplink_session(wifi_conn, wifi) is not None, "a board on Wi-Fi's hello is answered")
        stolen = wifi.next(0x0201, telemetry(AREA, 12, 41.0))
        relay_conn.sendall(stolen)
        pump()
        check(node_temperature(AREA, 12) is None, "a frame under one connection's session is refused on another")
        wifi_conn.sendall(stolen)
        pump()
        check(node_temperature(AREA, 12) == 41.0, "the same frame on its own connection is taken")
        os.write(master, relay.next(0x0201, reading_payload(9, 5, 42.0, 0, 1)))
        time.sleep(0.3)
        check(node_temperature(9, 5) is None, "a frame under an uplink session is refused on the serial link")

        # --- Announces -----------------------------------------------------
        wifi_conn.sendall(wifi.next(0x0101, announce_payload(7, 3, 12)))
        pump()
        check(children() == 0, "an announce from a board that is not a relay is refused")
        relay_conn.sendall(relay.next(0x0101, announce_payload(7, 2, 11)))
        pump()
        check(children() == 0, "a relay's announce for an area that is not its own is refused")
        relay_conn.sendall(relay.next(0x0101, announce_payload(7, 2, AREA)))
        pump()
        check(children() == 0, "an announce for the gateway's own area is refused")
        relay_conn.sendall(relay.next(0x0101, announce_payload(7, 2, 9)))
        pump()
        check(children() == 1, "a relay's announce for one of its areas is taken")

        # --- Relayed readings ----------------------------------------------
        relay_conn.sendall(relay.next(0x0201, reading_payload(9, 5, 43.0, 0, 2)))
        relay_conn.sendall(relay.next(0x0201, reading_payload(11, 5, 44.0, 0, 3)))
        relay_conn.sendall(relay.next(0x0201, reading_payload(AREA, 20, 45.0, 0, 4)))
        pump()
        check(node_temperature(9, 5) == 43.0, "a relayed reading for the relay's area is taken")
        check(node_temperature(11, 5) is None, "a relayed reading for another area is dropped")
        check(node_temperature(AREA, 20) == 45.0, "a relay's reading for its own place is taken")

        # --- Revocation, with no traffic -----------------------------------
        rows[1] = rows[1].replace("|0|9,10", "|1|9,10")
        rows[2] = rows[2].replace("|0|0|", "|0|1|")
        write_manifest(nodes, rows)
        time.sleep(6.5)
        status, body = smoke.http("POST", "/api/v1/nodes/9/5/sampling-rate", json.dumps({"interval_ms": 300}))
        drain(master)
        check(status != 202, f"a revoked relay's area is no longer routed to it ({status})")
        wifi_conn.sendall(wifi.next(0x0201, telemetry(AREA, 12, 46.0)))
        pump()
        check(node_temperature(AREA, 12) == 41.0, "a revoked board's session is dropped")
        wifi.nonce_b = os.urandom(8)
        check(uplink_session(wifi_conn, wifi, 1.0) is None, "a revoked board's new hello is not answered")
        relay_conn.close()
        wifi_conn.close()

        # --- Counters across a restart -------------------------------------
        os.write(master, board.sealed(1000, 0x0201, telemetry(AREA, 11, 50.0)))
        time.sleep(0.3)
        os.write(master, board.sealed(1010, 0x0201, telemetry(AREA, 11, 51.0)))
        time.sleep(0.3)
        check(node_temperature(AREA, 11) == 51.0, "a counter that jumps is taken")
        log += stop(proc)
        proc = start(env)
        os.write(master, board.sealed(1010, 0x0201, telemetry(AREA, 11, 52.0)))
        os.write(master, board.sealed(1100, 0x0201, telemetry(AREA, 11, 53.0)))
        time.sleep(0.4)
        check(node_temperature(AREA, 11) is None, "after a restart no counter used before it is taken")
        os.write(master, board.sealed(1129, 0x0201, telemetry(AREA, 11, 54.0)))
        time.sleep(0.4)
        check(node_temperature(AREA, 11) == 54.0, "after a restart the board is heard past the restored counter")
        log += stop(proc)
        check("node manifest" in log and "5 nodes, 1 revoked" in log and "did not parse skipped" in log,
              "the manifest is read and said, its bad line skipped")
        check("dropped: it is revoked" in log, "a revocation is said")

        # --- Without a manifest --------------------------------------------
        os.remove(nodes)
        os.remove(sessions)
        proc = start(env)
        first_relay = Board(k_gw)
        second_relay = Board(k_gw)
        status, _ = smoke.http("GET", f"/api/v1/nodes/{AREA}/11/diagnostics")
        check(status == 409 and not _serial_has_frame(master),
              f"a diagnostic for a board with no session is refused, and nothing is written ({status})")
        a = socket.create_connection((HOST, UPLINK_PORT), timeout=5)
        b = socket.create_connection((HOST, UPLINK_PORT), timeout=5)
        check(uplink_session(a, first_relay) is not None, "without a manifest a derivable key is answered")
        uplink_session(b, second_relay)
        a.sendall(first_relay.next(0x0101, announce_payload(7, 2, AREA)))
        pump()
        check(children() == 0, "without a manifest an announce for the gateway's own area is refused")
        a.sendall(first_relay.next(0x0101, announce_payload(7, 2, 9)))
        pump()
        check(children() == 1, "without a manifest a relay's announce for another area is taken")
        b.sendall(second_relay.next(0x0101, announce_payload(7, 4, 9)))
        b.sendall(second_relay.next(0x0101, announce_payload(7, 2, 10)))
        pump()
        check(children() == 1, "without a manifest an area or gateway id another session holds is not taken over")
        a.sendall(first_relay.next(0x0201, reading_payload(9, 6, 61.0, 0, 5)))
        a.sendall(first_relay.next(0x0201, reading_payload(12, 6, 62.0, 0, 6)))
        pump()
        check(node_temperature(9, 6) == 61.0 and node_temperature(12, 6) is None,
              "without a manifest a relay's readings are taken only for the areas it announced")
        behind = Board(k_gw)
        a.sendall(first_relay.next(0x0013, struct.pack("<BHH", 1, 0x0011, 0) + _hello_payload(behind)))
        pump()
        session = _relayed_session(a, first_relay)
        check(session is not None, "without a manifest a board behind a relay is answered through it")
        if session is not None:
            behind.accept(session)
            for area, temp in ((9, 63.0), (12, 64.0)):
                behind.counter += 1
                inner = telemetry(area, 7, temp)
                sid_counter = behind.sid + struct.pack("<I", behind.counter)
                tag = smoke.hmac16(behind.k_up, struct.pack("<HH", 0x8201, 0) + sid_counter + inner)[:8]
                a.sendall(first_relay.next(0x0013, struct.pack("<BHH", 1, 0x8201, 0) + sid_counter + inner + tag))
            pump()
            check(node_temperature(9, 7) == 63.0 and node_temperature(12, 7) is None,
                  "without a manifest a relayed board's readings are taken only for the areas its relay announced")
        a.close()
        b.close()
        out = stop(proc)
        check("no node manifest" in out, "a gateway with no manifest says so")
    finally:
        if proc.poll() is None:
            stop(proc)

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("\nOK: the gateway's trust rules hold")


def diag_exchange(master: int, area: int, node: int, answer: Board):
    """Asks the gateway for a board's diagnostics; the board on the serial link
    opens the request and answers sealed. Returns (status, the request's bytes)."""
    seen = {}

    def responder():
        try:
            msg_id, tx_id, sealed = parse_frame(read_frame(master))
            seen["msg_id"] = msg_id
            inner = answer.open(msg_id, tx_id, sealed)
            seen["inner"] = inner
            os.write(master, answer.next(0x0402, smoke.diag_response(area, node, 4242, 0x0085, 0), tx_id))
        except Exception as exc:  # noqa: BLE001
            seen["error"] = str(exc)

    t = threading.Thread(target=responder)
    t.start()
    status, _ = smoke.http("GET", f"/api/v1/nodes/{area}/{node}/diagnostics")
    t.join(timeout=5)
    if seen.get("msg_id") != 0x8401:
        return status, None
    return status, seen.get("inner")


def post_frame(data: bytes):
    """Posts one encoded frame, as a bridge does: without the delimiter."""
    return smoke.http("POST", "/api/v1/frames", data[:-1] if data.endswith(b"\x00") else data)


def _serial_has_frame(master: int) -> bool:
    try:
        read_frame(master, 0.5)
        return True
    except TimeoutError:
        return False


def _hello_payload(board: Board) -> bytes:
    board.nonce_b = os.urandom(8)
    return board.uid + board.nonce_b + smoke.hmac16(board.k_dev, b"rh-hello1" + board.uid + board.nonce_b)[:8]


def _relayed_session(sock, relay: Board, timeout: float = 3.0):
    """The SESSION payload a gateway sent inside RELAYED to a relay, or None."""
    deadline = time.time() + timeout
    sock.settimeout(timeout)
    while time.time() < deadline:
        try:
            msg_id, tx_id, payload = parse_frame(smoke.read_socket_frame(sock, max(0.1, deadline - time.time())))
        except (TimeoutError, socket.timeout, OSError):
            return None
        if msg_id == 0x8013:
            inner = relay.open(msg_id, tx_id, payload)
            if struct.unpack("<BHH", inner[:5])[1] == 0x0012:
                return inner[5:]
    return None


def _fresh(board: Board) -> Board:
    board.nonce_b = os.urandom(8)
    return board


if __name__ == "__main__":
    main()
