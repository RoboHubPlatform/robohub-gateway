# Changelog

All notable changes to this module are recorded here, one section per release.

## [0.2.0] - 2026-11-01

The gateway page shows a frame history, newest first, with selectable windows
of 10, 25, 50, 100, 500 and 1000 rows and CSV export. A node's `timestamp_ms`
is its own uptime; the gateway records the wall-clock `received_at_ms` beside
it and renders that as local date/time. Page and API carry both.
