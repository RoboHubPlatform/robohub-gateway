#!/usr/bin/env python3
"""Who may call the gateway, what a request must say, and how it is served.

The gateway runs with a pseudo-terminal as its serial link, and this checks:

- scopes: a token holding the wrong scope gets 403 on every route of each class
  — a reader on the routes that act, a commander on the routes that read, a
  token for another gateway on both — and no token at all is not let in;
- a board's hardware smoke is an action: it is a POST that needs the command
  scope, a reader's request is refused before anything reaches the board, and
  the old GET is not a way round it;
- acknowledging the outbox is a change: it needs `gateway:<id>:ack` (a
  collector holds read and ack and commands nothing), and the gateway's own
  check refuses a token that reads this gateway and acks another;
- the device API: switching and the sampling rate refuse a request that does
  not say what to do (a missing or malformed state, interval or device) rather
  than guessing, and do what a well-formed one asks;
- a device is registered only at an address the gateway should call (an
  http(s) URL of a host and port; not loopback, link-local, unspecified or a
  numeric host spelled otherwise than in four octets);
- a network switch is recorded as the device reports it, against a stand-in
  Shelly plug: confirmed (200), a stuck relay (502, the reported state kept),
  and a plug that takes the command but gives no status (202, pending);
- TLS: with RHO_TLS_CERT and RHO_TLS_KEY the API is served over HTTPS only — a
  plain request on the same port gets nothing — and a certificate that cannot
  be loaded, or half of the pair, stops the gateway instead of serving plain.

Run it after `rho build`:

    python3 tools/access-smoke.py
"""
import http.client as httpclient
import http.server
import json
import os
import pty
import shutil
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smoke  # noqa: E402
from smoke import control_response, frame, parse_frame, read_frame  # noqa: E402

HOST = "127.0.0.1"
HTTP_PORT = 18110
UPLINK_PORT = 18111
UDP_PORT = 19910
TLS_PORT = 18112
AREA = 7

failures = []


def check(ok: bool, message: str) -> None:
    print(("ok   " if ok else "FAIL ") + message)
    if not ok:
        failures.append(message)


def rho() -> str:
    return smoke.RHO if os.path.exists(smoke.RHO) else "rho"


def keygen(home: str) -> str:
    keys = os.path.join(home, ".rho", "auth")
    os.makedirs(keys, exist_ok=True)
    subprocess.run([rho(), "auth", "keygen", "--dir", keys, "--force"], check=True, capture_output=True, text=True)
    return os.path.join(keys, "private.pem")


def ticket(private: str, scopes: str) -> str:
    return subprocess.run([rho(), "auth", "ticket", "--private", private, "--issuer", "keeper",
                           "--audience", "gateway", "--subject", "access-smoke", "--scopes", scopes,
                           "--ttl", "1h"], check=True, capture_output=True, text=True).stdout.strip()


def call(method, path, token, body=None, port=HTTP_PORT):
    conn = httpclient.HTTPConnection(HOST, port, timeout=5)
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode(errors="replace")
    conn.close()
    return resp.status, data


