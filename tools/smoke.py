#!/usr/bin/env python3
"""End-to-end smoke test for robohub-gateway.

A pseudo-terminal stands in for the node and a UDP socket for PlotJuggler. The
gateway is a subprocess; this drives a telemetry frame and a control RPC through
the pty and checks the REST API, the diagnostics and the streamed datagrams.

The frames are the L2 (`link`) schema, generated from schemas/link.rho: a
`Telemetry` payload is the version byte, the device (a version byte, an area and
a node), a timestamp, a temperature and a status; a control is a device, an
opcode and an argument. Run it after `rho build`:

    python3 tools/smoke.py
"""
import hashlib
import hmac
import http.client as httpclient
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

BIN = os.environ.get("GATEWAY_BIN", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "build", "robohub-gateway"))
HOST = "127.0.0.1"
# The ports this smoke takes; another run on the same machine names others.
HTTP_PORT = int(os.environ.get("GATEWAY_SMOKE_HTTP_PORT", "18080"))
UDP_PORT = int(os.environ.get("GATEWAY_SMOKE_UDP_PORT", "19870"))
UPLINK_PORT = int(os.environ.get("GATEWAY_SMOKE_UPLINK_PORT", "18081"))
AREA = 7
SAMPLING_RATE_OPCODE = 1

sequence = 0

# A ticket for the gateway, as the keeper would issue one: the API answers only
# a caller that carries one (set in main).
TOKEN = ""
RHO = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "rho", "bin", "rho")


def fletcher16(data: bytes) -> int:
    s1 = s2 = 0
    for b in data:
        s1 = (s1 + b) % 255
        s2 = (s2 + s1) % 255
    return (s2 << 8) | s1


def cobs_encode(data: bytes) -> bytes:
    out = bytearray()
    run = bytearray()
    for b in data:
        if b == 0:
            out.append(len(run) + 1)
            out += run
            run = bytearray()
        else:
            run.append(b)
            if len(run) == 254:
                out.append(255)
                out += run
                run = bytearray()
    out.append(len(run) + 1)
    out += run
    return bytes(out)


def frame(msg_id: int, tx_id: int, payload: bytes) -> bytes:
    body = struct.pack("<HHH", msg_id, tx_id, len(payload)) + payload
    body += struct.pack("<H", fletcher16(body))
    return cobs_encode(body) + b"\x00"


def device(area: int, node: int) -> bytes:
    # A DeviceId carries its own version byte, so a message that contains one
    # contains the identity's bytes rather than duplicating them.
    return b"\x01" + struct.pack("<HH", area, node)


def telemetry_frame(area: int, node: int, temp_c: float, status: int, ts_ms: int) -> bytes:
    # Telemetry: version, device, timestamp, temperature, status — little-endian,
    # exactly as robohub-transport carries it.
    payload = b"\x01" + device(area, node) + struct.pack("<IfI", ts_ms, temp_c, status)
    return frame(0x0201, 0, payload)


def control_response(area: int, node: int, opcode: int, result: int, argument: int) -> bytes:
    return b"\x01" + device(area, node) + struct.pack("<HHI", opcode, result, argument)


def diag_response(area: int, node: int, uptime_ms: int, probe_ok: int, probe_fault: int) -> bytes:
    # DiagResponse: version, device, the counters, last error, and the smoke.
    counters = struct.pack("<IIIIIHII", uptime_ms, 17, 0, 3, 0, 0, probe_ok, probe_fault)
    return b"\x01" + device(area, node) + counters


def announce_payload(gateway_area: int, link_id: int, announces_area: int) -> bytes:
    # Announce: version, gateway (area, link), firmware, mode, capabilities, area.
    return b"\x01" + b"\x01" + struct.pack("<HH", gateway_area, link_id) + \
        struct.pack("<HHHH", 1, 2, 0x0A, announces_area)


def reading_payload(area: int, node: int, temp_c: float, status: int, ts_ms: int) -> bytes:
    # Reading (L1): version, device, timestamp, temperature, status as a u16.
    return b"\x01" + device(area, node) + struct.pack("<IfH", ts_ms, temp_c, status)


def power_reading_frame(area: int, node: int, kind: int, status: int, valid: int,
                        pv: float, load: float, grid: float, battery: float,
                        soc: float, e_today: float, e_total: float, ts_ms: int) -> bytes:
    # PowerReading: version, device, timestamp, kind, status, valid, then the
    # seven figures — little-endian, the layout both ends generate.
    payload = b"\x01" + device(area, node) + struct.pack(
        "<IIHHfffffff", ts_ms, kind, status, valid, pv, load, grid, battery, soc, e_today, e_total)
    return frame(0x0202, 0, payload)


def state_reading_frame(area: int, node: int, kind: int, status: int, valid: int,
                        switch_state: int, setpoint: float, ts_ms: int) -> bytes:
    # StateReading: version, device, timestamp, kind, status, valid, switch state,
    # setpoint.
    payload = b"\x01" + device(area, node) + struct.pack(
        "<IIHHHf", ts_ms, kind, status, valid, switch_state, setpoint)
    return frame(0x0203, 0, payload)


