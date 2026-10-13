# robohub-gateway

The edge gateway for RoboHub firmware nodes. It reads COBS frames from a serial
link, keeps each board's latest telemetry, streams every reading to PlotJuggler
over UDP, and serves the same state — plus the RPC that changes a node's
sampling rate — over HTTP.

It is a Rho application. `rho build` generates a Go server from the `.rho`
sources; the host primitives it needs (serial, UDP, the clock, shared state,
node attribution, transaction correlation) are one port, `gateway`, named in
`module.yml`.

## Modes

A gateway is one stack with a declared **mode** (`GATEWAY_MODE`), so the same
code is each without a rebuild:

| Mode | Senses | Downstream | Uplink |
|---|---|---|---|
| `node` | yes | — | serial |
| `edge` | yes | serial | serial or TCP |
| `relay` | no | serial or TCP | serial or TCP |
| `master` | no | serial or TCP | — |

This service is used today as the **`master`** (or a `relay`): it hears boards on
its link, keeps each device's latest values, and serves them. The uplink — a
gateway dialling a parent and pushing readings up, with commands routed down by
device id — is the next phase; the L1 schema (`schemas/uplink.rho`) and the two
levels' message families are already declared. The full design is
[docs/architecture.md](docs/architecture.md).

## Build and run

```sh
rho build .          # generate build/generated/go-server and build the binary
rho run .            # build, then start the gateway
```

`rho run` starts the service; the environment says where the link, the collector
and the listener are:

| Variable | Default | Meaning |
|---|---|---|
| `SERIAL_PORT` | `/dev/ttyACM0` | the node's serial device |
| `SERIAL_BAUD` | `115200` | the line speed |
| `PLOTJUGGLER_ADDR` | `127.0.0.1:9870` | the UDP collector, `host:port` |
| `REST_PORT` | `8080` | the REST listener, a port; `RHO_HTTP_ADDR` overrides it |
| `GATEWAY_MODE` | `master` | how it is deployed: `node`, `edge`, `relay` or `master` |
| `GATEWAY_AREA` | `1` | the area it speaks for, and numbers its devices in |
| `UPLINK_PORT` | `0` | the port a relay dials in on; `0` listens on none |

## REST API

### `GET /api/v1/telemetry/temperature`

The latest telemetry the gateway has seen, from whichever device sent it last.

```json
{ "area_id": 7, "node_id": 1, "temperature_c": 24.38, "status": "OK", "last_updated_ms": 186095 }
```

Before any frame arrives the reading is `0.00`, the status `OK` and the
timestamp `0` — a zero reading, not an error.

### `GET /api/v1/nodes`

Every device a frame has come from, each with its newest values. A reading names
its own device, so a gateway that hears several — directly or through an edge —
attributes them without the link telling it. A device is an area and a node, so
node 2 in area 7 and node 2 in area 9 are two devices.

```json
[
  { "area_id": 7, "node_id": 1, "temperature_c": 24.38, "status": "OK", "last_updated_ms": 186095 },
  { "area_id": 7, "node_id": 2, "temperature_c": 19.50, "status": "OK", "last_updated_ms": 124600 }
]
```

### `GET /api/v1/diagnostics`

What the gateway itself is doing: the mode it was deployed in, the area it
speaks for, how long it has been up, and how much it has carried.

```json
{ "mode": "master", "area_id": 7, "uptime_ms": 186095, "devices_seen": 3, "frames": 412 }
```

### `GET /api/v1/nodes/{area}/{node}/diagnostics`

Asks one device to run its hardware smoke and records the answer. The gateway
sends a `DIAG_REQUEST` (`0x0401`) for every probe, and the device answers with
the runtime's counters and two masks: the probes that passed and the probes that
failed. A probe in neither mask is **absent** — the board has no such device,
which is a fact rather than a failure.

```json
{
  "area_id": 7, "node_id": 1, "uptime_ms": 186095,
  "events_published": 412, "handler_errors": 0, "tasks_run": 93, "task_errors": 0,
  "last_error": 0, "probe_ok": 133, "probe_fault": 0
}
```

The answer is kept with the device, so `GET /api/v1/nodes` carries it as a
`diag` object on that device's record. `probe_ok: 133` is `0x85`: console,
timer, onewire, uart and net the board has — the probes a Pico node passes.

## The uplink

A gateway with `UPLINK_PORT` set listens for the gateways below it: a relay dials
in, announces itself with which area it speaks for, and pushes the readings it
hears. The frames are COBS — the same as a serial link — so a relay sends the
readings up the way a board sends them in, and one codec reads both.