def start(env):
    proc = subprocess.Popen([smoke.BIN], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    smoke.wait_ready(proc)
    return proc


def stop(proc) -> str:
    if proc.poll() is None:
        proc.terminate()
    try:
        out, _ = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    return out or ""


def drain(master: int) -> None:
    while True:
        try:
            os.read(master, 4096)
        except BlockingIOError:
            return


def serial_quiet(master: int, wait: float = 0.4) -> bool:
    """Whether nothing was written down the serial link for a moment."""
    time.sleep(wait)
    try:
        return len(os.read(master, 4096)) == 0
    except BlockingIOError:
        return True


# The routes, by the scope they need. Every one is called with a body that would
# be well formed, so a refusal is the scope's and not the request's.
READ_ROUTES = [
    ("GET", "/", None),
    ("GET", "/frames/5", None),
    ("GET", "/export/frames/5/csv", None),
    ("GET", "/api/v1/telemetry/temperature", None),
    ("GET", "/api/v1/telemetry/power", None),
    ("GET", "/api/v1/telemetry/state", None),
    ("GET", "/api/v1/telemetry/outbox", None),
    ("GET", "/api/v1/telemetry/outbox/status", None),
    ("GET", "/api/v1/nodes", None),
    ("GET", "/api/v1/diagnostics", None),
    ("GET", "/api/v1/keys", None),
    ("GET", "/api/v1/devices", None),
]
ACK_ROUTES = [
    ("POST", "/api/v1/telemetry/ack", json.dumps({"seq": 0})),
]
COMMAND_ROUTES = [
    ("POST", f"/api/v1/nodes/{AREA}/1/sampling-rate", json.dumps({"interval_ms": 250})),
    ("POST", f"/api/v1/nodes/{AREA}/1/command", json.dumps({"opcode": 1, "argument": 250})),
    ("POST", f"/api/v1/nodes/{AREA}/1/switch", json.dumps({"on": True})),
    ("POST", f"/api/v1/nodes/{AREA}/1/diagnostics", ""),
    ("POST", "/api/v1/telemetry/sampling-rate", json.dumps({"interval_ms": 250})),
    ("POST", "/api/v1/frames", "00"),
    ("POST", "/api/v1/devices", "name=plug&address=http://192.0.2.10&transport=http"),
    ("POST", "/api/v1/devices/1/poll", ""),
    ("POST", "/api/v1/devices/1/switch", "state=on"),
    ("POST", "/api/v1/uplink/pump", ""),
]


def refused_everywhere(routes, token, master):
    """The routes that did not answer 403 to `token`, and whether anything was
    written to the board meanwhile."""
    drain(master)
    let_through = []
    for method, path, body in routes:
        status, _ = call(method, path, token, body)
        if status != 403:
            let_through.append(f"{method} {path} -> {status}")
    return let_through, serial_quiet(master, 0.2)


def answer_once(master: int, msg_id_wanted: int, reply, seen: dict):
    """A board on the serial link: read one frame, record it, and answer it."""
    def responder():
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline:
                msg_id, tx_id, payload = parse_frame(read_frame(master))
                if msg_id != msg_id_wanted:
                    continue
                seen["payload"] = payload
                os.write(master, frame(msg_id_wanted + 1, tx_id, reply(payload)))
                return
        except Exception as exc:  # noqa: BLE001
            seen["error"] = str(exc)
    t = threading.Thread(target=responder)
    t.start()
    return t


def control_reply(payload: bytes) -> bytes:
    area, node = struct.unpack("<HH", payload[2:6])
    opcode, argument = struct.unpack("<HI", payload[6:12])
    return control_response(area, node, opcode, 0, argument)


def diag_reply(payload: bytes) -> bytes:
    area, node = struct.unpack("<HH", payload[2:6])
    return smoke.diag_response(area, node, 4242, 0x0085, 0)


def scopes_and_device_api(work: str, private: str) -> None:
    reader = ticket(private, "gateway:default:read")
    commander = ticket(private, "gateway:default:command")
    stranger = ticket(private, "gateway:other:*")
    elsewhere = ticket(private, "gateway:default:read,gateway:other:command")
    collector = ticket(private, "gateway:default:read,gateway:default:ack")
    acks_elsewhere = ticket(private, "gateway:default:read,gateway:other:ack")

    master, slave = pty.openpty()
    os.set_blocking(master, False)
    env = dict(os.environ)
    env.pop("GATEWAY_ID", None)
    env.pop("RHO_TLS_CERT", None)
    env.pop("RHO_TLS_KEY", None)
    env.update({
        "SERIAL_PORT": os.ttyname(slave),
        "REST_PORT": str(HTTP_PORT),
        "RHO_HTTP_ADDR": f"{HOST}:{HTTP_PORT}",
        "UPLINK_PORT": str(UPLINK_PORT),
        "PLOTJUGGLER_ADDR": f"{HOST}:{UDP_PORT}",
        "GATEWAY_MODE": "master",
        "GATEWAY_AREA": str(AREA),
        "HOME": work,
        "RHO_AUTH_PUBLIC_KEY": os.path.join(work, ".rho", "auth", "public.pem"),
        # A board being brought up speaks plain frames: what is checked here is
        # who may ask, not how the board answers.
        "ALLOW_PLAIN_FRAMES": "1",
    })
    smoke.HTTP_PORT = HTTP_PORT
    proc = start(env)
    try:
        # --- Scopes, by route class ----------------------------------------
        # @verifies GW-0008, GW-API-0001
        through, quiet = refused_everywhere(COMMAND_ROUTES, reader, master)
        check(not through and quiet, f"a read-only token gets 403 on every route that acts {through}")
        through, _ = refused_everywhere(READ_ROUTES + ACK_ROUTES, commander, master)
        check(not through, f"a command-only token gets 403 on every route that reads or acknowledges {through}")
        through, quiet = refused_everywhere(READ_ROUTES + ACK_ROUTES + COMMAND_ROUTES, stranger, master)
        check(not through and quiet, f"a token for another gateway gets 403 on every route {through}")
        let_in = [f"{m} {p}" for m, p, b in READ_ROUTES + ACK_ROUTES + COMMAND_ROUTES if call(m, p, "", b)[0] not in (401, 403)]
        check(not let_in, f"no token is let in anywhere {let_in}")
        not_ok = [f"{m} {p} -> {s}" for m, p, b in READ_ROUTES
                  for s in [call(m, p, reader, b)[0]] if s != 200]
        check(not not_ok, f"the read token is answered on every route that reads {not_ok}")

        # --- Acknowledging the outbox is a change --------------------------
        # @verifies GW-API-0001, GW-TEL-0003
        through, quiet = refused_everywhere(ACK_ROUTES, reader, master)
        check(not through, f"a read-only token cannot acknowledge the outbox {through}")
        # Reading this gateway and acknowledging another passes the middleware's
        # read-only rule, so the refusal here is the gateway's own.
        through, quiet = refused_everywhere(ACK_ROUTES, acks_elsewhere, master)
        check(not through, f"reading this gateway is not enough to acknowledge it {through}")
        status, body = call("POST", "/api/v1/telemetry/ack", collector, json.dumps({"seq": 0}))
        check(status == 200 and '"ack"' in body, f"a collector (read and ack) acknowledges ({status} {body})")
        status, _ = call("GET", "/api/v1/telemetry/outbox", collector)
        check(status == 200, f"and reads the outbox ({status})")
        through, quiet = refused_everywhere(COMMAND_ROUTES, collector, master)
        check(not through and quiet, f"and commands nothing {through}")
        status, _ = call("POST", "/api/v1/telemetry/ack", ticket(private, "gateway:default:*"), json.dumps({"seq": 0}))
        check(status == 200, f"Mesh's gateway:<id>:* token still acknowledges ({status})")

        # --- A board's hardware smoke is an action -------------------------
        # @verifies GW-API-0001, GW-API-0003
        drain(master)
        status, _ = call("POST", f"/api/v1/nodes/{AREA}/1/diagnostics", reader, "")
        check(status == 403 and serial_quiet(master),
              f"a read-only token cannot make a board run its hardware smoke ({status})")
        # A token that reads this gateway and commands another one passes the
        # middleware's read-only rule, so the refusal here is the gateway's own.
        status, _ = call("POST", f"/api/v1/nodes/{AREA}/1/diagnostics", elsewhere, "")
        check(status == 403 and serial_quiet(master),
              f"reading this gateway is not enough even with a command scope for another ({status})")
        # A GET of the old path is not the smoke: nothing reaches the board and
        # no diagnostic comes back (the path falls to the gateway's page).
        status, body = call("GET", f"/api/v1/nodes/{AREA}/1/diagnostics", reader)
        check(serial_quiet(master) and "probe_ok" not in body,
              f"the smoke is not run by a GET ({status})")
        status, body = call("GET", f"/api/v1/nodes/{AREA}/1/diagnostics", commander)
        check(serial_quiet(master) and "probe_ok" not in body,
              f"not even with the command scope ({status})")
        drain(master)
        seen = {}
        t = answer_once(master, 0x0401, diag_reply, seen)
        status, body = call("POST", f"/api/v1/nodes/{AREA}/1/diagnostics", commander, "")
        t.join(timeout=5)
        try:
            diag = json.loads(body)
        except ValueError:
            diag = {}
        check(status == 200 and "payload" in seen and diag.get("probe_ok") == 0x0085,
              f"a command token runs the smoke and is answered with it ({status} {body})")

        # --- The device API: what a request must say -----------------------
        # @verifies GW-API-0004
        drain(master)
        bad_rates = [
            (f"/api/v1/nodes/{AREA}/1/sampling-rate", ""),
            (f"/api/v1/nodes/{AREA}/1/sampling-rate", json.dumps({})),
            (f"/api/v1/nodes/{AREA}/1/sampling-rate", json.dumps({"interval_ms": 0})),
            (f"/api/v1/nodes/{AREA}/1/sampling-rate", json.dumps({"interval_ms": -5})),
            (f"/api/v1/nodes/{AREA}/1/sampling-rate", json.dumps({"interval_ms": "fast"})),
            (f"/api/v1/nodes/{AREA}/1/sampling-rate", json.dumps({"interval_ms": 99999999999})),
            ("/api/v1/nodes/0/1/sampling-rate", json.dumps({"interval_ms": 250})),
            ("/api/v1/nodes/x/1/sampling-rate", json.dumps({"interval_ms": 250})),
            ("/api/v1/telemetry/sampling-rate", json.dumps({"interval_ms": 0})),
        ]
        wrong = [f"{p} {b!r} -> {s}" for p, b in bad_rates for s in [call("POST", p, commander, b)[0]] if s != 400]
        check(not wrong and serial_quiet(master), f"a sampling rate without a positive interval or a device is refused {wrong}")
        bad_switches = [
            (f"/api/v1/nodes/{AREA}/1/switch", ""),
            (f"/api/v1/nodes/{AREA}/1/switch", json.dumps({})),
            (f"/api/v1/nodes/{AREA}/1/switch", json.dumps({"on": "maybe"})),
            (f"/api/v1/nodes/{AREA}/1/switch", json.dumps({"on": 2})),
            (f"/api/v1/nodes/{AREA}/1/switch", json.dumps({"online": True})),
            ("/api/v1/nodes/0/1/switch", json.dumps({"on": True})),
            (f"/api/v1/nodes/{AREA}/1/command", json.dumps({"argument": 5})),
        ]
        wrong = [f"{p} {b!r} -> {s}" for p, b in bad_switches for s in [call("POST", p, commander, b)[0]] if s != 400]
        check(not wrong and serial_quiet(master), f"a switch that does not say on or off is refused, not taken as off {wrong}")

        for body, want in ((json.dumps({"interval_ms": 250}), 250),):
            drain(master)
            seen = {}
            t = answer_once(master, 0x0301, control_reply, seen)
            status, answer = call("POST", f"/api/v1/nodes/{AREA}/1/sampling-rate", commander, body)
            t.join(timeout=5)
            sent = struct.unpack("<HI", seen["payload"][6:12]) if "payload" in seen else None
            check(status == 200 and sent == (1, want), f"a well-formed sampling rate reaches the board ({status} {answer} {sent})")
        for on, want in ((True, 1), (False, 0)):
            drain(master)
            seen = {}
            t = answer_once(master, 0x0301, control_reply, seen)
            status, answer = call("POST", f"/api/v1/nodes/{AREA}/1/switch", commander, json.dumps({"on": on}))
            t.join(timeout=5)
            sent = struct.unpack("<HI", seen["payload"][6:12]) if "payload" in seen else None
            # @verifies GW-TEL-0008
            check(status == 200 and sent == (2, want) and '"confirmed":true' in answer and
                  f'"state":"{"on" if on else "off"}"' in answer,
                  f"switching {'on' if on else 'off'} sends that state and answers the state the board reached ({status} {answer} {sent})")

        # A network device: registered, then switched only to on or off.
        status, body = call("POST", "/api/v1/devices", commander, "address=http://192.0.2.10")
        check(status == 400, f"a device without a name is not registered ({status})")
        # @verifies GW-API-0005
        bad_addresses = [
            "ftp://192.0.2.10", "file:///etc/passwd", "gopher://192.0.2.10:70", "192.0.2.10",
            "http://", "http://192.0.2.10/rpc", "http://192.0.2.10?x=1", "http://user:pw@192.0.2.10",
            "http://192.0.2.10:0", "http://192.0.2.10:70000", "http://192.0.2.10:http",
            "http://127.0.0.1:9", "http://localhost:8080", "http://LOCALHOST", "http://[::1]:80",
            "http://keeper.localhost", "http://0.0.0.0:19090", "http://169.254.169.254",
            "http://[fe80::1]", "http://2130706433", "http://0x7f.1", "http://127.1",
            "http://010.0.0.1", "http://bad_host", "http://a b",
        ]
        wrong = [f"{a} -> {s}" for a in bad_addresses
                 for s in [call("POST", "/api/v1/devices", commander,
                                "name=x&transport=http&address=" + urllib.parse.quote(a, safe=""))[0]] if s != 400]
        check(not wrong, f"an address the gateway should not call is refused {wrong}")
        status, _ = call("POST", "/api/v1/devices", commander, "name=x&transport=telnet&address=http://192.0.2.10")
        check(status == 400, f"a transport other than http or mqtt is refused ({status})")
        status, _ = call("POST", "/api/v1/devices", commander, "name=x&transport=mqtt&address=shellies/%23")
        check(status == 400, f"an MQTT prefix with a wildcard is refused ({status})")
        good = ["http://192.168.1.40", "http://10.0.0.7:8080/", "https://plug-kitchen.lan", "http://[fd00::12]:80",
                "http://shelly-plug.local"]
        wrong = [f"{a} -> {s} {b}" for i, a in enumerate(good)
                 for s, b in [call("POST", "/api/v1/devices", commander,
                                   f"name=lan{i}&transport=http&device_id=lan-{i}&address=" + urllib.parse.quote(a, safe=""))]
                 if s != 201]
        check(not wrong, f"a device on the LAN, by address or name, is registered {wrong}")
        _, listed = call("GET", "/api/v1/devices", reader)
        check('"http://10.0.0.7:8080"' in listed, "a trailing slash is not kept, and an encoded address is stored decoded")
        status, _ = call("POST", "/api/v1/devices", commander, "name=bus&transport=mqtt&address=shellyplug-01&device_id=bus-1")
        check(status == 201, f"an MQTT device is registered by its topic prefix ({status})")
        status, body = call("POST", "/api/v1/devices", commander,
                            "name=plug&address=http://192.0.2.10&transport=http&device_id=plug-1")
        check(status == 201, f"a device is registered ({status} {body})")
        status, body = call("POST", "/api/v1/devices/1/switch", commander, "state=banana")
        _, listed = call("GET", "/api/v1/devices", reader)
        check(status == 400 and "banana" not in listed,
              f"a network switch to anything but on or off is refused and not recorded ({status} {body})")
        status, _ = call("POST", "/api/v1/devices/99/switch", commander, "state=on")
        check(status == 404, f"switching a device that is not registered is 404 ({status})")
        status, _ = call("POST", "/api/v1/devices/99/poll", commander, "")
        check(status == 404, f"polling a device that is not registered is 404 ({status})")
    finally:
        stop(proc)
        os.close(master)
        os.close(slave)


class Plug:
    """A Shelly Plug's Gen2 RPC, as much of it as the gateway calls."""
    output = False
    stuck = False       # takes Switch.Set, but the relay does not move
    no_status = False   # takes Switch.Set, then gives no status


class PlugHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path == "/rpc/Switch.Set":
            body = {"was_on": Plug.output}
            if not Plug.stuck:
                Plug.output = query.get("on", ["false"])[0] == "true"
        elif url.path == "/rpc/Switch.GetStatus" and not Plug.no_status:
            body = {"id": 0, "output": Plug.output, "apower": 0.0}
        elif url.path == "/rpc/Shelly.GetStatus" and not Plug.no_status:
            body = {"switch:0": {"id": 0, "output": Plug.output, "apower": 3.5}}
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def network_switch(work: str, private: str) -> None:
    """A network switch, recorded as the device reports it. The stand-in plug
    listens on the loopback, so this gateway is started with
    GATEWAY_DEVICE_ALLOW_LOOPBACK=1, the way a simulator on the gateway's own
    machine is registered."""
    plug = http.server.ThreadingHTTPServer((HOST, 0), PlugHandler)
    threading.Thread(target=plug.serve_forever, daemon=True).start()
    plug_url = f"http://{HOST}:{plug.server_address[1]}"
    commander = ticket(private, "gateway:default:command")
    reader = ticket(private, "gateway:default:read")
    env = dict(os.environ)
    for name in ("GATEWAY_ID", "RHO_TLS_CERT", "RHO_TLS_KEY"):
        env.pop(name, None)
    env.update({
        "SERIAL_PORT": os.path.join(work, "no-tty"),
        "REST_PORT": str(HTTP_PORT),
        "RHO_HTTP_ADDR": f"{HOST}:{HTTP_PORT}",
        "UPLINK_PORT": "0",
        "PLOTJUGGLER_ADDR": f"{HOST}:{UDP_PORT}",
        "GATEWAY_MODE": "master",
        "GATEWAY_AREA": str(AREA),
        "HOME": work,
        "RHO_AUTH_PUBLIC_KEY": os.path.join(work, ".rho", "auth", "public.pem"),
        "OUTBOX_PATH": os.path.join(work, "switch-outbox.db"),
        "GATEWAY_DEVICE_ALLOW_LOOPBACK": "1",
    })
    smoke.HTTP_PORT = HTTP_PORT
    proc = start(env)
    try:
        def device(index):
            _, listed = call("GET", "/api/v1/devices", reader)
            try:
                return [d for d in json.loads(listed) if d.get("id") == index][0]
            except (ValueError, IndexError):
                return {}

        # @verifies GW-TEL-0008, GW-TEL-0007
        status, body = call("POST", "/api/v1/devices", commander,
                            "name=plug&transport=http&device_id=plug-sim&address=" + urllib.parse.quote(plug_url, safe=""))
        check(status == 201, f"a simulator on the gateway's machine is registered when loopback is allowed ({status} {body})")
        Plug.output, Plug.stuck, Plug.no_status = False, False, False
        status, body = call("POST", "/api/v1/devices/1/switch", commander, "state=on")
        answer = json.loads(body) if body.startswith("{") else {}
        check(status == 200 and answer.get("confirmed") is True and answer.get("device", {}).get("state") == "on",
              f"a plug that switches is confirmed from its status read back ({status} {body})")

        Plug.stuck = True
        status, body = call("POST", "/api/v1/devices/1/switch", commander, "state=off")
        check(status == 502 and "reports on" in body and device(1).get("state") == "on",
              f"a stuck relay is refused and the state it reports is kept, not the one asked ({status} {body})")

        Plug.stuck, Plug.no_status = False, True
        status, body = call("POST", "/api/v1/devices/1/switch", commander, "state=off")
        listed = device(1)
        check(status == 202 and '"confirmed":false' in body and listed.get("state") == "on" and listed.get("requested") == "off",
              f"a plug that gives no status is pending: the request kept, the last reported state unchanged ({status} {listed})")
        Plug.no_status = False
        status, body = call("POST", "/api/v1/devices/1/poll", commander, "")
        listed = device(1)
        check(status == 200 and listed.get("state") == "off" and listed.get("requested") == "",
              f"a poll that reads the plug ends the wait with what it reports ({status} {listed})")

        status, _ = call("POST", "/api/v1/devices", commander, "name=bus&transport=mqtt&address=shellyplug-01&device_id=bus-1")
        status_switch, _ = call("POST", "/api/v1/devices/2/switch", commander, "state=on")
        status_poll, _ = call("POST", "/api/v1/devices/2/poll", commander, "")
        check(status == 201 and status_switch == 409 and status_poll == 409,
              f"an MQTT device is not switched or polled over HTTP by its topic ({status} {status_switch} {status_poll})")
    finally:
        stop(proc)
        plug.shutdown()
        plug.server_close()


def https_call(path, token, cafile):
    context = ssl.create_default_context(cafile=cafile)
    conn = httpclient.HTTPSConnection(HOST, TLS_PORT, timeout=5, context=context)
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode(errors="replace")
    conn.close()
    return resp.status, data


def tls(work: str, private: str) -> None:
    if shutil.which("openssl") is None:
        print("SKIP TLS: openssl is not installed")
        return
    cert = os.path.join(work, "gateway.crt")
    key = os.path.join(work, "gateway.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                    "-nodes", "-keyout", key, "-out", cert, "-days", "1", "-subj", "/CN=127.0.0.1",
                    "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True)
    os.chmod(key, 0o600)
    reader = ticket(private, "gateway:default:read")
    env = dict(os.environ)
    env.pop("GATEWAY_ID", None)
    env.pop("ALLOW_PLAIN_FRAMES", None)
    env.update({
        "SERIAL_PORT": os.path.join(work, "no-tty"),
        "REST_PORT": str(TLS_PORT),
        "RHO_HTTP_ADDR": f"{HOST}:{TLS_PORT}",
        "UPLINK_PORT": "0",
        "PLOTJUGGLER_ADDR": f"{HOST}:{UDP_PORT}",
        "GATEWAY_MODE": "master",
        "GATEWAY_AREA": str(AREA),
        "HOME": work,
        "RHO_AUTH_PUBLIC_KEY": os.path.join(work, ".rho", "auth", "public.pem"),
        "RHO_TLS_CERT": cert,
        "RHO_TLS_KEY": key,
    })
    proc = subprocess.Popen([smoke.BIN], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        # @verifies GW-0013
        ready = False
        for _ in range(100):
            try:
                if https_call("/healthz", "", cert)[0] == 200:
                    ready = True
                    break
            except (OSError, ssl.SSLError):
                time.sleep(0.05)
        check(ready, "with a certificate and key the gateway answers over HTTPS")
        status, body = https_call("/api/v1/nodes", reader, cert)
        check(status == 200 and body.startswith("["), f"the API is served over HTTPS with a token ({status})")
        try:
            status, body = call("GET", "/api/v1/nodes", reader, port=TLS_PORT)
        except (OSError, httpclient.HTTPException) as exc:
            status, body = 0, str(exc)
        check(status != 200 and not body.startswith("["),
              f"a plain HTTP request on the TLS port is refused and served nothing ({status} {body[:60]!r})")
        try:
            status, _ = call("GET", "/healthz", "", port=TLS_PORT)
        except (OSError, httpclient.HTTPException):
            status = 0
        check(status != 200, f"not even the health check is answered in the clear ({status})")
    finally:
        stop(proc)

    # A pair that cannot be served is a stopped gateway, not a plain one.
    for name, overrides in (
        ("a certificate that cannot be loaded", {"RHO_TLS_CERT": os.path.join(work, "missing.crt")}),
        ("a certificate without its key", {"RHO_TLS_KEY": ""}),
        ("a key without its certificate", {"RHO_TLS_CERT": ""}),
    ):
        broken = dict(env, **overrides)
        proc = subprocess.Popen([smoke.BIN], env=broken, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        plain = 0
        try:
            plain, _ = call("GET", "/healthz", "", port=TLS_PORT)
        except (OSError, httpclient.HTTPException):
            plain = 0
        stopped = proc.poll() is not None and proc.returncode != 0
        out = stop(proc)
        check(stopped and plain != 200, f"{name} stops the gateway rather than serving plain HTTP "
              f"(exit {proc.returncode}, plain {plain}) {out.strip().splitlines()[-1:] if out else ''}")


def main():
    work = tempfile.mkdtemp(prefix="gateway-access-")
    private = keygen(work)
    try:
        scopes_and_device_api(work, private)
        network_switch(work, private)
        tls(work, private)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("\nOK: the gateway's access rules hold")


if __name__ == "__main__":
    main()
