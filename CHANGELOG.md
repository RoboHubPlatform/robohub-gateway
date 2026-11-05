# Changelog

All notable changes to this module are recorded here, one section per release.

## [Unreleased]

The gateway has a durable telemetry outbox. It normalizes each accepted reading
to one record per signal, assigns a sequence that survives a restart, and serves
`/api/v1/telemetry/outbox`, `/api/v1/telemetry/ack` and
`/api/v1/telemetry/outbox/status`. A new `SensorReading` message carries a
device-named measurement, so a sensor that is not temperature, power or state is
a manifest and a publish call rather than a gateway change. The outbox is
written in batches and acked by sequence, which is what lets Mesh keep a series
without losing readings across a restart. The feed can require a bearer token
(`OUTBOX_TOKEN`), and a network device the gateway polls now emits its state and
power into the same feed rather than only into the gateway's live state.

## [0.3.1] - 2026-11-02

Network device API. A Shelly or another HTTP device is registered with
`POST /api/v1/devices`, polled with `POST /api/v1/devices/{id}/poll`, and
switched with `POST /api/v1/devices/{id}/switch`; `GET /api/v1/devices` lists
the definitions and latest state. The gateway port gained the HTTP get and post
the device path uses. An aquarium switch widget can drive the plug through the
gateway switch endpoint.

## [0.3.0] - 2026-11-02

`POST /api/v1/frames` accepts one encoded frame from a network bridge. A Shelly over HTTP RPC or MQTT, or any other IP device behind a relay or poller, becomes a device record through the same ingest path as a serial frame.

## [0.2.0] - 2026-11-01

The gateway page shows a frame history, newest first, with selectable windows
of 10, 25, 50, 100, 500 and 1000 rows and CSV export. A node's `timestamp_ms`
is its own uptime; the gateway records the wall-clock `received_at_ms` beside
it and renders that as local date/time. Page and API carry both.
