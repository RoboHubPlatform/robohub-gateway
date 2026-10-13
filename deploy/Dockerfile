# Build and run the RoboHub gateway.
#
# The gateway is its own repository, and the Rho toolchain is another, so the
# image is built from two contexts: this one, and `rho` named as a build context.
# Nothing is fetched from a network — a build reads the two checkouts it is given.
#
#   docker build -f deploy/Dockerfile \
#       --build-context rho=/path/to/rho \
#       -t <dockerhub-user>/robohub-gateway .
#
# The image carries a properly installed rho and starts the service through
# `rho run` exactly as the registry image does: the container generates the Go
# project from the Rho sources and runs it, so it needs a Go toolchain for that
# build.
#
# A gateway binds to hardware when it has it and answers without it when it does
# not: a master that aggregates gateways over its uplink needs no serial device.
# Pass a device in only for a gateway with a board wired to it.

FROM golang:1.23-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --create-home --home-dir /home/rho --shell /usr/sbin/nologin rho

# The toolchain, from the `rho` build context.
COPY --from=rho . /src/rho
# The service, from this repository.
COPY . /src/gateway

# Build the core toolchain and install it.
RUN cd /src/rho \
 && CGO_ENABLED=0 go build -trimpath -o ./bin/rho     ./cmd/rho \
 && CGO_ENABLED=0 go build -trimpath -o ./bin/rho-deps ./cmd/rho-deps \
 && HOME=/home/rho ./bin/rho install --force \
 && HOME=/home/rho ./scripts/check.sh publish rho-fmt rho-log

# The service, beside the standard library the toolchain compiles against.
RUN mkdir -p /app \
 && cp -a /src/rho/std /app/std \
 && cp -a /src/gateway /app/gateway \
 && rm -rf /app/gateway/build \
 && chown -R rho:rho /app /home/rho

USER rho
WORKDIR /app/gateway

# A Go home the service can write, because `rho run` compiles at start-up.
#
# GATEWAY_MODE and GATEWAY_AREA say what this gateway is; REST_PORT is where the
# API listens and UPLINK_PORT where gateways below it dial in, and zero uplink
# means none. A deployment sets these; the defaults are a master that only
# serves.
ENV HOME=/home/rho \
    PATH=/home/rho/.rho/bin:$PATH \
    GATEWAY_MODE=master \
    GATEWAY_AREA=1 \
    REST_PORT=8080 \
    UPLINK_PORT=5001 \
    PLOTJUGGLER_ADDR=127.0.0.1:9870 \
    GOFLAGS=-buildvcs=false \
    GOPATH=/home/rho/go \
    GOMODCACHE=/home/rho/go/pkg/mod \
    GOCACHE=/tmp/go-build

# Build once as the service user so the module cache is populated and owned by
# the user that rebuilds at start-up; the output is discarded.
RUN rho build . \
 && rm -rf /app/gateway/build

# The REST API, and the uplink gateways below dial in on.
EXPOSE 8080 5001

# Started by rho: it generates the Go project from the Rho sources and runs it.
ENTRYPOINT ["rho", "run", "."]
