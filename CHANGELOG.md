# Changelog

All notable changes to this module are recorded here, one section per release.

## [0.5.5] - 2026-11-14

The unused generated `link/` and `uplink/` protocol copies are gone (only `wire/` is compiled); a device's JSON is written by `json_string`.

## [0.5.4] - 2026-11-14

The protocol schemas are a copy of robohub-protocol 1.0.0 (robohub/protocol), written by its tools/sync.py; the generated Rho is unchanged.

## [0.5.3] - 2026-11-13

A board that asks for a session from a gateway with no usable key is said once
in the log, naming the key file: it is why a sealed board goes unheard, and it
was silent.

## [0.5.2] - 2026-11-13

The serial link is opened when it appears and again when it comes back: a port
that was not there at start no longer disables ingest for good, and a board on
USB that resets (and re-enumerates) is heard again within two seconds. Built on
rho-robohub 0.5.1. The smoke test starts the gateway before its port exists,
then takes the port away and brings it back.

## [0.5.1] - 2026-11-13

A restart no longer blinds the boards.

- The live sessions are kept in a file only the gateway's user can read (`sessions` beside the gateway's key, or `GATEWAY_SESSION_FILE`) and taken back at start, so a board that holds a session is heard at once instead of after it expires, up to an hour later. Each session's counters are kept every 64 frames, and a restored session starts 64 past what was kept: a frame recorded before the restart cannot be replayed after it.
- A session's expiry is reckoned on the wall clock, not on the uptime counter, which wrapped after 49.7 days and stopped sessions expiring.
- A new session never takes the id of one still in use (the id is 16 bits); expired sessions are forgotten.
- The smoke test runs again: it carries a ticket and a gateway key, and drives the sealed path — a hello, a session, sealed frames, a restart, a refused stale counter and a refused replay. The relay test carries a ticket too.

## [0.5.0] - 2026-11-13

Relay mode: a gateway with `GATEWAY_MODE=relay` reports through a parent. It
dials `PARENT_UPLINK`, opens a session as a board of the parent's mesh with
`RELAY_UID` and the key the parent derives for it (`RELAY_KEY_FILE`), announces
its area, and carries what its boards report up under that session. A command
the parent routes to its area is passed on to the board and answered with the
parent's transaction id. A parent files a relay's sensor readings by the area
and node each names, rather than under the relay. Needs rho-robohub 0.4.2.

## [0.4.1] - 2026-11-06

Security: all write paths (frames, device registration, polling, switching,
sampling-rate, outbox) now require the bearer token when GATEWAY_TOKEN or
OUTBOX_TOKEN is set. The uplink honours UPLINK_BIND. Frames history no longer
records redundant sensor entries.

## [0.4.0] - 2026-11-05

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

The live listing now understands the generic `m<index>.<field>` readings the
generated node firmware publishes: `/api/v1/nodes` carries a `sensors` object,
the node table shows the module temperature rather than the legacy zero spot
field, and a SensorReading adds a row to the frames history.

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

## [0.6.0] - 2026-11-14

The gateway knows its boards. Mesh writes a node manifest (`nodes.txt`, beside the gateway key): each board's id, place, role and whether it is revoked. A board not in it, or revoked, is refused, and a revoked board's sessions end. A board's readings are taken only for its own place; a relay speaks only for the areas it is given, and no one may announce the gateway's own area.

Boards behind a relay are authenticated end to end: the relay forwards their handshake and sealed frames in `RELAYED`, the gateway opens them with the board's own key and keeps the readings under the board's id, and commands go back to each board through the relay it came by. A gateway in relay mode does the same for boards its parent planned.

A session belongs to the link it was opened on; a replayed hello is refused; a live session is never overwritten; counters are saved so a restart leaves no replay window; tags are compared in constant time. Plain frames are refused on the network uplink whatever `ALLOW_PLAIN_FRAMES` says, and a hello posted over HTTP is answered in the response rather than on the serial line. Needs rho-robohub 0.5.3 for the uplink limits.

## [0.7.0] - 2026-11-14

Board keys can be rotated. The manifest's epoch is the key a board should have: a board still on the one before is let in for a grace period and handed its new key (`REKEY`); its confirmation ends the old key, and drops any other session that still used it. A board that cannot store a key says so, and Mesh shows it. Status is in `rekey.txt` and `GET /api/v1/keys`. Diagnostics go to a board sealed under its session. A C relay on the serial link carries its boards end to end.

After a restart the gateway asks each board whose session it restored to start a new one (`SESSION_RESET`): on the serial link as soon as it opens, through a relay or an uplink at the board's first refused frame. A board is heard again within a second instead of staying silent until its counter passes the restored mark; a frame recorded before the restart is still refused.
