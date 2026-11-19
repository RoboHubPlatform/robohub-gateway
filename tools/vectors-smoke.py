#!/usr/bin/env python3
"""The gateway's sealed frames against the shared known-answer vectors.

robohub-transport holds the vectors (tests/vectors/secure.json, written once by
its tests/vectors/generate.py from the protocol in include/robohub/secure.h) and
checks the board against them; tests/vectors/secure.json here is an identical
copy, so the gateway's suite runs without the sibling checkout, and the first
check below fails when the two differ. This drives the gateway binary with the
vectors' bytes and compares what it writes, byte for byte:

- a restored session (the vector session, written as the sessions file a
  previous run kept): the SESSION_RESET the gateway sends when the serial link
  opens, the REKEY it sends after the board's sealed reading (the new key
  wrapped under the frame's own counter), and a command — each the vector frame
  exactly; the board's sealed reading, REKEY_ACK (its proof) and acknowledgement
  taken from the vector bytes;
- the handshake: the vector hellos under epoch 0 and epoch 1 answered, with a
  session whose tag is the vector board key's (the gateway's nonce is random,
  so a session cannot be compared whole).

Run it after `rho build`:

    python3 tools/vectors-smoke.py
"""
import hashlib
import hmac
import json
import os
import pty
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smoke  # noqa: E402
from smoke import node_temperature, parse_frame, read_frame, wait_ready  # noqa: E402

HOST = "127.0.0.1"
HTTP_PORT = 18114
UDP_PORT = 19914
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VECTORS = os.path.join(ROOT, "tests", "vectors", "secure.json")

failures = []


def check(ok: bool, message: str) -> None:
    print(("ok   " if ok else "FAIL ") + message)
    if not ok:
        failures.append(message)


def canonical_vectors():
    """The transport's file, beside this checkout or under ROBOHUB_WORKSPACE."""
    candidates = []
    if os.environ.get("ROBOHUB_WORKSPACE"):
        candidates.append(os.path.join(os.environ["ROBOHUB_WORKSPACE"], "robohub-transport"))
    candidates.append(os.path.join(os.path.dirname(ROOT), "robohub-transport"))
    for base in candidates:
        path = os.path.join(base, "tests", "vectors", "secure.json")
        if os.path.exists(path):
            return path
    return None


def drain(master: int) -> None:
    while True:
        try:
            os.read(master, 4096)
        except BlockingIOError:
            return


def next_frame(master: int, timeout: float = 3.0):
    """The next frame the gateway writes, as its encoded bytes (no delimiter)."""
    try:
        return read_frame(master, timeout)
    except TimeoutError:
        return b""


def env_for(work: str, key_file: str, tty: str) -> dict:
    env = dict(os.environ)
    for name in ("ALLOW_PLAIN_FRAMES", "UPLINK_PORT", "GATEWAY_SESSION_FILE", "GATEWAY_ID",
                 "RHO_TLS_CERT", "RHO_TLS_KEY"):
        env.pop(name, None)
    env.update({
        "SERIAL_PORT": tty,
        "REST_PORT": str(HTTP_PORT),
        "RHO_HTTP_ADDR": f"{HOST}:{HTTP_PORT}",
        "PLOTJUGGLER_ADDR": f"{HOST}:{UDP_PORT}",
        "GATEWAY_MODE": "master",
        "GATEWAY_AREA": "7",
        "HOME": work,
        "GATEWAY_KEY_FILE": key_file,
    })
    return env


def stop(proc) -> str:
    if proc.poll() is None:
        proc.terminate()
    try:
        out, _ = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    return out or ""


def prepare(work: str, v: dict) -> str:
    """The gateway's key and a manifest placing the vector board at epoch 1."""
    os.makedirs(work, exist_ok=True)
    key_file = os.path.join(work, "gateway.key")
    with open(key_file, "w") as f:
        f.write(v["keys"]["k_gw"] + "\n")
    os.chmod(key_file, 0o600)
    place = v["placement"]
    nodes = os.path.join(work, "nodes.txt")
    with open(nodes, "w") as f:
        f.write("# uid|area|node|role|epoch|revoked|areas\n")
        f.write(f"{v['keys']['uid']}|{place['area']}|{place['node']}|board|1|0|\n")
    os.chmod(nodes, 0o600)
    return key_file


def keys_entry(uid: str):
    status, body = smoke.http("GET", "/api/v1/keys")
    try:
        for entry in json.loads(body):
            if entry.get("uid") == uid:
                return entry
    except ValueError:
        pass
    return {}


