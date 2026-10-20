# robohub-gateway, as a container.
#
# The gateway is a single statically-built Go server configured entirely by the
# environment — the serial link it reads, the ports it serves, the collector it
# streams to — so the image carries the binary and nothing else, and a board is
# configured at run time rather than at build time.
#
# Built and carried to a board by `rho deploy -target uno-q -docker`: the image
# is built here, saved, copied, loaded and started on the target. `rho build`
# produces build/robohub-gateway beside this file, which is what is copied in.
#
# @project RoboHub Platform
# @license MIT

FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY build/robohub-gateway /usr/local/bin/robohub-gateway

# The defaults a deployment overrides. A serial link is a device, so a board
# that reads one adds `--device /dev/ttyUSB0` (or names the tty) to the run
# arguments.
ENV GATEWAY_MODE=master \
    GATEWAY_AREA=1 \
    REST_PORT=8080

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/robohub-gateway"]
