# RoboHub gateway architecture

How gateways, boards and the master fit together, and why. This is the design the
code is built to; the decisions in it are recorded with the reason, so a later
change is a change and not a rediscovery.

## The goal

Aggregate measurements from many boards spread over an area, at a scale where a
single serial link is not enough. Boards are cheap radios and wires; the
aggregation is a tree of gateways that grows by adding a link, not by rewriting a
program.

## Operating modes

A gateway is one stack with a declared **mode**, so the same code builds each:

| Mode | Senses | Downstream | Uplink | Runs on |
|---|---|---|---|---|
| `node` | yes | — | console/serial | any board |
| `edge` | yes | serial/UART | console or TCP | a board (e.g. Pico) |
| `relay` | no | serial or TCP | serial or TCP | a board or a host |
| `master` | no | TCP and/or serial | — | a host (`robohub-linux`, later `robohub-win`) |

- **`edge`** is "gateway and measuring at once": it publishes its own readings on
  the same bus as the frames it hears downstream, exactly as the `repeater`
  example does today. It forwards a board **by hearing it** — a reading names its
  device, so there is no source table.
- **`relay`** is a gateway with no sensor: it hears downstream and forwards
  upstream. A `relay` on a board is the headless hop; on a host it is a
  collector that is not the master.
- **`master`** is a gateway with no uplink. It terminates the tree, serves the
  REST API and the PlotJuggler stream, and is where state is queried.

The mode is declared once — in the manifest for the Rho gateway, in Kconfig for
the C stack (`CONFIG_RH_GATEWAY_MODE`) — and the build selects the links. A
mode is not a separate program; it is which links are opened.

## Two protocol levels, one framing

