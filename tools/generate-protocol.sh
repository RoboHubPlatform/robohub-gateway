#!/bin/sh
# Regenerate the Rho payloads and transport from the schemas.
#
# The schemas are split by who speaks them:
#
#   schemas/common.rho    the identity and families both levels share
#   schemas/link.rho      L2, board  <-> gateway   (a node generates this)
#   schemas/uplink.rho    L1, gateway <-> gateway  (a relay generates this)
#   schemas/gateway.rho   both levels              (a gateway generates this)
#
# Each root generates into its own directory, because the generator writes a
# fixed `protocol.*` name and merges every schema a root includes.
#
# The output is committed so a build needs no generator step, but it must be
# regenerated whenever a schema changes. The same schemas generate the C for a
# node and the Rho for a gateway, which is what keeps the ends on one
# description.
#
# Run from the repository root, after `rho build .` has built the toolchain:
#
#     tools/generate-protocol.sh
set -eu

rho protocol generate --target rho --out src/gateway/link  schemas/link.rho
rho protocol generate --target rho --out src/gateway/uplink schemas/uplink.rho
rho protocol generate --target rho --out src/gateway/wire   schemas/gateway.rho
