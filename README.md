# robohub-gateway

The edge gateway for RoboHub firmware nodes. It reads COBS frames from a serial
link, keeps each board's latest telemetry, streams every reading to PlotJuggler
over UDP, and serves the same state — plus the RPC that changes a node's
sampling rate — over HTTP.

It is a Rho application. `rho build` generates a Go server from the `.rho`
sources; the host primitives it needs (serial, UDP, the clock, shared state,
node attribution, transaction correlation) are one port, `gateway`, named in
`module.yml`.

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

## REST API

### `GET /api/v1/telemetry/temperature`

The latest telemetry the gateway has seen, from whichever board sent it last.

```json
{ "node_id": 1, "temperature_c": 24.38, "status": "OK", "last_updated_ms": 186095 }
```

Before any frame arrives the reading is `0.00`, the status `OK` and the
timestamp `0` — a zero reading, not an error.

### `GET /api/v1/nodes`

Every board a frame has come from, each with its newest values. A reading names
its board, so a gateway that hears several — directly or through a repeater —
attributes them without the link telling it.

```json
[
  { "node_id": 1, "temperature_c": 24.38, "status": "OK", "last_updated_ms": 186095 },
  { "node_id": 2, "temperature_c": 19.50, "status": "OK", "last_updated_ms": 124600 }
]
```

### `POST /api/v1/telemetry/sampling-rate`

Asks the node to change its reporting interval. The gateway frames a `0x0301`
command with a fresh `tx_id`, writes it, and waits up to 2000 ms for the `0x0302`
acknowledgement that carries the same `tx_id`.

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
collector, with the board it came from:

```json
{"node_id":1,"timestamp_ms":186095,"temperature_c":24.38,"status":0}
```

Point PlotJuggler at a UDP input on `127.0.0.1:9870` and plot `temperature_c`
against `timestamp_ms`, per `node_id`.

## The wire

A frame is robohub-transport's, the same one a C firmware node writes:

```
msg_id:u16  tx_id:u16  payload_len:u16  payload  fletcher16:u16
```

COBS-encoded, closed with a `0x00` delimiter. `tx_id == 0` marks unsolicited
telemetry (`0x0201`), which updates the state and the stream; `tx_id > 0` marks a
response (`0x0302`), which is handed to the handler waiting on that `tx_id`.

The frame and the payloads are both generated from `schemas/telemetry.rho` into
`src/gateway/protocol.rho`:

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
| `src/gateway/protocol.rho` | generated: payloads and the robohub transport frame |
| `src/gateway/ingest.rho` | what a frame means: telemetry, or a response |
| `src/gateway/stream.rho` | telemetry to per-node state and to PlotJuggler |
| `src/gateway/state.rho` | the REST handlers and the sampling-rate RPC |
| `schemas/telemetry.rho` | the schema both ends are generated from |
| `module.yml` | the module, and the go-server target with its `gateway` port |
| `tools/generate-protocol.sh` | regenerate `src/gateway/protocol.rho` |
| `tools/smoke.py` | end-to-end test against a pseudo-terminal |

## Running with a Pico 2W node

The node is the `robohub-drivers` example
`examples/temperature/ds18b20/c/telemetry`: a DS18B20 read on the sensor bus,
published on the runtime's event bus, framed by `robohub-transport` and written
to USB CDC. It is generated from the same `schemas/telemetry.rho`, so its
`Telemetry` payload and message ids are the ones this gateway decodes.

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
# {"node_id":1,"temperature_c":24.38,"status":"OK","last_updated_ms":186095}
curl -s localhost:8080/api/v1/nodes
# [{"node_id":1,"temperature_c":24.38,"status":"OK","last_updated_ms":186095}]
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"interval_ms":250}' \
     localhost:8080/api/v1/telemetry/sampling-rate
# {"success":true,"actual_interval_ms":250}
```

`rho flash` is the toolchain's: it runs the platform's own ELF-to-UF2 converter
and writes the image to the board (the `flash` block in the board's target spec).
Nothing here calls a flashing tool by hand.

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

Verified on a Pico 2W: the node firmware streams a real DS18B20 reading over USB
CDC, the gateway serves it, PlotJuggler receives it, and the sampling-rate RPC
round-trips (`250` honoured, `50` clamped to `100`). The repeater firmware boots
on the board and streams its own reading with the downstream link in place.
Aggregating a second board needs that board wired to the repeater's `uart1`
(GP4 TX → board RX, GP5 RX → board TX, GND); attribution of several nodes is
covered by the smoke test.