def health_frame(area: int, node: int, reason: int, boot_count: int, fault_code: int,
                 fault_task: int, fault_pc: int, fault_uptime_ms: int, firmware: int) -> bytes:
    # Health (PROTO-0007, 0x0403): version, device, reset reason (u32), boot
    # count, fault code, fault task (u16), fault pc, fault uptime, firmware (u16).
    payload = b"\x01" + device(area, node) + struct.pack(
        "<IIIHIIH", reason, boot_count, fault_code, fault_task, fault_pc, fault_uptime_ms, firmware)
    return frame(0x0403, 0, payload)


def parse_frame(raw: bytes):
    # raw is COBS-encoded without the delimiter; decode it.
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        code = raw[i]
        i += 1
        for _ in range(1, code):
            out.append(raw[i])
            i += 1
        if code < 255 and i < n:
            out.append(0)
    msg_id, tx_id, plen = struct.unpack("<HHH", out[:6])
    payload = bytes(out[6:6 + plen])
    crc = struct.unpack("<H", out[6 + plen:6 + plen + 2])[0]
    assert crc == fletcher16(bytes(out[:6 + plen])), "crc mismatch"
    return msg_id, tx_id, payload


def read_frame(master: int, timeout: float = 3.0) -> bytes:
    deadline = time.time() + timeout
    buf = bytearray()
    while time.time() < deadline:
        try:
            chunk = os.read(master, 256)
        except BlockingIOError:
            time.sleep(0.01)
            continue
        buf += chunk
        if 0 in buf:
            return bytes(buf[:buf.index(0)])
    raise TimeoutError("no frame from gateway")


def read_socket_frame(sock, timeout: float = 3.0) -> bytes:
    # The uplink carries the same COBS frames closed with a zero, so a socket
    # reads them the way a pty does.
    deadline = time.time() + timeout
    buf = bytearray()
    while time.time() < deadline:
        chunk = sock.recv(256)
        if not chunk:
            raise TimeoutError("uplink closed")
        buf += chunk
        if 0 in buf:
            return bytes(buf[:buf.index(0)])
    raise TimeoutError("no frame from the gateway")


def http(method, path, body=None):
    conn = httpclient.HTTPConnection(HOST, HTTP_PORT, timeout=5)
    headers = {"Content-Type": "application/json"} if body else {}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    return resp.status, data


def mint_token(home: str) -> str:
    rho = RHO if os.path.exists(RHO) else "rho"
    keys = os.path.join(home, ".rho", "auth")
    os.makedirs(keys, exist_ok=True)
    subprocess.run([rho, "auth", "keygen", "--dir", keys, "--force"], check=True, capture_output=True, text=True)
    return subprocess.run([rho, "auth", "ticket", "--private", os.path.join(keys, "private.pem"), "--issuer", "keeper",
                           "--audience", "gateway", "--subject", "smoke", "--scopes", "*", "--ttl", "1h"],
                          check=True, capture_output=True, text=True).stdout.strip()


def wait_ready(proc) -> None:
    last = None
    for _ in range(100):
        try:
            status, _ = http("GET", "/healthz")
            if status == 200:
                return
            last = f"status {status}"
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(0.05)
    proc.kill()
    print("FAIL: server did not become ready; last:", last)
    print(proc.stdout.read())
    sys.exit(1)


def hmac16(key: bytes, message: bytes) -> bytes:
    return hmac.new(key, message, hashlib.sha256).digest()


class Board:
    """A board's half of robohub-transport's sealed frames, as include/robohub/secure.h
    has it: a hello, the gateway's session, then frames carrying a counter and a tag."""

    def __init__(self, k_gw: bytes, uid: bytes = b""):
        self.uid = uid or os.urandom(8)
        self.k_dev = hmac16(k_gw, b"rh-dev1" + self.uid)[:16]
        self.nonce_b = os.urandom(8)
        self.sid = b""
        self.k_up = b""
        self.k_down = b""
        self.counter = 0

    def hello(self, fresh: bool = False) -> bytes:
        if fresh:
            self.nonce_b = os.urandom(8)
        tag = hmac16(self.k_dev, b"rh-hello1" + self.uid + self.nonce_b)[:8]
        return frame(17, 0, self.uid + self.nonce_b + tag)

    def accept(self, payload: bytes) -> None:
        uid, sid, nonce_g, lifetime, tag = payload[:8], payload[8:10], payload[10:18], payload[18:20], payload[20:28]
        want = hmac16(self.k_dev, b"rh-session1" + uid + self.nonce_b + sid + nonce_g + lifetime)[:8]
        assert uid == self.uid and tag == want, "the session did not authenticate"
        self.sid = sid
        self.k_up = hmac16(self.k_dev, b"rh-up1" + self.nonce_b + nonce_g)[:16]
        self.k_down = hmac16(self.k_dev, b"rh-down1" + self.nonce_b + nonce_g)[:16]
        self.counter = 0

    def sealed(self, counter: int, msg_id: int, inner: bytes, tx_id: int = 0, sid: bytes = b"") -> bytes:
        flagged = msg_id | 0x8000
        sid_counter = (sid or self.sid) + struct.pack("<I", counter)
        tag = hmac16(self.k_up, struct.pack("<HH", flagged, tx_id) + sid_counter + inner)[:8]
        return frame(flagged, tx_id, sid_counter + inner + tag)

    def next(self, msg_id: int, inner: bytes, tx_id: int = 0) -> bytes:
        self.counter += 1
        return self.sealed(self.counter, msg_id, inner, tx_id)

    def open(self, msg_id: int, tx_id: int, payload: bytes) -> bytes:
        """The inner payload of a frame the gateway sealed for this board."""
        assert msg_id & 0x8000, "the gateway sent a plain frame"
        sid_counter, inner, tag = payload[:6], payload[6:-8], payload[-8:]
        want = hmac16(self.k_down, struct.pack("<HH", msg_id, tx_id) + sid_counter + inner)[:8]
        assert sid_counter[:2] == self.sid and tag == want, "the gateway's frame did not authenticate"
        return inner


