# robohub-gateway

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

- `POST /api/v1/telemetry/sampling-rate` — an RPC that changes a node's
  reporting interval, answered `200` with the node's acknowledgement or `504`
  when the node does not reply in time. A node may clamp the interval it can
  meet and reports what it actually set.

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
