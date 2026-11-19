---
id: ADR-0002
title: Ask each board with a restored session for a new one (SESSION_RESET)
status: proposed
date: 2026-11-14
requirements: [GW-0004, GW-SEC-0013, SYS-0023]
supersedes: []
superseded_by: []
---

# Ask each board with a restored session for a new one (SESSION_RESET)

## Context

With sessions restored 128 past the kept mark (ADR-0001), a board that did not
see the restart goes on counting below that mark, and its frames are refused
until its counter passes it — at a sensor's rate, minutes. Dropping the restored
sessions instead would leave the board unheard until its session expired. A
plain "please say hello" message would let anyone on the link force boards to
renegotiate.

## Decision

The gateway sends SESSION_RESET (message 0x0016, reason 1: the gateway
restarted), sealed under the restored session's downward key with a counter past
anything the board accepted, to each board holding a restored session: on the
serial link as soon as it opens; through a relay (inside RELAYED) or on an uplink
connection at the board's first refused frame whose tag is the session's and
whose counter lies in the restored window. It is sent at most once in 10 s and
five times per session (every attempt counts, one that could not be written
included), and each is logged; after the fifth the gateway stops asking, so a
board that cannot take the request is not sent one every 10 s for the life of
the session, and is heard again once it says hello on its own or its counter
passes the restored mark. Once the board's new session carries
a frame, the restored one is dropped. A board reached only over the HTTP bridge
is not asked.

## Consequences

- A board is heard again within seconds of a restart, with no one touching it.
- Only the holder of the session's key can make a board drop its session.
- A board needs a robohub-transport that knows SESSION_RESET; an older firmware
  ignores it and is heard once its counter passes the restored mark.
