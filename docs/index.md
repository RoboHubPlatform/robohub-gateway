# robohub-gateway

The edge gateway for RoboHub firmware nodes.

![The gateway's own page: its mode, area, uptime, the latest value per node and the frame history](images/gateway.jpg)

A node writes telemetry as COBS frames over serial. The gateway reads them,
keeps each board's latest values where an HTTP client can read them, forwards
every reading to PlotJuggler over UDP, and answers a REST call that asks a node
to change its sampling rate — an RPC over the same link.

It is written in Rho and generated into a Go server by `rho build`. The only
host-side parts are the ones Rho cannot express, and they are one port: opening
the serial link, sending a datagram, reading a monotonic clock, remembering
which boards have been heard from, and holding the state a reader and an HTTP
handler share.

Mesh builds, deploys and reads its gateways: each Mesh project has one, started
with that project's settings (see {ref}`mesh/index`). Mesh ingests the
gateway's outbox into its telemetry store (see {ref}`mesh-telemetry/index`),
and applications read the readings from Mesh, never from the gateway directly.

## What it answers

Every route but `/healthz` needs a token (see [Access](#access) below).


- `GET /api/v1/telemetry/temperature` — the latest reading, from whichever device
  sent it last.

  ```json
  { "area_id": 7, "node_id": 1, "temperature_c": 24.38, "status": "OK", "last_updated_ms": 186095 }
  ```

- `GET /api/v1/nodes` — every device a frame has come from, each with its newest
  values. A reading names its own device — an area and a node — so two boards
  numbered 2 in different areas are two devices.

- `GET /api/v1/telemetry/power`, `GET /api/v1/telemetry/state` — the newest
  power reading and device state, from whichever device sent it last.

- `GET /api/v1/diagnostics` — what the gateway itself is doing: the mode it was
  deployed in, the area it speaks for, its uptime and what it has carried.

- `GET /api/v1/nodes/{area}/{node}/diagnostics` — asks one device to run its
  hardware smoke and records the answer with the device. The request is sealed
  under the board's session, on the link that session is held to (a relay's,
  for a board behind one), as a command is; `409` when the board has none.

- `GET /api/v1/keys` — each board's key rotation as the gateway knows it: the
  epoch the manifest names, the epoch the board confirmed, and what last
  happened (`confirmed`, or `cannot` for a board that has no store for a key).
  See [key epochs](#key-epochs-and-rotation).

- `POST /api/v1/nodes/{area}/{node}/sampling-rate`, `…/switch`, `…/command` —
  a control sent to a named device: its reporting interval, a switch (on or
  off), or any opcode and argument (a device's own opcodes start at the vendor
  base, so they cannot collide with a core one). A device in a child gateway's
  area is reached down that child's uplink.

- `POST /api/v1/devices` — register a network device (name, kind, transport,
  address, device id). The id is the device's stable identity: a client that
  knows the hardware id passes it, one that does not gets `transport:address`,
  and a second registration of the same id is refused rather than becoming a
  second series.
- `GET /api/v1/devices` — the registered devices and their latest state.
- `POST /api/v1/devices/{id}/poll` — read an HTTP device's status.
- `POST /api/v1/devices/{id}/switch` — switch an HTTP device on or off; the
  state may come as a form field or as `?state=on|off`.
- `POST /api/v1/frames` — accepts one encoded frame (without its delimiter) from
  a network bridge holding a `command` token. A Shelly or another IP device
  reaches the gateway through a small poller; the frame is the one a board
  sends, so it lands in the same device record and stream. The frames are a
  board's own: a hello opens a session and is answered in the response,
  `{"success":true,"session":"<encoded SESSION frame, hex>"}` — never on the
  serial link — and a frame sealed under a session opened this way is taken as
  that board's. A plain frame, or one sealed under a session of another link, is
  refused (`403`): `ALLOW_PLAIN_FRAMES` is the serial link's alone.
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
- `POST /api/v1/uplink/pump` — drains what has arrived on the uplink and says
  how many frames it applied.
- `GET /healthz` — a liveness probe, registered for every service, and the one
  route that needs no token.

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
| `GATEWAY_AREA` | `1` | the area this gateway speaks for, and numbers its devices in |
| `UPLINK_PORT` | `0` | the TCP port the gateways below (and boards on Wi-Fi) dial in on; `0` listens on none |
| `UPLINK_BIND` | the host of `RHO_HTTP_ADDR`, else every interface | the address the uplink listens on: `127.0.0.1` for this machine only, or one LAN interface's address |
| `UPLINK_MAX_CONNECTIONS` | `32` | how many connections the uplink holds at once; one past it is closed as it arrives |
| `UPLINK_IDLE_TIMEOUT_S` | `300` | a connection that sends nothing for this long is closed; a relay dials again |
| `PARENT_UPLINK` | unset | for `relay`: the parent's uplink, `host:port`, which this gateway dials |
| `RELAY_UID` | unset | for `relay`: its id as a board of the parent's mesh, 16 hex digits |
| `RELAY_KEY_FILE` | unset | for `relay`: the key the parent derives for that id, 32 hex digits, in a file only its user reads |
| `OUTBOX_PATH` | `~/.rho/robohub/gateway-outbox.jsonl` | the durable reading feed |
| `OUTBOX_MAX_ROWS` | `200000` | how many unacked readings are retained before the oldest are dropped |
| `GATEWAY_ID` | `default` | this gateway's id; a caller's token must carry `gateway:<id>:read` or `gateway:<id>:command` |
| `RHO_AUTH_PUBLIC_KEY` | `~/.rho/auth/public.pem` | keeper's public key, which verifies every caller's token |
| `GATEWAY_KEY_FILE` | unset | this gateway's key (64 hex digits, owner-only); each board's key is derived from it and the board's id |
| `GATEWAY_SESSION_FILE` | `sessions` beside the key | where live board sessions are kept across a restart, owner-only |
| `ALLOW_PLAIN_FRAMES` | unset | `1` accepts unauthenticated frames on the **serial link** from boards not yet reflashed, with a warning; unset drops them. The uplink never takes a plain frame |
| `RHO_TLS_CERT`, `RHO_TLS_KEY` | unset | a certificate and key: with both, the API is served over HTTPS (see [TLS](#tls)) |

When Mesh deploys a gateway it sets these itself: `GATEWAY_ID` to the project's
id, the mode, the area, the ports, the key file, and the TLS pair when the
project names one. It also writes the [node manifest](#the-node-manifest) beside
the key.

## The node manifest

A board's key derives from the gateway's key and the board's id, so a key that
derives says a board was keyed for this gateway — not that it still belongs to
it, nor where it sits. Mesh writes `nodes.txt` beside `gateway.key` (owner-only,
written to a temporary file and renamed) at every deploy and whenever a board is
added, moved, revoked or re-keyed. One line per node, `#` for a comment:

```text
# uid|area|node|role|epoch|revoked|areas
0123456789abcdef|7|11|board|0|0|
fedcba9876543210|7|20|relay|0|0|9,10
```

`role` is `board` or `relay`; `areas` is, for a relay, the areas it may speak
for; `revoked` is `0` or `1`; `epoch` is the key epoch the board should be at
(empty is 0; see [key epochs](#key-epochs-and-rotation)). With a manifest:

- a hello from a board that is not listed, or is revoked, is not answered, and
  the refusal is logged once an hour per board;
- revoking a board drops the sessions it holds, within a few seconds, and the
  routes it announced — no re-keying of the mesh;
- a board's readings are taken only for its own area and node;
- an announce is taken only from a `relay`, for an area in its `areas`; a relay's
  readings only for its own place and those areas;
- a board behind a relay (below) is answered only when it is listed, and placed in
  an area the relay may speak for: the relay's own area — an in-mesh relay is
  listed with no `areas` and carries the boards of its own area — or one of its
  `areas`.

The file is read at start and again, when it changes, at most every five
seconds. Without one the gateway says so once (`no node manifest: any board with
a derivable key is accepted`) and answers any board whose key derives, as before.
A manifest that disappears after one was read leaves the last one in force.

Whatever the manifest says, an announce for the gateway's **own** area is never
taken; without a manifest an area or a child's gateway id that another board's
live session holds is not taken over, and a session that announced areas is
taken only for those. Every session is bound to the link it was opened on: a
frame sealed under a session from one uplink connection is refused on another
and on the serial link.

### Key epochs and rotation

A board's key has an epoch. Epoch 0 is `HMAC-SHA256(K_gw, "rh-dev1" | uid)[0:16]`;
epoch n ≥ 1 is `HMAC-SHA256(K_gw, "rh-dev2" | uid | n as u32 LE)[0:16]`. A hello
does not say which epoch it was made under, so the gateway tries the manifest's
epoch for the board, then the one before it (the grace epoch) while the board
has not confirmed the new one; the session records the epoch it was opened under.

A board whose session is under the grace epoch is sent `REKEY` (`0x0014`),
sealed under that session, on the first frame it sends: the new epoch and the new
key wrapped under the current one, bound to the frame's own session id and
counter. The board stores the key and answers `REKEY_ACK` (`0x0015`) with a proof
made with the new key; the gateway checks it and the new epoch is the board's
**confirmed** epoch. From then on a hello under an older epoch is refused, and
any other session the board's id holds under one — someone else holding the old
key — is dropped; the board's own session goes on until its next hello, which is
under the new key. A hello that authenticates under the manifest's epoch confirms
it too, so a board flashed with its current key needs no `REKEY`. An unanswered
`REKEY` is sent again after 20 seconds on a later frame, at most three times a
session. A board whose platform has no key store answers epoch 0 and a zero
proof: it cannot rotate, which is kept and said, and it stays in its grace epoch
and is not asked again until the manifest names another epoch.

What happened to each board's key is kept with the sessions (across a restart)
and written to `rekey.txt` beside the manifest, owner-only, for Mesh to show:

```text
# uid|epoch|status|time, written by the gateway
0123456789abcdef|1|confirmed|1794668892
```

`GET /api/v1/keys` answers the same as JSON. A board posted over HTTP
(`POST /api/v1/frames`) has no link a `REKEY` can go down, so it is not rotated;
reflash it with its current key. Whoever holds a board's old key and recorded the
`REKEY` frame can unwrap the new one — there is no forward secrecy — so after a
key is known to have leaked, rotate and then reflash, or rotate while no one is
listening.

### Sessions and replay

A hello is answered once: the latest nonces of each board are remembered, kept
with the sessions across a restart, and the same hello again is refused without
touching the sessions file. A new session opened on the serial link becomes the
one commands are sealed under only once a frame comes up under it (or at once
when the link holds none), so a replayed hello cannot take the link from the
board. A session id still in use is never given out again; when no free one is
found the hello is refused and logged. A session's counters are kept on disk
whenever one has moved 64 past what was last kept, and a restored session starts
128 past the kept mark, so no frame recorded before a restart opens after it.
Tags are compared in constant time.

A board does not know the gateway restarted, and goes on counting under its
session below that mark. So each restored session's board is asked, sealed under
the restored session, for a new one (`SESSION_RESET`): on the serial link as soon
as the link is open; for a board behind a relay (inside `RELAYED`) or on an uplink
connection, on its first refused frame — one whose tag is the session's and whose
counter is past the kept mark. The downward counter is restored past the same
margin, so the board takes the request; it drops the session and says hello, and
is heard at once. A request goes at most once in 10 s per session, five times in
all, and each is logged (`session <sid> restored after restart: asked board <uid>
to start a new session`). Once the board's new session carries a frame, the
restored one is dropped. A board posted over HTTP is not asked: its next post
under the restored session is refused, and it says hello again. A board needs a
robohub-transport that knows `SESSION_RESET` to take the request; an older firmware ignores it and is heard again once its counter passes
the mark.

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

## Relay mode

A gateway with `GATEWAY_MODE=relay` and `PARENT_UPLINK` set reports through its
parent: it is how one mesh is linked under another. The relay is a board of the
parent's mesh: it dials the parent's uplink, says hello with `RELAY_UID` and
opens a session with the key in `RELAY_KEY_FILE` —
`HMAC-SHA256(K_gw, "rh-dev1" | uid)[0:16]` of the parent's gateway key, which is
what the parent derives for any board — so the parent needs nothing new to
authenticate it. It then announces its `GATEWAY_AREA`, and redials and opens a
new session whenever the connection drops.

What the relay hears from its own boards and from the gateways below it goes up
under that session: readings, power, state and sensor readings, each naming its
area and node, so the parent's `GET /api/v1/nodes` lists them — hop by hop, and
taken by the parent only for the areas its manifest gives the relay. A command
the parent routes to the relay's area is opened by the relay and passed on to
the board it names, under the relay's own session with that board; the board's
answer goes back up with the parent's transaction id.

A board on the relay's serial link that its **parent** planned — keyed from the
parent's gateway key, so one this gateway cannot open: not in this gateway's
node manifest, or, without one, a board whose hello does not authenticate under
this gateway's key — is carried whole instead, in [RELAYED](#boards-behind-a-relay-relayed)
on port 0, and the parent authenticates it with its own key. The SESSION the
parent sends back names the session id whose frames the relay carries from then
on.

## Boards behind a relay: RELAYED

A relay that holds no key of a board's — the C relay
(`robohub-drivers/examples/gateway/c/relay`, `relay-host`), or a gateway in relay
mode for a board its parent planned — carries the board's frames whole. Its
hello (`0x0011`) and every frame it seals under its own session go up unchanged
inside a `RELAYED` message (`0x0013`), sealed under the relay's own session:

```text
port(u8) | inner_msg_id(u16) | inner_tx_id(u16) | inner_payload
```

The ids are little-endian, as in the transport header. This gateway opens the
outer frame with the relay's session and the inner one with the board's own key:

- the relay must be a `relay` in the node manifest (with one); the board must be
  listed, in an area the relay may speak for, and its hello is answered with a
  SESSION that goes back inside `RELAYED` to the relay's link and port;
- the board's session is held to that relay and port: a board's frame carried by
  another relay, or on another port, is refused, as is a plain frame inside
  `RELAYED`, and one changed after the board sealed it does not open;
- a reading is the board's — kept under its id — and taken for the board's own
  place and an area the relay may speak for (without a manifest: the areas the
  relay announced, when it announced any);
- a command for the board is sealed under the board's session and goes down
  inside `RELAYED` to the relay, which writes the inner frame to the port; the
  board's answer comes back the same way and answers the request.

So a relay can drop or delay a board's frame and cannot forge, change or move
one. Downlink is routed per board session — the session a command is sealed
under names the link it goes down, the serial link, an uplink connection, or a
relay and port — rather than one session per link: a command for a device goes
to the session of the board the manifest places there (without a manifest, of
the board whose own readings come from there), and only when no board session is
known to the area's announced child or the serial link as before. A board's
session and the link it is held to are kept across a restart; a relay that
dials again opens a new session of its own, and the boards behind it go on under
theirs.

A frame nested twice — a board behind a C relay behind a gateway in relay mode —
is a `SensorReading` (80 bytes) sealed (+14), carried (+5), sealed (+14),
carried and sealed again: 132 bytes of payload, within the 256-byte frame a
board's transport takes.

## The uplink

A gateway with `UPLINK_PORT` set listens for what dials in from below: a relay,
or a board on Wi-Fi. The frames are COBS — the same as a serial link — so a relay
sends readings up the way a board sends them in, and one codec reads both.

- `ANNOUNCE` (`0x0101`) binds the child to its connection and records the area it
  owns, so a device in that area is known to be behind it.
- `READING` (`0x0201`) becomes the **same device record** a reading heard
  directly becomes, so `GET /api/v1/nodes` lists a board behind a relay exactly
  as it lists one that is wired in.
- The read endpoints drain the uplink on their way in, so a listing is current;
  `POST /api/v1/uplink/pump` drives a master nothing is asking yet.
- A control for a device in a **child's area** goes down that child's uplink and
  is answered asynchronously (the reply is applied when it is drained); one on
  this gateway's own link is answered in the same request.

The uplink is authenticated like the serial link: whoever dials in says hello on
its connection and is answered on it, every frame after that is sealed under the
session that connection opened, and a plain frame is dropped — also with
`ALLOW_PLAIN_FRAMES=1`, which is for the serial link alone. Who may announce
which area is the [node manifest](#the-node-manifest)'s to say. A sensor reading that comes up the uplink
from a board is kept under that board's id; what a relay carries up is placed by
area and node.

Without `UPLINK_BIND` the uplink listens where the API does when `RHO_HTTP_ADDR`
names a host, and otherwise on every interface — relays on other machines dial
it — which is the wrong default for a shared network:

```sh
UPLINK_BIND=127.0.0.1      # this machine only
UPLINK_BIND=192.168.1.20   # one LAN interface
```

What a peer can hold is bounded wherever it listens: at most
`UPLINK_MAX_CONNECTIONS` connections, each closed after `UPLINK_IDLE_TIMEOUT_S`
of silence or after 4 KiB without a frame delimiter, a bounded queue of frames
waiting to be applied, and writes to a peer that stopped reading time out. A
refusal of each kind is logged at most once a minute, and so is a hello that
does not authenticate.

The uplink is plain TCP, not HTTP: a reverse proxy does not carry it (see
{ref}`robohub-gateway/deployment`). `tools/relay-smoke.py` runs a master, the C
relay (`relay-host`, on `robohub-linux`, built natively when it is not built, and
skipped where it cannot be) and sealed boards on one machine and checks a board's
session and reading through the relay, a command down and its answer, a relay
that forges, a plain command on the relay's USB port, a gateway in relay mode
carrying a board its parent planned, the master restarting, a board's key
rotated through the relay, and a second C relay wired to the master's serial link
(`RHO_PARENT_LINK=console`: its own session on the serial link, the board's frames
inside `RELAYED` under it) carrying a board's session, reading, command,
diagnostic and key rotation.
`tools/security-smoke.py` checks the rules above: the manifest, announces,
sessions bound to their link, replayed hellos, counters across a restart, frames
posted over HTTP, sealed diagnostics, and relayed boards without a manifest.
`tools/restart-smoke.py` checks a restart as the boards see it: the request
for a new session on the serial link at open, sealed past the board's last
downward counter; frames within the restored window and recorded before the
restart refused; a board behind a relay asked inside `RELAYED` and one on an
uplink connection asked on its new connection; the restored session dropped once
the new one carries a frame; and — when `gateway_node` is built — the C host node
heard again within seconds.
`tools/rekey-smoke.py` checks key epochs: the grace epoch, `REKEY` and its proof,
the old epoch refused after confirmation and another holder of it dropped, a board
flashed at its current epoch, one too far behind, one that cannot rotate, a proof
that does not check, a restart, and — when robohub-devices' `gateway_node` is
built — the C host node rewriting its key file.

## Access

Every route but `/healthz` takes a short-lived token the keeper issued for the
audience `gateway`, verified with the keeper's public key
(`RHO_AUTH_PUBLIC_KEY`, by default `~/.rho/auth/public.pem`, copied beside the
gateway when it is deployed). An API client sends it as `Authorization: Bearer
<token>`; a browser is sent to the keeper's sign-in.

The token's scopes name this gateway (`GATEWAY_ID`, which Mesh sets to the
project's id) and what the caller may do:

| Scope | Allows |
|---|---|
| `gateway:<id>:read` | telemetry, the outbox, nodes, diagnostics, the page and its frames |
| `gateway:<id>:command` | switching, sampling rate, commands, registering and polling devices, injecting frames, pumping the uplink |
| `gateway:<id>:*`, `gateway:*`, `*` | both |

A caller without the scope it needs gets `403`. There is no static token: one
that leaks expires in minutes, and one gateway's token is not another's. Mesh,
which reads and commands its gateways, holds a keeper client credential and asks
for a `gateway:<project>:*` token for each project's gateway itself.

## TLS

A gateway reached over a network should serve HTTPS, so the tokens Mesh presents
never cross the network in the clear. Give it a certificate and key:
`RHO_TLS_CERT` and `RHO_TLS_KEY` (paths on the machine the gateway runs on).
With both set the API is served over TLS; without them it stays plain HTTP, for
a loopback address or a reverse proxy that ends TLS in front of it.

In Mesh, a project names the pair as its `gateway_tls_cert` and
`gateway_tls_key`, and Mesh passes them to the gateway it deploys; the project's
gateway address is then an `https://` one.

## More

- {ref}`Architecture <robohub-gateway/architecture>` — how gateways, boards and the master fit
  together, and why.
- {ref}`Deployment <robohub-gateway/deployment>` — the container, the device, the second port,
  and verifying a deployment.