def uplink_session(sock, board, timeout: float = 3.0):
    """Says a board's hello up an uplink connection and takes the session the
    gateway answers with, or returns None when it does not answer."""
    sock.sendall(board.hello())
    # The hello is applied when the uplink is drained; it is asked for until the
    # answer comes, since one drain can run before the hello has arrived.
    deadline = time.time() + timeout
    sock.settimeout(0.2)
    buf = bytearray()
    while time.time() < deadline:
        http("POST", "/api/v1/uplink/pump")
        try:
            chunk = sock.recv(256)
            if not chunk:
                return None
            buf += chunk
        except socket.timeout:
            continue
        except OSError:
            return None
        while 0 in buf:
            at = buf.index(0)
            raw = bytes(buf[:at])
            del buf[:at + 1]
            if not raw:
                continue
            msg_id, _, payload = parse_frame(raw)
            if msg_id == 18:
                board.accept(payload)
                sock.settimeout(timeout)
                return payload
    sock.settimeout(timeout)
    return None


def node_temperature(area: int, node: int):
    status, body = http("GET", "/api/v1/nodes")
    try:
        for n in json.loads(body):
            if n["area_id"] == area and n["node_id"] == node:
                return n.get("temperature_c")
    except Exception:  # noqa: BLE001
        pass
    return None