def restored_session(work: str, v: dict) -> None:
    """A gateway that kept the vector session: every frame it seals is known."""
    key_file = prepare(work, v)
    s = v["session"]
    uid = v["keys"]["uid"]
    expiry = int(time.time()) + 3000
    with open(os.path.join(work, "sessions"), "w") as f:
        f.write(f"{s['sid_bytes']}|{uid}|{s['k_up']}|{s['k_down']}|{expiry}|{s['stored_up']}|{s['stored_down']}|serial|0\n")
        f.write(f"serial|{s['sid_bytes']}|\n")
    os.chmod(os.path.join(work, "sessions"), 0o600)

    master, slave = pty.openpty()
    os.set_blocking(master, False)
    proc = subprocess.Popen([smoke.BIN], env=env_for(work, key_file, os.ttyname(slave)),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        wait_ready(proc)
        place = v["placement"]
        # The serial link opens: the board holding the restored session is asked
        # for a new one, sealed under it.
        got = next_frame(master)
        want = bytes.fromhex(v["session_reset"]["frame"])[:-1]
        check(got == want, f"the SESSION_RESET the gateway seals is the vector's ({got.hex()})")

        # The board's sealed reading is taken, and — the manifest naming epoch 1
        # — answered with a REKEY wrapped under the frame's own counter.
        os.write(master, bytes.fromhex(v["reading"]["frame"]))
        got = next_frame(master)
        want = bytes.fromhex(v["rekey"]["frame"])[:-1]
        check(got == want, f"the REKEY the gateway seals is the vector's ({got.hex()})")
        check(node_temperature(place["area"], place["node"]) == v["reading"]["temperature_c"],
              "the vector's sealed reading is opened and taken")

        # The board's REKEY_ACK: its proof is the vector's, and confirms epoch 1.
        os.write(master, bytes.fromhex(v["rekey_ack"]["frame"]))
        deadline = time.time() + 3
        entry = {}
        while time.time() < deadline:
            entry = keys_entry(uid)
            if entry.get("confirmed") == 1:
                break
            time.sleep(0.1)
        check(entry.get("confirmed") == 1 and entry.get("status") == "confirmed",
              f"the vector's REKEY_ACK proves the epoch-1 key ({entry})")

        # A command down, sealed as the vector says, answered with the vector's
        # acknowledgement.
        drain(master)
        seen = {}

        def board():
            seen["frame"] = next_frame(master)
            if seen["frame"]:
                os.write(master, bytes.fromhex(v["control_ack"]["frame"]))

        t = threading.Thread(target=board)
        t.start()
        status, body = smoke.http("POST", f"/api/v1/nodes/{place['area']}/{place['node']}/sampling-rate",
                                  json.dumps({"interval_ms": 250}))
        t.join(timeout=5)
        want = bytes.fromhex(v["control"]["frame"])[:-1]
        check(seen.get("frame") == want, f"the command the gateway seals is the vector's ({seen.get('frame', b'').hex()})")
        try:
            answer = json.loads(body)
        except ValueError:
            answer = {}
        check(status == 200 and answer.get("actual") == v["control_ack"]["actual"],
              f"the vector's sealed acknowledgement answers the command ({status} {body})")
    finally:
        stop(proc)
        os.close(master)
        os.close(slave)


def session_tag_ok(payload: bytes, k_dev: bytes, uid: bytes, nonce_b: bytes) -> bool:
    if len(payload) != 28:
        return False
    sid, nonce_g, lifetime, tag = payload[8:10], payload[10:18], payload[18:20], payload[20:28]
    want = hmac.new(k_dev, b"rh-session1" + uid + nonce_b + sid + nonce_g + lifetime, hashlib.sha256).digest()[:8]
    return payload[:8] == uid and hmac.compare_digest(tag, want)


def handshake(work: str, v: dict) -> None:
    """The vector hellos: the gateway derives the board's keys as the vectors do."""
    key_file = prepare(work, v)
    master, slave = pty.openpty()
    os.set_blocking(master, False)
    proc = subprocess.Popen([smoke.BIN], env=env_for(work, key_file, os.ttyname(slave)),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    uid = bytes.fromhex(v["keys"]["uid"])
    lifetime = struct.pack("<H", v["session"]["lifetime_s"])
    try:
        wait_ready(proc)
        for name, key in (("hello", "k_dev_epoch0"), ("hello_epoch1", "k_dev_epoch1")):
            drain(master)
            os.write(master, bytes.fromhex(v[name]["frame"]))
            payload = b""
            deadline = time.time() + 3
            while time.time() < deadline:
                raw = next_frame(master, max(0.1, deadline - time.time()))
                if not raw:
                    break
                msg_id, _, body = parse_frame(raw)
                if msg_id == 0x0012:
                    payload = body
                    break
            ok = session_tag_ok(payload, bytes.fromhex(v["keys"][key]), uid, bytes.fromhex(v[name]["nonce_b"]))
            check(ok and payload[18:20] == lifetime,
                  f"the vector {name} (epoch {v[name]['epoch']}) is answered with a session under the vector key")
        entry = keys_entry(v["keys"]["uid"])
        check(entry.get("confirmed") == 1, f"the epoch-1 hello confirms the epoch-1 key ({entry})")
    finally:
        stop(proc)
        os.close(master)
        os.close(slave)


def main():
    with open(VECTORS) as f:
        text = f.read()
    v = json.loads(text)
    canonical = canonical_vectors()
    if canonical is None:
        print("SKIP the copy's match: no robohub-transport checkout beside this one")
    else:
        with open(canonical) as f:
            check(f.read() == text, f"tests/vectors/secure.json is the same as {canonical}")

    smoke.HTTP_PORT = HTTP_PORT
    smoke.TOKEN = ""
    work = tempfile.mkdtemp(prefix="gateway-vectors-")
    try:
        smoke.TOKEN = smoke.mint_token(work)
        os.environ["RHO_AUTH_PUBLIC_KEY"] = os.path.join(work, ".rho", "auth", "public.pem")
        restored_session(os.path.join(work, "restored"), v)
        handshake(os.path.join(work, "handshake"), v)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("\nOK: the gateway's sealed frames are the vectors'")


if __name__ == "__main__":
    main()
