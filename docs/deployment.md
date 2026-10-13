# Deploying the gateway

The gateway is deployed as the registry is — a container image, a systemd unit,
nginx in front — with two differences: it may need a **device**, and it has a
**second port** that is not HTTP.

The shared mechanics (Ubuntu, Docker, systemd, nginx, ISPConfig, TLS, firewall)
are in the registry's `applications/registry/docs/deployment.md`; this page is
what is particular to a gateway.

## What is being deployed

A `robohub-gateway` is one program in four modes (`GATEWAY_MODE`): `node`,
`edge`, `relay`, `master`. What changes between them is which links are open, not
which binary is built:

- A **master** terminates a tree. It needs no serial device: it listens for the
  gateways below on `UPLINK_PORT` and serves the API.
- A gateway with a **board wired to it** (a `node` or `edge`) also needs the
  serial device, passed into the container.
- **PlotJuggler** reads the UDP stream (`PLOTJUGGLER_ADDR`), which is optional
  and off to a collector elsewhere in practice.

## Container

The gateway and the Rho toolchain are separate checkouts, so the image is built
from two contexts — this repository, and `rho` named as a build context. Nothing
is fetched from a network:

```sh
docker build -f deploy/Dockerfile \
    --build-context rho=/path/to/rho \
    -t <dockerhub-user>/robohub-gateway .
```

## systemd

```sh
sudo install -m 0644 deploy/gateway-docker.service /etc/systemd/system/gateway-docker.service
sudo sed -i 's#^Environment=IMAGE=.*#Environment=IMAGE=<dockerhub-user>/robohub-gateway:latest#' \
  /etc/systemd/system/gateway-docker.service
sudo systemctl daemon-reload
sudo systemctl enable --now gateway-docker
curl -fsS http://127.0.0.1:8080/healthz
```

The unit reads `GATEWAY_MODE`, `GATEWAY_AREA`, `REST_PORT`, `UPLINK_PORT` and
`PLOTJUGGLER_ADDR` from its own environment, so a host changes them by editing
the unit, not the image.

### A gateway with a board

Add the device to the run line:

```ini
ExecStart=/usr/bin/docker run --rm --name gateway \
    --device /dev/ttyACM0:/dev/ttyACM0 --group-add dialout \
    -e SERIAL_PORT=/dev/ttyACM0 -e SERIAL_BAUD=115200 \
    ...
```

`dialout` is the group that owns a serial device on Debian and Ubuntu; without it
the container's user cannot open the port, and the gateway logs "no serial link,
ingest disabled" and serves anyway — a failure that is reported, not fatal.

## The two ports

- **`REST_PORT` (8080)** — the HTTP API. Bind it to loopback and put nginx in
  front, as the registry does.
- **`UPLINK_PORT` (5001)** — where the gateways below dial in. It is a raw TCP
  stream carrying RoboHub frames, not HTTP, so nginx's `http` block does not
  apply: publish the port and open it in the firewall, or front it with an nginx
  `stream` proxy (the commented block in `deploy/nginx.conf`).

A relay **dials the master**, so the master is the side that listens and only its
uplink port must be reachable — and a relay behind NAT still connects up. The
tree is acyclic by construction: each gateway names its parent.

```sh
sudo ufw allow 22,80,443/tcp
sudo ufw allow 5001/tcp     # only if the uplink is published directly
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `GATEWAY_MODE` | `master` | `node`, `edge`, `relay` or `master` |
| `GATEWAY_AREA` | `1` | the area this gateway speaks for |
| `REST_PORT` | `8080` | the HTTP API |
| `UPLINK_PORT` | `5001` | where gateways below dial in; `0` listens on none |
| `PLOTJUGGLER_ADDR` | `127.0.0.1:9870` | the UDP collector, if any |
| `SERIAL_PORT` | *(unset)* | the board's device, when there is one |
| `SERIAL_BAUD` | `115200` | the line speed |

There is no secret and no data directory: a gateway holds only what it has heard
since it started, and a reader re-reads it. State is aggregated; nothing is
backed up but the configuration.

## Verifying a deployment

```sh
curl -fsS https://gw.example.com/healthz
curl -fsS https://gw.example.com/api/v1/diagnostics   # mode, area, children, frames
curl -fsS https://gw.example.com/api/v1/nodes         # the devices heard
```

`children` above zero is a gateway below having dialled in and announced. To
check the whole path — a reading up and a command down — the repository's
`tools/relay-smoke.py` does it on one machine, and against a deployed master it
is the same test pointed at the host: run a `relay-host` with
`RHO_PARENT_HOST`/`RHO_PARENT_PORT` at the master.
