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

## [0.7.1] - 2026-11-14

After a restart the gateway asks each board whose session it restored to start a new one (`SESSION_RESET`): on the serial link as soon as it opens, through a relay or an uplink at the board's first refused frame. A board is heard again within a second instead of staying silent until its counter passes the restored mark; a frame recorded before the restart is still refused.

## [0.7.2] - 2026-11-15

Every `let` the code assigned to is declared with `var`, as rho 0.19.0 asks (assigning to a `let` is deprecated there and refused in the next minor release). Nothing else changes.

## [0.7.3] - 2026-11-19

Security fixes from the requirements review.

**Breaking:** a board's hardware smoke is `POST /api/v1/nodes/{area}/{node}/diagnostics` and needs the `gateway:<id>:command` scope. It was a `GET` under the read scope, so any token that could read the gateway — and anything that could get a reader's browser to load a URL — could make a board drive its outputs and probes. Nothing in the ecosystem called the `GET`, so no compatibility path is kept; the gateway's own page (`GET /api/v1/diagnostics`) is unchanged and still read-only.

A switch request without `on`, or with anything but `true`, `false`, `1` or `0`, is refused with `400` instead of being sent as "off"; a network device's switch takes `state=on` or `state=off` only, where any other value was sent as off and recorded as the device's state.

With only one of `RHO_TLS_CERT` and `RHO_TLS_KEY` set the gateway does not start: it served plain HTTP, carrying the tokens it is given in the clear on a deployment that meant to encrypt them. A pair that cannot be loaded already stopped it.

New suites: `gateway-access` (`tools/access-smoke.py`) — a token with the wrong scope refused on every route of each class, the smoke as an action, the device API's validation, and TLS (HTTPS only, a plain request on its port served nothing, a broken pair stopping the gateway); `gateway-vectors` (`tools/vectors-smoke.py`) — the frames the gateway seals checked byte for byte against robohub-transport's known-answer vectors (`tests/vectors/secure.json`, an identical copy). `tools/smoke.py` takes its ports from `GATEWAY_SMOKE_HTTP_PORT`, `GATEWAY_SMOKE_UDP_PORT` and `GATEWAY_SMOKE_UPLINK_PORT` when they are set.

Bug fixes from the requirements review (same release).

**Breaking:** acknowledging the outbox (`POST /api/v1/telemetry/ack`) needs the new `gateway:<id>:ack` scope (or `gateway:<id>:*`, `gateway:*`, `*`). It checked the read scope and its documentation said read, yet the auth middleware refuses every POST from a token whose scopes all end in `:read`, so in practice it took a wildcard — a collector had to be given `gateway:<id>:*`, which also commands every device. An acknowledgement moves the caller's cursor and lets the gateway delete readings, so it is a change; a collector is now given `gateway:<id>:read,gateway:<id>:ack` and commands nothing. Mesh's `gateway:<id>:*` token is unaffected. **Breaking:** `POST /api/v1/devices` refuses an address the gateway should not call (below), and a network switch answers `202` or `502` where it answered `200` (below).

- **A network switch records the state the device reports, not the one requested.** `POST /api/v1/devices/{id}/switch` stored the requested state, and appended it to the outbox, as soon as `Switch.Set` answered — and `Switch.Set` answers with the state the relay *was* in — so a stuck relay, or a plug held by its own button, was recorded and shown as switched (SYS-0025). The gateway now reads `Switch.GetStatus` after switching: `200` with `"confirmed":true` when the device reports the requested state; `502` with the state it does report recorded when it reports the other; `202` with `"confirmed":false`, the request kept as `requested` and the last reported state unchanged, when it gives no status — until a poll reads the device. A board's switch (`/api/v1/nodes/{area}/{node}/switch`) already answered with the state its acknowledgement carries (`actual`); it now also says it as `"state"` and whether it is the one asked for (`"confirmed"`), and one routed to a child gateway answers `"state":"pending"`. Polling or switching an MQTT device, whose address is a topic, is `409` instead of an HTTP request to the topic. A status written with a space after the colon is read.
- **A device is registered only at an address the gateway should call.** `POST /api/v1/devices` stored any address and the gateway later requested it on its own authority from inside the LAN, so a command token could make it request any URL — its own machine's services (the keeper, Mesh) on loopback, or a cloud metadata endpoint — blind, but enough to reach any GET that acts. An HTTP device's address must now be an `http://` or `https://` URL of a host and an optional port, with no path, query or credentials; LAN and public addresses and host names are accepted; loopback (unless `GATEWAY_DEVICE_ALLOW_LOOPBACK=1`, for a simulator), unspecified and link-local addresses, and numeric hosts not written as four decimal octets, are refused with `400`. An MQTT device's topic prefix may not hold wildcards; another transport is refused. A URL-encoded address is decoded (it was stored encoded and never reached).
- `tools/access-smoke.py` checks the acknowledgement's scope (a reader, a token reading this gateway and acknowledging another, a collector, Mesh's wildcard), the addresses a device may and may not be registered at, and a network switch against a stand-in Shelly plug (confirmed, stuck, no status, poll ending the wait, MQTT refused), and that a board's switch answers the state it reached.

Documentation (same release): the outbox's guide and README said an acknowledgement removes what the aggregator committed and the architecture page that the gateway does not remember consumers; it keeps a cursor per consumer and removes a record only once every known consumer has acknowledged it, and `OUTBOX_MAX_ROWS` is its only other bound — nothing is dropped by age. The `SESSION_RESET` cap is spelled out: five requests per restored session, every attempt counted, none after the fifth.

## [0.8.0] - 2026-11-20

- **A board's health is kept and listed (GW-0015).** `HEALTH` (0x0403, PROTO-0007) from an admitted board — held to its own place like a reading — is kept per device with the time it arrived, and `GET /api/v1/nodes` carries it as `health`: the reset reason (word and code), the boot count, the firmware version and the fault record (`null` when there was none). A reset by the watchdog or a fault is appended to the outbox as a `reset` event (value the reason, status the fault code) for Mesh to alert on. A relay carries a board's report up as it carries a reading.
- The protocol copies know `HEALTH` and `ResetReason`.
- `tools/smoke.py` checks a watchdog report and a power-on report: both listed, one `reset` event.

## [0.9.0] - 2026-11-20

- **Updates carried to a board (GW-0016, robohub ADR-0022).** `POST /api/v1/nodes/{area}/{node}/update?offer=<hex>` takes the signed `UPDATE_OFFER` payload Mesh wrote and the image as the body; the gateway sends the offer, then the chunks with up to four in flight, moving on as the board's `UPDATE_STATUS` says — the next index it expects, a chunk lost and sent again, `verified`, `confirmed`, `rolled back`, `refused`, `failed`. No answer for 2 s sends the offer or the chunks in flight again; 10 s of silence while sending offers again, and the board says where to resume. `GET` shows where it stands (also in the board's entry of `/api/v1/nodes`), `DELETE` cancels it and tells the board. One update per board at a time; the image is kept in memory until the board verified it. Each answer goes to the outbox as an `update` event (value the state, status the reason). The gateway does not check the manifest's signature: the board does.
- The protocol copies know the update messages (robohub-protocol 1.5.0).
- `tools/smoke.py` updates a stand-in board on the pty — a lost chunk sent again, verified, confirmed — refuses a malformed offer, a short image and a second update, and cancels one.