- `ANNOUNCE` (`0x0101`) binds the child to its link and records the area it owns,
  so a device in that area is known to be behind it.
- `READING` (`0x0201`) becomes the **same device record** a reading heard
  directly becomes, so `GET /api/v1/nodes` lists a board behind a relay exactly
  as it lists one that is wired in.
- `POST /api/v1/uplink/pump` drains what has arrived and reports how many frames
  it applied. The read endpoints drain on their way in, so a listing is current;
  a supervisor can call the pump to drive a master nothing is asking yet.
- `POST /api/v1/nodes/{area}/{node}/sampling-rate` sends a control to a named
  device. One in a **child's area** goes down that child's uplink and is answered
  asynchronously (the reply arrives on the uplink and is applied when it is
  drained); one on this gateway's own link is answered in the same request.

### The whole tree on one machine

`tools/relay-smoke.py` runs a master, a relay (`relay-host`, on `robohub-linux`)
and a board (a pseudo-terminal) together, and checks both directions: a reading
up from the board to the master, and a command down from the master to the board
and the answer back.

```sh
python3 tools/relay-smoke.py
```

It needs the relay built for the host first — see
`robohub-drivers/examples/gateway/c/relay-host`.

### `POST /api/v1/telemetry/sampling-rate`

Asks the device that sent the latest reading to change its reporting interval.
The gateway sends a `CONTROL_REQUEST` (`0x0301`) with opcode `1` and the
interval, with a fresh `tx_id`, and waits up to 2000 ms for the
`CONTROL_RESPONSE` (`0x0302`) that carries the same `tx_id`.

```json
// request
{ "interval_ms": 250 }

// 200 OK
{ "success": true, "actual_interval_ms": 250 }
```

A node that does not answer in time is `504` with a JSON error; a request without
a positive `interval_ms` is `400`; a link that is not open is `503`. A node may
clamp the interval it can meet, and reports what it actually set (`50` becomes
`100` on the reference node).

### `GET /healthz`

The router port answers a liveness probe for every service:
`{"status":"ok"}`.

## What it emits

Each valid telemetry frame becomes one newline-delimited JSON object on the UDP
collector, with the device it came from:

```json
{"area_id":7,"node_id":1,"timestamp_ms":186095,"temperature_c":24.38,"status":0}
```

Point PlotJuggler at a UDP input on `127.0.0.1:9870` and plot `temperature_c`
against `timestamp_ms`, per area and node.

## The wire

A frame is robohub-transport's, the same one a C firmware node writes:

```
msg_id:u16  tx_id:u16  payload_len:u16  payload  fletcher16:u16
```

COBS-encoded, closed with a `0x00` delimiter. `tx_id == 0` marks unsolicited
telemetry (`TELEMETRY`, `0x0201`), which updates the state and the stream;
`tx_id > 0` marks a response (`CONTROL_RESPONSE`, `0x0302`), which is handed to
the handler waiting on that `tx_id`.

The frame and the payloads are both generated from `schemas/gateway.rho` — which
includes `common.rho`, `link.rho` (L2) and `uplink.rho` (L1) — into
`src/gateway/wire/protocol.rho`:

```sh
tools/generate-protocol.sh
```

That Rho module is the schema's copy for the Rho edge — the same version byte and
little-endian fields the node's C is generated with, and the same COBS and
Fletcher-16 framing as the node's `robohub-transport` — so the two ends are one
description rather than two implementations kept in step. The gateway only
decides what a frame means (`src/gateway/ingest.rho`).

## Layout

| Path | What it is |
|---|---|
| `src/main.rho` | entry, routes, `init`, and the `background()` reader loop |
| `src/gateway/port.rho` | the `extern fn` host declarations the port supplies |
| `src/gateway/wire/protocol.rho` | generated: both levels' payloads and the robohub transport frame |
| `src/gateway/ingest.rho` | what a frame means: telemetry, or a response |
| `src/gateway/stream.rho` | telemetry to per-device state and to PlotJuggler |
| `src/gateway/state.rho` | the REST handlers, diagnostics, and the control RPC |
| `schemas/common.rho` | the identity and families both levels share |
| `schemas/link.rho` | L2: what a board and its gateway exchange |
| `schemas/uplink.rho` | L1: what gateways exchange |
| `schemas/gateway.rho` | both levels, which is what this service generates |
| `module.yml` | the module, and the go-server target with its `gateway` port |
| `tools/generate-protocol.sh` | regenerate `src/gateway/wire/protocol.rho` |
| `tools/smoke.py` | end-to-end test against a pseudo-terminal |

