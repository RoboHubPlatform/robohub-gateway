# robohub-gateway

**Version:** 0.2.0

The edge gateway for RoboHub firmware nodes.

A node writes telemetry as COBS frames over serial. The gateway reads them,
keeps each board's latest values where an HTTP client can read them, forwards
every reading to PlotJuggler over UDP, and answers a REST call that asks a node
to change its sampling rate — an RPC over the same link.

It is written in Rho and generated into a Go server by `rho build`. The only
host-side parts are the ones Rho cannot express, and they are one port: opening
the serial link, sending a datagram, reading a monotonic clock, remembering
which boards have been heard from, and holding the state a reader and an HTTP
handler share.

## What it answers

- `GET /api/v1/telemetry/temperature` — the latest reading, from whichever device
  sent it last.

  ```json
  { "area_id": 7, "node_id": 1, "temperature_c": 24.38, "status": "OK", "last_updated_ms": 186095 }
  ```

- `GET /api/v1/nodes` — every device a frame has come from, each with its newest
  values. A reading names its own device — an area and a node — so two boards
  numbered 2 in different areas are two devices.

- `GET /api/v1/diagnostics` — what the gateway itself is doing: the mode it was
  deployed in, the area it speaks for, its uptime and what it has carried.

- `POST /api/v1/devices` — register a network device (name, kind, transport,
  address, device id). The id is the device's stable identity: a client that
  knows the hardware id passes it, one that does not gets `transport:address`,
  and a second registration of the same id is refused rather than becoming a
  second series.
- `GET /api/v1/devices` — the registered devices and their latest state.
- `POST /api/v1/devices/{id}/poll` — read an HTTP device's status.
- `POST /api/v1/devices/{id}/switch` — switch an HTTP device on or off; the
  state may come as a form field or as `?state=on|off`.
- `POST /api/v1/frames` — accepts one encoded frame from a network bridge. A
  Shelly or another IP device reaches the gateway through a relay or a small
  poller; the frame is the same one a serial link carries, so it lands in the
  same device record and stream.
- `POST /api/v1/telemetry/sampling-rate` — an RPC that changes a node's
  reporting interval, answered `200` with the node's acknowledgement or `504`
  when the node does not reply in time. A node may clamp the interval it can
  meet and reports what it actually set.

- `GET /` — the gateway page: mode, area, uptime, devices, frames, the latest
  values per node, and a frame history whose first row is newest. A node's
  `last_updated_ms` is its own uptime; the gateway's wall-clock
  `received_at_ms` is recorded beside it, and the page renders that locally. The history
  window is `/frames/{10|25|50|100|500|1000}`; `/export/frames/<count>/csv`
  exports it. Every node record carries both `last_updated_ms` (the node's
  uptime) and the gateway's wall-clock `received_at_ms`.
- `GET /healthz` — a liveness probe, registered for every service.

## What it emits

Each valid telemetry frame becomes one newline-delimited JSON object on the UDP
collector, naming the device it came from:

```json
{"area_id":7,"node_id":1,"timestamp_ms":186095,"temperature_c":24.38,"status":0}
```

## The wire

A frame is robohub-transport's, the same one a C firmware node writes: a six-byte
header (`msg_id`, `tx_id`, `payload_len`), the payload, and a Fletcher-16 check,
COBS-encoded and closed with a `0x00` delimiter. The payloads and the framing in
this gateway are generated from `schemas/gateway.rho`, which includes the same
`common.rho` and `link.rho` a board's C is generated from, so the two ends are
one description.

## A Pico as the gateway

A Pico 2W can be the gateway itself. The `robohub-drivers` example
`examples/gateway/c/repeater` is the measuring node plus a downstream UART:
boards wired to it publish the same `Telemetry` event, and the readings stream
up the USB CDC — to this program when a host is present, or to whatever listens
when there is not.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `SERIAL_PORT` | `/dev/ttyACM0` | the node's serial device |
| `SERIAL_BAUD` | `115200` | the line speed |
| `PLOTJUGGLER_ADDR` | `127.0.0.1:9870` | the UDP collector, `host:port` |
| `REST_PORT` | `8080` | the REST listener, a port; `RHO_HTTP_ADDR` overrides it |
| `GATEWAY_MODE` | `master` | `node`, `edge`, `relay` or `master` |
| `GATEWAY_AREA` | `1` | the area this gateway speaks for |
| `OUTBOX_PATH` | `~/.rho/robohub/gateway-outbox.jsonl` | the durable reading feed |
| `OUTBOX_MAX_ROWS` | `200000` | how many unacked readings are retained before the oldest are dropped |
| `GATEWAY_ID` | `default` | this gateway's id; a caller's token must carry `gateway:<id>:read` or `gateway:<id>:command` |
| `RHO_AUTH_PUBLIC_KEY` | `~/.rho/auth/public.pem` | keeper's public key, which verifies every caller's token |
| `GATEWAY_KEY_FILE` | unset | this gateway's key (64 hex digits, owner-only); each board's key is derived from it and the board's id |
| `ALLOW_PLAIN_FRAMES` | unset | `1` accepts unauthenticated frames from boards not yet reflashed, with a warning; unset drops them |

## The telemetry outbox

The gateway is also the edge recorder. Every accepted reading is normalized to
one record per signal — a `PowerReading` becomes one record per valid field, a
`StateReading` one per valid state, a `SensorReading` one for the field it
names — and appended to a durable outbox with a per-gateway sequence.

An aggregator reads a page, commits it, and acknowledges the sequence, which is
what lets the gateway prune:

- `GET /api/v1/telemetry/outbox?after=<seq>&limit=<n>` — the readings after
  `after`, at most `limit` (default 500). A record is
  `{"seq","source","area_id","node_id","field","value","status","received_ms"}`,
  where `source` is the board that sent it, by the id its session was opened
  with. A reading that arrived unauthenticated, or from a gateway below, has
  none, and the store places it by area and node.
- `POST /api/v1/telemetry/ack` — `{"seq": n}`; removes what the aggregator has
  committed and records the floor so a restart does not replay it.
- `GET /api/v1/telemetry/outbox/status` — `path`, `depth`, `base_seq`,
  `last_seq` and `dropped`.

The sequence continues across a restart, including after the outbox has been
emptied, and a repeated page is harmless: the aggregator keys a reading by
gateway and sequence. The file is written through a buffered writer and synced
every two seconds or every hundred readings, so a board's eMMC sees batches
rather than one write per sample. `OUTBOX_MAX_ROWS` bounds what is kept when no
aggregator is reachable; the oldest records are dropped and counted.

The same feed carries a network device the gateway polls. A Shelly's state and
power are written as `state` and `power_w` under the gateway's own device id, so
the history exists before the device is folded into a plan.
