---
id: ADR-0003
title: Bind a session to its link and refuse a replayed hello by its nonce
status: proposed
date: 2026-11-14
requirements: [GW-SEC-0004, GW-SEC-0005, GW-0001, SYS-0020]
supersedes: []
superseded_by: []
---

# Bind a session to its link and refuse a replayed hello by its nonce

## Context

A hello carries no time and is authenticated by the board's key alone, so a
recorded hello is as valid the second time as the first. Before 0.6.0 a
replayed hello opened a new session and could take the serial link's session
from the board that held it, and a frame sealed under a session opened on one
link was accepted on another — a frame captured on Wi-Fi could be injected on a
different connection.

## Decision

- The gateway remembers each board's latest eight hello nonces, keeps them with
  the sessions across a restart, and does not answer a hello that repeats one;
  the refusal changes neither the sessions nor the sessions file.
- A session is held to the link its hello came on: the serial link, one uplink
  connection, the HTTP bridge, or a relay and its port. A frame naming a session
  the link it arrived on does not hold is refused.
- On the serial link a new session becomes the one commands are sealed under
  only once a frame sealed under it has come up, or at once when the link holds
  no live session.
- A live session's id is never given out again.

## Consequences

- A replayed hello cannot open a session or disturb the board's own.
- A board that moves between links opens a new session on the new one.
- The nonce list is bounded (eight per board). A hello older than that is
  answered, but the session it opens is useless to whoever replayed it — its keys
  derive from the board's key — and it takes no link from the board, because it
  never carries a frame.