def main():
    global TOKEN
    work = tempfile.mkdtemp(prefix="gateway-smoke-")
    TOKEN = mint_token(work)
    k_gw = os.urandom(32)
    key_file = os.path.join(work, "gateway.key")
    with open(key_file, "w") as f:
        f.write(k_gw.hex() + "\n")
    os.chmod(key_file, 0o600)
    master, slave = pty.openpty()
    os.set_blocking(master, False)
    slave_name = os.ttyname(slave)

    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind((HOST, UDP_PORT))
    udp.settimeout(3.0)

    env = dict(os.environ)
    env.update({
        "SERIAL_PORT": slave_name,
        "SERIAL_BAUD": "115200",
        "REST_PORT": str(HTTP_PORT),
        "PLOTJUGGLER_ADDR": f"{HOST}:{UDP_PORT}",
        "GATEWAY_MODE": "master",
        "GATEWAY_AREA": str(AREA),
        "UPLINK_PORT": str(UPLINK_PORT),
        "HOME": work,
        "GATEWAY_KEY_FILE": key_file,
        # The sections below drive plain frames, as boards being reflashed send;
        # the sealed path is driven at the end.
        "ALLOW_PLAIN_FRAMES": "1",
        "GATEWAY_UPDATE_OUTCOME_MS": "1500",
    })
    proc = subprocess.Popen([BIN], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)

    failures = []

    wait_ready(proc)

    # 1. Telemetry frame -> state + UDP.
    os.write(master, telemetry_frame(AREA, 1, 24.31, 0, 124500))
    try:
        datagram = udp.recv(4096).decode()
    except socket.timeout:
        datagram = ""
    print("UDP:", datagram.strip())
    want = {"area_id": AREA, "node_id": 1, "timestamp_ms": 124500, "temperature_c": 24.31, "status": 0}
    try:
        got = json.loads(datagram)
    except Exception:
        got = None
    if got != want:
        failures.append(f"UDP JSON mismatch: got {got!r} want {want!r}")

    # 2. REST read reflects the frame.
    status, body = http("GET", "/api/v1/telemetry/temperature")
    print("GET:", status, body)
    try:
        latest = json.loads(body)
    except Exception:
        latest = {}
    if status != 200 or latest.get("area_id") != AREA or latest.get("node_id") != 1 or \
       latest.get("temperature_c") != 24.31 or latest.get("status") != "OK" or \
       latest.get("last_updated_ms") != 124500:
        failures.append(f"GET mismatch: {status} {body}")

    # 2b. A second board is attributed separately; a board in another area with
    # the same node id is a third device, which is the point of the area.
    os.write(master, telemetry_frame(AREA, 2, 19.5, 0, 124600))
    os.write(master, telemetry_frame(9, 2, 17.25, 0, 124700))
    time.sleep(0.2)
    status, body = http("GET", "/api/v1/nodes")
    print("NODES:", status, body)
    try:
        listed = {(n["area_id"], n["node_id"]): n for n in json.loads(body)}
    except Exception:
        listed = {}
    if status != 200 or set(listed) != {(AREA, 1), (AREA, 2), (9, 2)} or \
       listed.get((AREA, 2), {}).get("temperature_c") != 19.5:
        failures.append(f"nodes mismatch: {status} {body}")

    # 2c. Diagnostics says the mode and the area it was deployed with.
    status, body = http("GET", "/api/v1/diagnostics")
    print("DIAG:", status, body)
    try:
        diagnostics = json.loads(body)
    except Exception:
        diagnostics = {}
    if status != 200 or diagnostics.get("mode") != "master" or diagnostics.get("area_id") != AREA \
       or diagnostics.get("devices_seen") != 3:
        failures.append(f"diagnostics mismatch: {status} {body}")

    # 2c-bis. The root page is the gateway's own page, not a 404: a person opens
    # the same server the API runs on and sees the nodes it has heard from.
    status, body = http("GET", "/")
    print("HOME:", status, "bytes:", len(body))
    if status != 200 or "RoboHub Gateway" not in body or "24.31" not in body:
        failures.append(f"home page mismatch: {status} {body[:200]}")

    # 2c-ter. The frame history: more than the latest row, newest first, and a
    # CSV export that keeps the transmitted millisecond timestamps.
    status, body = http("GET", "/frames/10")
    print("FRAMES:", status, "bytes:", len(body))
    if status != 200 or "Frames" not in body or "24.31" not in body:
        failures.append(f"frames page mismatch: {status} {body[:200]}")
    status, body = http("GET", "/export/frames/10/csv")
    print("CSV:", status, "bytes:", len(body))
    if status != 200 or not body.startswith("timestamp_ms,received_at_ms,kind,area_id,node_id") or "24.31" not in body:
        failures.append(f"frames csv mismatch: {status} {body[:200]}")

    # 2d. A power device — an inverter or a meter — sends a PowerReading, its own
    # message. It becomes a device record with power and energy figures, the
    # listing carries them, the power endpoint serves the latest, and the stream
    # has a power record of its own shape.
    udp.settimeout(0.5)
    try:
        while True:
            udp.recv(4096)
    except socket.timeout:
        pass
    udp.settimeout(3.0)

    os.write(master, power_reading_frame(AREA, 10, 1, 0, 0x7F,
                                         1200.5, 800.0, -350.25, 50.0, 85.0, 4.2, 1234.5, 130000))
    try:
        power_dgram = udp.recv(4096).decode()
    except socket.timeout:
        power_dgram = ""
    print("UDP power:", power_dgram.strip())
    try:
        got_power = json.loads(power_dgram)
    except Exception:
        got_power = None
    if not got_power or got_power.get("area_id") != AREA or got_power.get("node_id") != 10 or \
       got_power.get("kind") != 1 or got_power.get("pv_power_w") != 1200.5 or \
       got_power.get("battery_soc") != 85.0:
        failures.append(f"power stream mismatch: {got_power!r}")

    status, body = http("GET", "/api/v1/telemetry/power")
    print("GET power:", status, body)
    try:
        latest_power = json.loads(body)
    except Exception:
        latest_power = {}
    if status != 200 or latest_power.get("area_id") != AREA or latest_power.get("node_id") != 10 or \
       latest_power.get("pv_power_w") != 1200.5 or latest_power.get("grid_power_w") != -350.25 or \
       latest_power.get("energy_total_kwh") != 1234.5:
        failures.append(f"power GET mismatch: {status} {body}")

    status, body = http("GET", "/api/v1/nodes")
    print("NODES with power:", status, body)
    try:
        listed = {(n["area_id"], n["node_id"]): n for n in json.loads(body)}
    except Exception:
        listed = {}
    if listed.get((AREA, 10), {}).get("kind") != 1 or \
       listed.get((AREA, 10), {}).get("pv_power_w") != 1200.5:
        failures.append(f"the power device is missing from the listing: {body}")

    # 2e. A device's state — a switch's on/off — is its own reading, and a device
    # that is only a switch carries its kind and state and no power.
    udp.settimeout(0.5)
    try:
        while True:
            udp.recv(4096)
    except socket.timeout:
        pass
    udp.settimeout(3.0)

    os.write(master, state_reading_frame(AREA, 12, 6, 0, 1, 1, 0.0, 140000))
    try:
        state_dgram = udp.recv(4096).decode()
    except socket.timeout:
        state_dgram = ""
    print("UDP state:", state_dgram.strip())
    try:
        got_state = json.loads(state_dgram)
    except Exception:
        got_state = None
    if not got_state or got_state.get("area_id") != AREA or got_state.get("node_id") != 12 or \
       got_state.get("kind") != 6 or got_state.get("switch_state") != 1:
        failures.append(f"state stream mismatch: {got_state!r}")

    status, body = http("GET", "/api/v1/telemetry/state")
    print("GET state:", status, body)
    try:
        latest_state = json.loads(body)
    except Exception:
        latest_state = {}
    if status != 200 or latest_state.get("kind") != 6 or latest_state.get("switch_state") != 1:
        failures.append(f"state GET mismatch: {status} {body}")

    status, body = http("GET", "/api/v1/nodes")
    try:
        listed = {(n["area_id"], n["node_id"]): n for n in json.loads(body)}
    except Exception:
        listed = {}
    if listed.get((AREA, 12), {}).get("switch_state") != 1 or \
       listed.get((AREA, 12), {}).get("kind") != 6:
        failures.append(f"the switch is missing from the listing: {body}")

    # 2b. Health (GW-0015): a board that restarted on its watchdog says so once;
    # the listing keeps the last report, and the outbox carries a `reset` event.
    os.write(master, health_frame(AREA, 12, 2, 5, 2, 3, 0x10001234, 61000, 7))
    os.write(master, health_frame(AREA, 13, 1, 1, 0, 0xFFFF, 0, 0, 7))
    time.sleep(0.5)
    status, body = http("GET", "/api/v1/nodes")
    print("GET nodes (health):", status, body)
    try:
        listed = {(n["area_id"], n["node_id"]): n for n in json.loads(body)}
    except Exception:
        listed = {}
    health = listed.get((AREA, 12), {}).get("health") or {}
    fault = health.get("fault") or {}
    if health.get("reset_reason") != "watchdog" or health.get("boot_count") != 5 or \
       health.get("firmware_version") != 7 or fault.get("code") != 2 or fault.get("task") != 3 or \
       fault.get("pc") != 0x10001234 or fault.get("uptime_ms") != 61000 or not health.get("received_at_ms"):
        failures.append(f"the health report is missing from the listing: {body}")
    calm = listed.get((AREA, 13), {}).get("health") or {}
    if calm.get("reset_reason") != "power-on" or calm.get("fault", "absent") is not None:
        failures.append(f"a power-on report is not listed as one: {body}")
    status, body = http("GET", "/api/v1/telemetry/outbox?after=0&limit=1000")
    print("GET outbox (health):", status, body[:400])
    try:
        resets = [e for e in json.loads(body) if e.get("field") == "reset"]
    except Exception:
        resets = []
    if [(e.get("area_id"), e.get("node_id"), e.get("value"), e.get("status")) for e in resets] != [(AREA, 12, 2, 2)]:
        failures.append(f"one reset event for the watchdog reset, none for the power-on: {body[:400]}")

    # 2c. An update carried to a board (GW-0016): a board on the pty takes the
    # offer, loses a chunk and gets it again, verifies the image and confirms it;
    # a second update is cancelled. The board here is a stand-in: the gateway
    # does not check the manifest's signature, the board does.
    import hashlib
    image = bytes((i * 31 + (i >> 4)) & 0xFF for i in range(5000))

    def offer_hex(update_id, data, chunk=192):
        manifest = struct.pack("<BI8sHII32sH", 1, update_id, b"\x01" * 8, 2, 1, len(data),
                               hashlib.sha256(data).digest(), chunk)
        return (b"\x01" + manifest + b"\x5a" * 64).hex()

    def status_frame(update_id, state, reason, nxt, version=1, slot=0):
        return frame(0x0503, 0, struct.pack("<BIIIIHB", 1, update_id, state, reason, nxt, version, slot))

    board_seen = {"offers": [], "chunks": 0, "cancels": [], "dropped": 0, "image": bytearray()}
    board_stop = threading.Event()

    def update_board():
        buf = bytearray()
        expect = None
        while not board_stop.is_set():
            try:
                data = os.read(master, 4096)
            except BlockingIOError:
                time.sleep(0.005)
                continue
            buf += data
            while 0 in buf:
                raw = bytes(buf[:buf.index(0)])
                del buf[:buf.index(0) + 1]
                try:
                    msg_id, _tx, payload = parse_frame(raw)
                except Exception:  # noqa: BLE001
                    continue
                if msg_id == 0x0501:
                    _ov, _mv, uid, _u, _fw, _sec, size, digest, chunk = struct.unpack("<BBI8sHII32sH", payload[:58])
                    board_seen["offers"].append(uid)
                    if uid == 502:
                        continue  # this board does not answer: the update waits, and is cancelled
                    if expect is not None and expect["id"] == uid and expect.get("ended"):
                        board_seen["asked_again"] = board_seen.get("asked_again", 0) + 1
                        os.write(master, status_frame(uid, expect["ended"], 0, 0, version=2, slot=1))
                        continue
                    if expect is None or expect["id"] != uid:
                        expect = {"id": uid, "size": size, "digest": digest, "chunk": chunk, "next": 0, "since": 0}
                        board_seen["image"] = bytearray()
                    os.write(master, status_frame(uid, 1, 0, expect["next"]))
                elif msg_id == 0x0502 and expect is not None:
                    uid, index = struct.unpack("<II", payload[:8])
                    board_seen["chunks"] += 1
                    if uid != expect["id"]:
                        continue
                    if index == 7 and board_seen["dropped"] == 0:
                        board_seen["dropped"] = 1      # lost on the wire
                        continue
                    if index != expect["next"]:
                        os.write(master, status_frame(uid, 3, 0, expect["next"]))
                        continue
                    board_seen["image"] += payload[8:]
                    expect["next"] += 1
                    expect["since"] += 1
                    if len(board_seen["image"]) == expect["size"]:
                        ok = hashlib.sha256(bytes(board_seen["image"])).digest() == expect["digest"]
                        os.write(master, status_frame(uid, 4 if ok else 7, 0 if ok else 8, expect["next"]))
                        # The board reboots into it, and its `confirmed` is lost
                        # with its link: the gateway asks again by offering it.
                        expect["ended"] = 5 if ok else None
                    elif expect["since"] >= 4:
                        expect["since"] = 0
                        os.write(master, status_frame(uid, 3, 0, expect["next"]))
                elif msg_id == 0x0504:
                    board_seen["cancels"].append(struct.unpack("<I", payload[1:5])[0])

    board_thread = threading.Thread(target=update_board, daemon=True)
    board_thread.start()
    status, body = http("POST", f"/api/v1/nodes/{AREA}/12/update?offer=00", "x")
    if status != 400:
        failures.append(f"a malformed offer is refused: {status} {body}")
    status, body = http("POST", f"/api/v1/nodes/{AREA}/12/update?offer={offer_hex(500, image)}", image[:100].decode("latin-1"))
    if status != 400:
        failures.append(f"an image shorter than its manifest is refused: {status} {body}")
    conn = httpclient.HTTPConnection(HOST, HTTP_PORT, timeout=10)
    conn.request("POST", f"/api/v1/nodes/{AREA}/12/update?offer={offer_hex(500, image)}", body=image,
                 headers={"Content-Type": "application/octet-stream", "Authorization": f"Bearer {TOKEN}"})
    resp = conn.getresponse()
    started = resp.read().decode()
    print("UPDATE:", resp.status, started)
    if resp.status != 202:
        failures.append(f"an update is taken: {resp.status} {started}")
    status, body = http("POST", f"/api/v1/nodes/{AREA}/12/update?offer={offer_hex(501, image)}", "x" * 10)
    deadline = time.time() + 20
    state = {}
    while time.time() < deadline:
        status, body = http("GET", f"/api/v1/nodes/{AREA}/12/update")
        try:
            state = json.loads(body)
        except Exception:
            state = {}
        if state.get("state") in ("confirmed", "failed", "refused", "rolled_back"):
            break
        time.sleep(0.2)
    print("UPDATE state:", state, "board saw", board_seen["chunks"], "chunks,", len(board_seen["offers"]), "offers")
    if state.get("state") != "confirmed" or state.get("running_version") != 2 or state.get("next") != 27:
        failures.append(f"the update reaches confirmed: {state}")
    if bytes(board_seen["image"]) != image or board_seen["dropped"] != 1:
        failures.append("the board received the whole image, the lost chunk sent again")
    if board_seen.get("asked_again", 0) < 1:
        failures.append("after verified with no outcome, the gateway offered the update again to hear how it ended")
    status, body = http("GET", "/api/v1/nodes")
    listed = {(n["area_id"], n["node_id"]): n for n in json.loads(body)} if status == 200 else {}
    if (listed.get((AREA, 12), {}).get("update") or {}).get("state") != "confirmed":
        failures.append(f"the board's entry carries its update: {body[:300]}")
    status, body = http("GET", "/api/v1/telemetry/outbox?after=0&limit=1000")
    updates = [e for e in json.loads(body) if e.get("field") == "update"] if status == 200 else []
    if not updates or updates[-1].get("value") != 5:
        failures.append(f"the board's answers reach the outbox as update events: {updates[-3:]}")
    # A second update, cancelled while it waits.
    conn = httpclient.HTTPConnection(HOST, HTTP_PORT, timeout=10)
    board_stop_offers = len(board_seen["offers"])
    conn.request("POST", f"/api/v1/nodes/{AREA}/13/update?offer={offer_hex(502, image[:300])}", body=image[:300],
                 headers={"Content-Type": "application/octet-stream", "Authorization": f"Bearer {TOKEN}"})
    resp = conn.getresponse()
    resp.read()
    conn = httpclient.HTTPConnection(HOST, HTTP_PORT, timeout=10)
    conn.request("POST", f"/api/v1/nodes/{AREA}/13/update?offer={offer_hex(503, image[:300])}", body=image[:300],
                 headers={"Content-Type": "application/octet-stream", "Authorization": f"Bearer {TOKEN}"})
    resp = conn.getresponse()
    busy = resp.status
    resp.read()
    if busy != 409:
        failures.append(f"a second update for a board with one under way is refused: {busy}")
    status, body = http("DELETE", f"/api/v1/nodes/{AREA}/13/update")
    deadline = time.time() + 2
    while time.time() < deadline and 502 not in board_seen["cancels"]:
        time.sleep(0.05)
    if status != 200 or json.loads(body).get("state") != "cancelled" or 502 not in board_seen["cancels"]:
        failures.append(f"an update is cancelled, and the board is told: {status} {body} {board_seen['cancels']}")
    board_stop.set()
    board_thread.join(timeout=2)

    # 3. Control: POST sampling-rate, answer the gateway's 0x0301 with a 0x0302.

    def node_responder():
        try:
            raw = read_frame(master)
            msg_id, tx_id, payload = parse_frame(raw)
            print(f"node saw msg=0x{msg_id:04x} tx={tx_id} payload={payload.hex()}")
            # payload: version, device (version, area, node), opcode, argument.
            version = payload[0]
            area, node = struct.unpack("<HH", payload[2:6])
            opcode, argument = struct.unpack("<HI", payload[6:12])
            if msg_id != 0x0301 or version != 1 or opcode != SAMPLING_RATE_OPCODE or argument != 250:
                failures.append(f"unexpected control frame {msg_id} {payload.hex()}")
            # The device clamps to 240 and answers with what it set.
            os.write(master, frame(0x0302, tx_id, control_response(area, node, opcode, 0, 240)))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"responder: {exc}")

    t = threading.Thread(target=node_responder)
    t.start()
    status, body = http("POST", "/api/v1/telemetry/sampling-rate",
                        json.dumps({"interval_ms": 250}))
    t.join(timeout=5)
    print("POST:", status, body)
    try:
        rpc = json.loads(body)
    except Exception:
        rpc = {}
    if status != 200 or rpc.get("success") is not True or rpc.get("actual") != 240:
        failures.append(f"POST mismatch: {status} {body}")

    # 3b. A switch is driven by the same control family: the switch endpoint sends
    # opcode 2 with the state as the argument, and the device acknowledges.
    def switch_responder():
        try:
            raw = read_frame(master)
            msg_id, tx_id, payload = parse_frame(raw)
            area, node = struct.unpack("<HH", payload[2:6])
            opcode, argument = struct.unpack("<HI", payload[6:12])
            print(f"node saw switch msg=0x{msg_id:04x} opcode={opcode} arg={argument}")
            if msg_id != 0x0301 or opcode != 2 or argument != 1 or (area, node) != (AREA, 12):
                failures.append(f"unexpected switch control {msg_id} {payload.hex()}")
            os.write(master, frame(0x0302, tx_id, control_response(area, node, opcode, 0, argument)))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"switch responder: {exc}")

    t = threading.Thread(target=switch_responder)
    t.start()
    status, body = http("POST", f"/api/v1/nodes/{AREA}/12/switch", json.dumps({"on": True}))
    t.join(timeout=5)
    print("SWITCH:", status, body)
    try:
        switch = json.loads(body)
    except Exception:
        switch = {}
    if status != 200 or switch.get("opcode") != 2 or switch.get("actual") != 1:
        failures.append(f"switch mismatch: {status} {body}")

    # 4. Timeout path: a request the node never answers is a 504. The request is
    # read and left unanswered, so the pty is clean for the next exchange.
    def node_silent():
        try:
            raw = read_frame(master)
            msg_id, tx_id, _ = parse_frame(raw)
            print(f"node saw (silent) msg=0x{msg_id:04x} tx={tx_id}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"silent reader: {exc}")

    t = threading.Thread(target=node_silent)
    t.start()
    status, body = http("POST", "/api/v1/telemetry/sampling-rate",
                        json.dumps({"interval_ms": 500}))
    t.join(timeout=5)
    print("POST timeout:", status, body)
    if status != 504:
        failures.append(f"expected 504, got {status} {body}")

    # 5. Diagnostics: the gateway asks a device for its smoke, the device
    # answers with its counters and probe masks, and the listing carries it.
    def diag_responder():
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline:
                raw = read_frame(master)
                msg_id, tx_id, payload = parse_frame(raw)
                if msg_id != 0x0401:
                    print(f"diag responder ignoring msg=0x{msg_id:04x}")
                    continue
                print(f"node saw msg=0x{msg_id:04x} tx={tx_id} payload={payload.hex()}")
                area, node = struct.unpack("<HH", payload[2:6])
                mask = struct.unpack("<I", payload[6:10])[0]
                if mask != 255:
                    failures.append(f"unexpected diagnostic mask {mask}")
                # Every probe the board has passed; none failed.
                os.write(master, frame(0x0402, tx_id, diag_response(area, node, 4242, 0x0085, 0x0000)))
                return
            failures.append("diag responder saw no diagnostic request")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"diag responder: {exc}")

    t = threading.Thread(target=diag_responder)
    t.start()
    status, body = http("POST", "/api/v1/nodes/7/1/diagnostics")
    t.join(timeout=5)
    print("DIAG device:", status, body)
    try:
        device_diag = json.loads(body)
    except Exception:
        device_diag = {}
    if status != 200 or device_diag.get("uptime_ms") != 4242 or device_diag.get("probe_ok") != 0x0085 \
       or device_diag.get("probe_fault") != 0:
        failures.append(f"diagnostic mismatch: {status} {body}")

    status, body = http("GET", "/api/v1/nodes")
    print("NODES with diag:", status, body)
    try:
        listed = {(n["area_id"], n["node_id"]): n for n in json.loads(body)}
    except Exception:
        listed = {}
    if listed.get((7, 1), {}).get("diag", {}).get("probe_ok") != 0x0085:
        failures.append(f"the node record does not carry its smoke: {body}")

    # 6. The uplink: a relay dials in, opens a session as a board of this mesh,
    # announces the area it speaks for under it, and pushes a reading for a
    # board the master does not hear directly. The uplink takes no plain frame,
    # even with ALLOW_PLAIN_FRAMES=1, which is the serial link's alone.
    uplink = socket.create_connection((HOST, UPLINK_PORT), timeout=5)
    uplink.sendall(frame(0x0101, 0, announce_payload(7, 2, 9)))
    uplink.sendall(frame(0x0201, 0, reading_payload(9, 6, 11.5, 0, 8000)))
    time.sleep(0.3)
    http("POST", "/api/v1/uplink/pump")
    status, body = http("GET", "/api/v1/diagnostics")
    if json.loads(body).get("children") != 0 or node_temperature(9, 6) is not None:
        failures.append(f"a plain frame on the uplink was taken: {body}")

    relay = Board(k_gw)
    if uplink_session(uplink, relay) is None:
        failures.append("the relay's hello on the uplink was not answered")
    uplink.sendall(relay.next(0x0101, announce_payload(7, 2, 9)))
    uplink.sendall(relay.next(0x0201, reading_payload(9, 5, 12.5, 0, 9000)))
    time.sleep(0.3)

    status, body = http("POST", "/api/v1/uplink/pump")
    print("PUMP:", status, body)

    status, body = http("GET", "/api/v1/nodes")
    print("NODES with uplink:", status, body)
    if node_temperature(9, 5) != 12.5:
        failures.append(f"the uplinked device is missing: {body}")

    status, body = http("GET", "/api/v1/diagnostics")
    try:
        diag = json.loads(body)
    except Exception:
        diag = {}
    if diag.get("children") != 1:
        failures.append(f"the announced child was not recorded: {body}")

    # 7. Down: a command for a device in the child's area is sent down its
    # uplink sealed under its session, and the child's reply comes back up on
    # the same link.
    uplink.settimeout(3.0)

    def uplink_responder():
        try:
            raw = read_socket_frame(uplink)
            msg_id, tx_id, sealed = parse_frame(raw)
            payload = relay.open(msg_id, tx_id, sealed)
            msg_id &= 0x7FFF
            print(f"relay saw msg=0x{msg_id:04x} tx={tx_id} payload={payload.hex()}")
            area, node = struct.unpack("<HH", payload[2:6])
            opcode, argument = struct.unpack("<HI", payload[6:12])
            if msg_id != 0x0301 or opcode != 1 or argument != 300 or (area, node) != (9, 5):
                failures.append(f"unexpected downlink frame {msg_id} {payload.hex()}")
            uplink.sendall(relay.next(0x0302, control_response(area, node, opcode, 0, 280), tx_id))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"uplink responder: {exc}")

    t = threading.Thread(target=uplink_responder)
    t.start()
    status, body = http("POST", "/api/v1/nodes/9/5/sampling-rate", json.dumps({"interval_ms": 300}))
    t.join(timeout=5)
    print("DOWN:", status, body)
    try:
        down = json.loads(body)
    except Exception:
        down = {}
    if status != 202 or down.get("routed") is not True:
        failures.append(f"the downlink command was not routed: {status} {body}")

    # The child's reply is applied when the uplink is drained.
    time.sleep(0.2)
    status, body = http("POST", "/api/v1/uplink/pump")
    print("PUMP down:", status, body)
    uplink.close()

    # Sealed frames: a board says hello, sends under its session, and is still
    # heard after the gateway restarts — the session is kept, and its counter
    # starts past what was kept, so a frame recorded before cannot be replayed.
    board = Board(k_gw)
    while True:
        try:
            os.read(master, 4096)
        except BlockingIOError:
            break
    os.write(master, board.hello())
    deadline = time.time() + 5
    session = None
    while time.time() < deadline and session is None:
        msg_id, _, payload = parse_frame(read_frame(master))
        if msg_id == 18:
            session = payload
    if session is None:
        failures.append("the hello was not answered with a session")
    else:
        board.accept(session)
        sealed_inner = lambda temp: b"\x01" + device(AREA, 11) + struct.pack("<IfI", 1000, temp, 0)
        os.write(master, board.sealed(1, 0x0201, sealed_inner(21.0)))
        time.sleep(0.3)
        if node_temperature(AREA, 11) != 21.0:
            failures.append(f"a sealed frame was not taken: {node_temperature(AREA, 11)}")
        sessions = os.path.join(work, "sessions")
        if not os.path.exists(sessions) or os.stat(sessions).st_mode & 0o777 != 0o600:
            failures.append("the sessions are not kept readable by the gateway alone")

        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        proc = subprocess.Popen([BIN], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        wait_ready(proc)
        os.write(master, board.sealed(128, 0x0201, sealed_inner(22.0)))
        time.sleep(0.3)
        if node_temperature(AREA, 11) is not None:
            failures.append("a counter at or below the kept mark was taken after the restart")
        # A counter is restored at twice the store interval past the kept mark.
        os.write(master, board.sealed(129, 0x0201, sealed_inner(23.5)))
        time.sleep(0.3)
        if node_temperature(AREA, 11) != 23.5:
            failures.append(f"the board was not heard after the restart: {node_temperature(AREA, 11)}")
        os.write(master, board.sealed(129, 0x0201, sealed_inner(99.0)))
        time.sleep(0.3)
        if node_temperature(AREA, 11) != 23.5:
            failures.append("a replayed frame was taken")

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    # A serial link that is not there at start is opened when it appears, and
    # one that goes away (a board on USB resetting) is opened again when it is
    # back, as another device behind the same name.
    late = os.path.join(work, "late-tty")
    late_env = dict(env, SERIAL_PORT=late)
    proc = subprocess.Popen([BIN], env=late_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    wait_ready(proc)
    first_master, first_slave = pty.openpty()
    os.symlink(os.ttyname(first_slave), late)
    time.sleep(2.6)
    os.write(first_master, telemetry_frame(AREA, 21, 18.5, 0, 1000))
    time.sleep(0.3)
    if node_temperature(AREA, 21) != 18.5:
        failures.append("a serial link that appeared after the start was not opened")
    os.close(first_master)
    os.close(first_slave)
    second_master, second_slave = pty.openpty()
    os.remove(late)
    os.symlink(os.ttyname(second_slave), late)
    time.sleep(2.6)
    os.write(second_master, telemetry_frame(AREA, 22, 17.0, 0, 2000))
    deadline = time.time() + 3
    while time.time() < deadline and node_temperature(AREA, 22) != 17.0:
        time.sleep(0.2)
    if node_temperature(AREA, 22) != 17.0:
        failures.append("a serial link that went away and came back was not opened again")
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("\nOK: gateway smoke test passed")


if __name__ == "__main__":
    main()
