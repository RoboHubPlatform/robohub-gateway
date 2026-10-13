#!/usr/bin/env python3
"""The whole RoboHub tree on one machine.

A master (robohub-gateway), a relay (relay-host, on robohub-linux) and a board
(a pseudo-terminal) meet over the same wire:

  board  --uart1/pty-->  relay  --TCP uplink-->  master

It drives a reading up from the board to the master and a command down from the
master to the board, and checks both answers — the tree's read and its write.

The relay is a real program: it dials the master, announces the area it speaks
for, forwards the board's readings, and routes the master's command down the
downstream link and the board's answer back up.

    python3 tools/relay-smoke.py
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

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GATEWAY_BIN = os.environ.get("GATEWAY_BIN", os.path.join(ROOT, "build", "robohub-gateway"))
RELAY_BIN = os.environ.get(
    "RELAY_BIN",
    "/home/miraur/projects/robohub-drivers/examples/gateway/c/relay-host/build/output/relay_host",
)
HOST = "127.0.0.1"
REST_PORT = 18084
UPLINK_PORT = 18085
AREA = 9
NODE = 3


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


def parse_frame(raw: bytes):
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


def read_frame(fd, timeout: float = 3.0) -> bytes:
    deadline = time.time() + timeout
    buf = bytearray()
    while time.time() < deadline:
        try:
            chunk = os.read(fd, 256)
        except (BlockingIOError, OSError):
            time.sleep(0.01)
            continue
        buf += chunk
        if 0 in buf:
            return bytes(buf[:buf.index(0)])
    raise TimeoutError("no frame")


def http(method, path, body=None):
    conn = httpclient.HTTPConnection(HOST, REST_PORT, timeout=5)
    headers = {"Content-Type": "application/json"} if body else {}
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    return resp.status, data


def device(area: int, node: int) -> bytes:
    return b"\x01" + struct.pack("<HH", area, node)


def telemetry(area: int, node: int, temp_c: float, ts_ms: int) -> bytes:
    return b"\x01" + device(area, node) + struct.pack("<IfI", ts_ms, temp_c, 0)


def control_response(area: int, node: int, opcode: int, result: int, argument: int) -> bytes:
    return b"\x01" + device(area, node) + struct.pack("<HHI", opcode, result, argument)


def main():
    failures = []

    # The master's own serial: nothing writes to it, so the relay is the only
    # upstream it has. A pty keeps the port open without a real tty.
    gw_master, gw_slave = pty.openpty()

    env = dict(os.environ)
    env.update({
        "SERIAL_PORT": os.ttyname(gw_slave),
        "REST_PORT": str(REST_PORT),
        "UPLINK_PORT": str(UPLINK_PORT),
        "GATEWAY_MODE": "master",
        "GATEWAY_AREA": "7",
    })
    gateway = subprocess.Popen([GATEWAY_BIN], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    ready = False
    for _ in range(100):
        try:
            status, _ = http("GET", "/healthz")
            if status == 200:
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.05)
    if not ready:
        gateway.kill()
        print("FAIL: the master did not come up")
        print(gateway.stdout.read())
        sys.exit(1)

    # The board's link: the relay opens the slave, this is the board.
    board_master, board_slave = pty.openpty()
    os.set_blocking(board_master, False)

    relay_env = dict(os.environ)
    relay_env.update({
        "RHO_UART1": os.ttyname(board_slave),
        "RHO_PARENT_HOST": HOST,
        "RHO_PARENT_PORT": str(UPLINK_PORT),
    })
    relay = subprocess.Popen([RELAY_BIN], env=relay_env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    # 1. The relay dials the master and announces its area.
    announced = False
    for _ in range(100):
        status, body = http("GET", "/api/v1/diagnostics")
        if status == 200 and json.loads(body).get("children") == 1:
            announced = True
            break
        time.sleep(0.05)
    if not announced:
        failures.append("the relay did not announce itself to the master")

    # 2. The board sends a reading; it reaches the master through the relay.
    os.write(board_master, frame(0x0201, 0, telemetry(AREA, NODE, 20.0, 1000)))
    time.sleep(0.3)
    http("POST", "/api/v1/uplink/pump")
    status, body = http("GET", "/api/v1/nodes")
    print("NODES:", body)
    try:
        listed = {(n["area_id"], n["node_id"]): n for n in json.loads(body)}
    except Exception:
        listed = {}
    if listed.get((AREA, NODE), {}).get("temperature_c") != 20.0:
        failures.append(f"the board's reading did not reach the master: {body}")

    # 3. The master sends a command for the board; the relay routes it down.
    status, body = http("POST", f"/api/v1/nodes/{AREA}/{NODE}/sampling-rate",
                        json.dumps({"interval_ms": 300}))
    print("DOWN:", status, body)
    if status != 202 or json.loads(body).get("routed") is not True:
        failures.append(f"the master did not route the command down: {status} {body}")

    # 4. The board hears the command, on its own link, naming its own device.
    try:
        msg_id, tx_id, payload = parse_frame(read_frame(board_master))
        got_area, got_node = struct.unpack("<HH", payload[2:6])
        opcode, argument = struct.unpack("<HI", payload[6:12])
        print(f"board saw msg=0x{msg_id:04x} tx={tx_id} device={got_area}/{got_node} op={opcode} arg={argument}")
        if msg_id != 0x0301 or (got_area, got_node) != (AREA, NODE) or opcode != 1 or argument != 300:
            failures.append(f"the command the board heard is wrong: {payload.hex()}")
        os.write(board_master, frame(0x0302, tx_id, control_response(got_area, got_node, opcode, 0, 280)))
    except Exception as exc:  # noqa: BLE001
        failures.append(f"the board heard no command: {exc}")

    # 5. The board's answer reaches the master, which applies it.
    time.sleep(0.3)
    status, body = http("POST", "/api/v1/uplink/pump")
    print("PUMP:", body)
    if status != 200 or json.loads(body).get("applied", 0) < 1:
        failures.append(f"the board's answer did not reach the master: {body}")

    gateway.terminate()
    relay.terminate()
    for proc in (gateway, relay):
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("\nOK: the tree works on one machine — a reading up, a command down")


if __name__ == "__main__":
    main()
