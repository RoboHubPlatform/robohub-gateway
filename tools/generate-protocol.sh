#!/bin/sh
# Regenerate the Rho payloads and transport from the schema.
#
# The output, src/gateway/protocol.rho, is committed so a build needs no
# generator step, but it must be regenerated whenever schemas/telemetry.rho
# changes. The node's C is generated from the same schema, which is what keeps
# the two ends on one description.
#
# Run from the repository root, after `rho build .` has built the toolchain:
#
#     tools/generate-protocol.sh
set -eu

rho protocol generate --target rho --out src/gateway schemas/telemetry.rho
