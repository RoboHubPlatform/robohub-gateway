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
import http.client as httpclient
import json
import os
import pty
import socket
import struct
import subprocess
import sys
import time

BIN = os.environ.get("GATEWAY_BIN", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "build", "robohub-gateway"))
HOST = "127.0.0.1"
HTTP_PORT = 18080
UDP_PORT = 19870
UPLINK_PORT = 18081
AREA = 7
SAMPLING_RATE_OPCODE = 1

sequence = 0


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
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    return resp.status, data


def main():
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
    })
    proc = subprocess.Popen([BIN], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)

    failures = []

    # Wait for the listener.
    ready = False
    last = None
    for _ in range(100):
        try:
            status, _ = http("GET", "/healthz")
            if status == 200:
                ready = True
                break
            last = f"status {status}"
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(0.05)
    if not ready:
        proc.kill()
        print("FAIL: server did not become ready; last:", last)
        print(proc.stdout.read())
        sys.exit(1)

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

    # 3. Control: POST sampling-rate, answer the gateway's 0x0301 with a 0x0302.
    import threading

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
    if status != 200 or rpc.get("success") is not True or rpc.get("actual_interval_ms") != 240:
        failures.append(f"POST mismatch: {status} {body}")

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
    status, body = http("GET", "/api/v1/nodes/7/1/diagnostics")
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

    # 6. The uplink: a relay dials in, announces the area it speaks for, and
    # pushes a reading for a board the master does not hear directly.
    uplink = socket.create_connection((HOST, UPLINK_PORT), timeout=5)
    uplink.sendall(frame(0x0101, 0, announce_payload(7, 2, 9)))
    uplink.sendall(frame(0x0201, 0, reading_payload(9, 5, 12.5, 0, 9000)))
    time.sleep(0.3)

    status, body = http("POST", "/api/v1/uplink/pump")
    print("PUMP:", status, body)
    try:
        pumped = json.loads(body)
    except Exception:
        pumped = {}
    if status != 200 or pumped.get("applied") != 2:
        failures.append(f"the uplink frames were not applied: {status} {body}")

    status, body = http("GET", "/api/v1/nodes")
    print("NODES with uplink:", status, body)
    try:
        listed = {(n["area_id"], n["node_id"]): n for n in json.loads(body)}
    except Exception:
        listed = {}
    if listed.get((9, 5), {}).get("temperature_c") != 12.5:
        failures.append(f"the uplinked device is missing: {body}")

    status, body = http("GET", "/api/v1/diagnostics")
    try:
        diag = json.loads(body)
    except Exception:
        diag = {}
    if diag.get("children") != 1:
        failures.append(f"the announced child was not recorded: {body}")

    # 7. Down: a command for a device in the child's area is sent down its
    # uplink, and the child's reply comes back up on the same link.
    uplink.settimeout(3.0)

    def uplink_responder():
        try:
            raw = read_socket_frame(uplink)
            msg_id, tx_id, payload = parse_frame(raw)
            print(f"relay saw msg=0x{msg_id:04x} tx={tx_id} payload={payload.hex()}")
            area, node = struct.unpack("<HH", payload[2:6])
            opcode, argument = struct.unpack("<HI", payload[6:12])
            if msg_id != 0x0301 or opcode != 1 or argument != 300 or (area, node) != (9, 5):
                failures.append(f"unexpected downlink frame {msg_id} {payload.hex()}")
            uplink.sendall(frame(0x0302, tx_id, control_response(area, node, opcode, 0, 280)))
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
