---
id: ADR-0001
title: Keep board sessions on disk and restore their counters past the kept mark
status: proposed
date: 2026-11-13
requirements: [GW-0004, GW-SEC-0012, SYS-0023]
supersedes: []
superseded_by: []
---

# Keep board sessions on disk and restore their counters past the kept mark

## Context

Until 0.5.1 the gateway held its board sessions in memory only. After a restart
a board that held a session went on sending under it, and every frame was
refused until the session expired — up to an hour of silence after a power cut.
Forgetting the sessions is safe; keeping them naively is not, because a frame
recorded before the restart would open again if the counters came back where
they were. Writing every counter on every frame would wear an eMMC card.

## Decision

The gateway keeps its live sessions, their keys, the link each is held to and
each counter in a file only its user can read (`GATEWAY_SESSION_FILE`, by
default `sessions` beside the gateway key). The file is written when a session
opens and whenever a counter has moved 64 past what was last written. At start
each unexpired session is restored with both counters 128 past the written
marks: every counter used before the restart lies below the mark plus 64, so no
recorded frame opens after it, and the downward counter is past anything the
board accepted. A session's expiry is reckoned on the wall clock, so it stays
right across the restart.

## Consequences

- A restart loses neither the sessions nor the counters' high-water marks.
- A board that keeps counting under a restored session is below the restored
  mark and is refused until it passes it; ADR-0002 closes that gap.
- The session file holds per-session keys and is a secret, created owner-only.
