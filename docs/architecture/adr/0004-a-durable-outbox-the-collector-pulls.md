---
id: ADR-0004
title: A durable outbox that collectors pull and acknowledge on their own cursors
status: proposed
date: 2026-11-05
requirements: [GW-0005, GW-TEL-0002, GW-TEL-0003, SYS-0024]
supersedes: []
superseded_by: []
---

# A durable outbox that collectors pull and acknowledge on their own cursors

## Context

Mesh keeps the history of every reading, but Mesh is restarted, updated and
sometimes not running, and a gateway may sit on another host. Pushing readings
to Mesh would lose what arrives while Mesh is away, or make the gateway hold
retries for a peer it cannot see. Writing each reading to an SD or eMMC card as
it arrives wears it.

## Decision

The gateway normalizes every accepted reading into one record per signal and
appends it to a file on disk with a per-gateway sequence that continues across
restarts, even after the file was emptied. The file is written through a buffer
and synced in batches. A collector pulls pages (`GET /api/v1/telemetry/outbox
?after=&limit=`) and acknowledges the sequence it has committed
(`POST /api/v1/telemetry/ack`). Since 2026-11-12 the acknowledgement is kept per
consumer — the subject of its token — and a record is removed only when every
known consumer has acknowledged it. `OUTBOX_MAX_ROWS` (200000) bounds what is
kept when nobody collects; the oldest records are dropped and counted.

## Consequences

- Delivery is at least once; Mesh keys a reading by gateway and sequence, so a
  page fetched twice is stored once.
- A second reader (a backup, a debugger) is harmless.
- A consumer that stops acknowledging holds records until the row bound drops
  them; there is no retention by age.
