#!/usr/bin/env python3
"""End-to-end smoke test for robohub-gateway.

A pseudo-terminal stands in for the node and a UDP socket for PlotJuggler. The
gateway is a subprocess; this drives a telemetry frame and a sampling-rate RPC
through the pty and checks the REST API and the streamed datagrams.

Run it after `rho build`:

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

seq = 0


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


def telemetry_frame(temp_c: float, status: int, ts_ms: int, node_id: int = 1) -> bytes:
    # The payload is the generated Telemetry: a schema version byte, the node id
    # and the fields little-endian, exactly as robohub-transport carries it.
    payload = b"\x01" + struct.pack("<HIfI", node_id, ts_ms, temp_c, status)
    return frame(0x0201, 0, payload)


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
    os.write(master, telemetry_frame(24.31, 0, 124500))
    try:
        datagram = udp.recv(4096).decode()
    except socket.timeout:
        datagram = ""
    print("UDP:", datagram.strip())
    want = {"node_id": 1, "timestamp_ms": 124500, "temperature_c": 24.31, "status": 0}
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
    if status != 200 or latest.get("node_id") != 1 or latest.get("temperature_c") != 24.31 or \
       latest.get("status") != "OK" or latest.get("last_updated_ms") != 124500:
        failures.append(f"GET mismatch: {status} {body}")

    # 2b. A second board is attributed separately.
    os.write(master, telemetry_frame(19.5, 0, 124600, node_id=2))
    time.sleep(0.2)
    status, body = http("GET", "/api/v1/nodes")
    print("NODES:", status, body)
    try:
        listed = {n["node_id"]: n for n in json.loads(body)}
    except Exception:
        listed = {}
    if status != 200 or set(listed) != {1, 2} or listed.get(2, {}).get("temperature_c") != 19.5:
        failures.append(f"nodes mismatch: {status} {body}")

    # 3. RPC: POST sampling-rate, answer the node's 0x0301 with a 0x0302.
    import threading

    def node_responder():
        try:
            raw = read_frame(master)
            msg_id, tx_id, payload = parse_frame(raw)
            print(f"node saw msg=0x{msg_id:04x} tx={tx_id} payload={payload.hex()}")
            # payload[0] is the schema version; the interval follows.
            interval = struct.unpack("<H", payload[1:3])[0] if len(payload) >= 3 else -1
            if msg_id != 0x0301 or payload[0] != 1 or interval != 250:
                failures.append(f"unexpected RPC frame {msg_id} {payload.hex()}")
            os.write(master, frame(0x0302, tx_id, b"\x01" + struct.pack("<H", 240)))
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

    # 4. Timeout path: a request the node never answers is a 504.
    status, body = http("POST", "/api/v1/telemetry/sampling-rate",
                        json.dumps({"interval_ms": 500}))
    print("POST timeout:", status, body)
    if status != 504:
        failures.append(f"expected 504, got {status} {body}")

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