Both levels use **robohub-transport's frame** — `msg_id:u16 tx_id:u16
len:u16 payload fletcher16`, COBS, `0x00` — because a frame is bytes on both and
there is no reason for a second encoder. What differs is the **schema** (the
message ids and payloads), not the container.

- **L2 — board ↔ gateway** (`schemas/link.rho`). The link a board writes: over
  the console/UART, or over a socket where the board has IP. This is today's
  `telemetry.rho`, extended with identity and diagnostics.
- **L1 — gateway ↔ gateway** (`schemas/uplink.rho`). A relay or edge connects to
  its parent and pushes upstream. Carried over **TCP** (the socket sink already
  exists in `robohub-telemetry`), or over serial for a board-to-board hop.

Both schemas carry the same four **message families**, so the meaning of a
message does not change with the level it travels on:

1. **announce** (`HELLO`) — who the sender is and what it can do: device id,
   firmware version, platform, mode, and the capabilities it offers (sensing,
   gatewaying, diagnostics).
2. **measurement** (`TELEMETRY`) — a reading, naming its device.
3. **diagnostics** (`DIAG_REQUEST` / `DIAG_RESPONSE`) — hardware smoke and
   runtime counters, on both a board and a gateway.
4. **control** (`CONTROL_REQUEST` / `CONTROL_RESPONSE`) — a command addressed to
   a device id. Sampling-rate is the first such command; the family is general.

## Identity

A `node_id:u16` is unique only on one bus, which is enough for a star and not for
a tree across an area. Every device is named by a **device id**:

```
DeviceId { area_id: u16, node_id: u16 }
```

- `node_id` stays what a board already sets (`CONFIG_RH_NODE_ID`); it is unique
  within its **area**.
- `area_id` names the area — the subtree a board belongs to, assigned to the
  gateway it is wired to. The master issues an area id when a gateway joins;
  until then a gateway may run with an area id from its config.

32 bits is small enough for a frame and wide enough for an area of areas; a
64-bit chip EUI is available if a deployment needs global uniqueness without
coordination, and the `hello` is where it would be announced. The master keys
its state by `area_id/node_id`, so two boards with the same `node_id` in
different areas are two devices.

## Topology: a configured tree now, a mesh later

Chosen: **a configured tree**, with the master found by a configured parent
rather than by discovery.

- Every non-master gateway names a **parent**: a `host:port` for a TCP uplink, or
  a serial device for a board-to-board hop. The master is the gateway with no
  parent.
- Uplinks are **outbound**. A gateway connects to its parent; a parent never
  dials a child. A board or an edge box behind NAT therefore still aggregates
  upward, and no edge device needs an open port.
- Readings flow **up** the chain to the master. A command addressed to
  `area_id/node_id` flows **down**: each gateway consults the child table it
  built from the devices it has heard (and from the `hello`s of the gateways
  below it) and forwards to the one that owns the subtree.
- The tree is **acyclic by construction**: a parent is explicit, so there is no
  loop to detect and no path to select.

A **mesh** — peers discovering each other and forwarding toward the master by a
hop rule — is a next-hop policy over the *same* L1 schema, added later, not a
redesign. The wire format does not change; only how a gateway picks where to send
does. Designing the `hello` to carry capabilities and the device id now is what
keeps that door open.

## Diagnostics and hardware smoke

A board in the field has no debugger, so both levels answer:

- **On a board**: a `DIAG_REQUEST` runs a **hardware smoke** — probe each
  registered HAL (onewire/i2c/spi device presence, a GPIO loopback, timer
  monotonicity, console write) — and returns pass/fail per probe, together with
  `robohub_core_diagnostics` (events published, handler errors, tasks run and
  failed, uptime, last error). The same counters already exist in
  `robohub-core`.
- **On a gateway**: a `DIAG_RESPONSE` reports uptime, each link's state, nodes
  and gateways seen, frames in and out, drops, and the platform's network
  status.

The master exposes its own diagnostics as `GET /api/v1/diagnostics`, and a
device's last smoke as part of its node record, so "it stopped" becomes "the
third probe on that board has been failing since the link came up".

## The host platform: `robohub-linux`

`robohub-common/platform.h` already names `robohub-linux` among the platforms
that implement `robohub_platform_init` and `robohub_board_register`. It makes the
C stack run **on a host**, for gateway-only modes:

- `hal/uart` over a POSIX tty (a USB serial device, a PTY in a test);
- `hal/timer` over `CLOCK_MONOTONIC`;
- `net.h` over POSIX sockets (TCP and UDP) — the implementation the uplink uses;
- `hal/gpio`/`spi`/`i2c` over libgpiod/spidev at first, stubbed where a host has
  none, so a `relay` runs with the probes it can and reports the rest as absent.

`robohub-win` implements the same contract later.

The **master** is the Rho gateway (`applications`-style, Rho → Go) rather than a
C program: it already serves REST and the PlotJuggler stream, and it aggregates
without sensing. The C stack is the on-board and edge side; the two meet at the
L1 schema.

## What changes where

| Repository | Change |
|---|---|
| `robohub-gateway` | the `mode`; `schemas/link.rho` and `schemas/uplink.rho`; the uplink listener and dialer; device-keyed state; `/api/v1/diagnostics`; routing down by device id |
| `robohub-common` | `DeviceId` in `types.h`; the `hello`/diag payloads' C side |
| `robohub-core` | a HAL smoke entry point over the registry (probe what is registered) |
| `robohub-transport` | unchanged — one framing for both levels |
| `robohub-telemetry` | a sink per level: transport (L2), socket (L1); decimation applies per uplink |
| `robohub-linux` | new: `platform.h`, `net.h`, the HALs over POSIX |
| `robohub-drivers` | the node/edge examples regenerate from the shared schema; `edge` and `relay` examples |

## Phases

1. **Schema and identity.** `link.rho` (L2) and `uplink.rho` (L1) with the four
   message families and `DeviceId`; generate Rho, Go and C; the gateway and the
   node examples build against them.
2. **Modes.** The gateway's `mode`, the Kconfig value for the C stack, and the
   link selection each mode implies.
3. **Diagnostics.** The HAL smoke over the registry; `DIAG` on both levels; the
   master's `/api/v1/diagnostics`.
4. **The uplink.** The master's TCP listener, the relay/edge dialer, aggregation
   upward, and routing down by device id.
5. **`robohub-linux`.** The platform and its `net.h`, so a `relay` (and a test
   harness) runs on a host.
6. **Deployment.** A `master` image and service, as the registry and the
   documentation site are deployed.

Phase 1 is the foundation: every later phase speaks the schemas it defines.