## Running with a Pico 2W node

The node is the `robohub-drivers` example
`examples/temperature/ds18b20/c/telemetry`: a DS18B20 read on the sensor bus,
published on the runtime's event bus, framed by `robohub-transport` and written
to USB CDC. It is generated from the same schema the gateways use
(`schemas/link.rho`), so its `Telemetry` payload and message ids are the ones
this gateway decodes.

```sh
# 1. Build and flash the node (hold BOOTSEL while plugging the board in).
cd /home/miraur/projects/robohub-drivers/examples/temperature/ds18b20/c/telemetry
rho build -target pico2w .
rho flash -target pico2w -project . build/output/telemetry.elf

# 2. The gateway, on the host the board is plugged into.
cd /home/miraur/projects/robohub-gateway
rho run .                       # SERIAL_PORT defaults to /dev/ttyACM0

# 3. Watch it.
curl -s localhost:8080/api/v1/telemetry/temperature
# {"area_id":7,"node_id":1,"temperature_c":24.38,"status":"OK","last_updated_ms":186095}
curl -s localhost:8080/api/v1/nodes
# [{"area_id":7,"node_id":1,"temperature_c":24.38,"status":"OK","last_updated_ms":186095}]
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"interval_ms":250}' \
     localhost:8080/api/v1/telemetry/sampling-rate
# {"success":true,"actual_interval_ms":250}
```

`rho flash` is the toolchain's: it runs the platform's own ELF-to-UF2 converter
and writes the image to the board (the `flash` block in the board's target spec).
Nothing here calls a flashing tool by hand.

**While the wire changes.** The board firmware is being regenerated from the
shared schema. Until it is, a board built from the older `telemetry` schema does
not interoperate with this gateway: a reading now names a device (an area and a
node), and a command is a control with an opcode rather than the sampling-rate
message it used to be. Regenerating the node and edge examples is the next step.

### A Pico as the gateway

A Pico 2W can be the gateway itself, for a garden with no host. The
`robohub-drivers` example `examples/gateway/c/repeater` is the measuring node
plus a downstream UART: frames that arrive on `uart1` publish the same
`Telemetry` event on the same bus, and `robohub-telemetry` bridges the bus to
the upstream USB CDC. It forwards a board by hearing it — the reading already
names its node, so there is no source table — and it measures while it does.

```sh
cd robohub-drivers/examples/gateway/c/repeater
rho build -target pico2w .
rho flash -target pico2w -project . build/output/repeater.elf
```

Point this gateway at that Pico and `GET /api/v1/nodes` lists the boards behind
it as well as the Pico. With no host at all, the same firmware is the gateway:
it streams upstream to whatever listens. RS-485 for a longer bus is the `rs485`
HAL's direction control on the same UART; the platform's RS-485 implementation
is still a stub.

One host per serial port, today; several boards on the bus behind one Pico are
one link, and several Picos are several ports.

## Test

`tools/smoke.py` stands a pseudo-terminal in for the node and a UDP socket in for
PlotJuggler, then drives a telemetry frame from two boards, checks the API and
the datagrams, and drives a sampling-rate RPC:

```sh
rho build .
python3 tools/smoke.py
```

## Status

The gateway runs, and the tree around it runs with it.

- A board's reading — an area and a node — is attributed, kept and served; a
  command for a device is a control with an opcode, answered by the device it
  names, or routed **down** to a relay's link and answered asynchronously.
- Gateways below dial the uplink; their readings join the same device records,
  and their announcements become the child table a command is routed by.
- A board's diagnostic (its runtime counters and a hardware smoke) is recorded
  with the device and served.
- `tools/smoke.py` drives the gateway itself over a pty; `tools/relay-smoke.py`
  runs a **whole tree on one machine** — a master, a relay on `robohub-linux`, and
  a board — and checks a reading up and a command down.

On hardware, the node, edge and relay firmwares build for `pico2w`; the relay
streams a downstream board and routes a command down to it. The full design is
[docs/architecture.md](docs/architecture.md).

## Deployment

A container image, a systemd unit and an nginx vhost, as the registry is
deployed:

```sh
docker build -f deploy/Dockerfile --build-context rho=/path/to/rho -t <user>/robohub-gateway .
```

[docs/deployment.md](docs/deployment.md) covers the modes, the second (uplink)
port that is not HTTP, running a gateway with a board wired to it, and verifying
a deployment.
