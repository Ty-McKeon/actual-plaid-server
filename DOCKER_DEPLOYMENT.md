# Docker isolation and rollout

The deployment now uses a fixed-operation Docker gateway and a separate bridge
network per Actual user. These changes are prepared in the repository; applying
them to the running deployment requires a rebuild and controlled restart.

## Network and privilege boundaries

| Network | Members |
| --- | --- |
| `budget_net` | Cloudflared, Caddy, Flask bridge |
| `provisioner_net` (internal) | Flask bridge, unprivileged provisioner relay |
| `docker_control_net` (internal) | Provisioner relay, Docker gateway |
| `<project>_actual_net_<user>` | One Actual container, Caddy, Flask bridge |

Actual containers have no published ports and never join the shared or control
networks. They retain outbound internet access through their own bridge network.
Caddy and Flask are shared trusted services, and are intentionally reachable by
each tenant. Flask's Cloudflare and SimpleFIN authentication remains required.
Docker's normal bridge firewall rules must remain enabled.

Only `docker_gateway` holds `/var/run/docker.sock`. The provisioner relay runs as
UID/GID 65534 with no mounts, a read-only filesystem, no capabilities, and
no-new-privileges. Neither service publishes a port on the host.

The gateway exposes only:

* `GET /health`
* `POST /containers/<validated-name>/ensure`, with no body

There is no generic Docker API forwarding. The gateway builds the configuration
itself using its startup policy: official Actual image digest, exactly one tenant
data bind, one tenant network, project ownership labels, configured resource
limits, NET_RAW removed, and no-new-privileges. Callers cannot supply an image,
host path, container configuration, privileged mode, device, or network setting.
Requests and per-user locks are bounded; capacity applies to containers and networks.

The gateway itself remains privileged by virtue of the Docker socket. Compromising
that service, or obtaining Docker access on the host, defeats these boundaries.
Keep it patched and the host restricted. A compromised relay can consume the
configured tenant capacity, but cannot submit arbitrary Docker configurations.

## Lifecycle and existing containers

Networks and containers carry project/user ownership labels. Caddy and Flask are
found using this project's Compose service labels, so replacement containers are
reattached automatically on the next user request. Idle cleanup removes the
Actual container, detaches trusted peers, and removes its network. User data is
retained in `actual-data/users/<name>`. Partial creation and orphan networks are
recovered on subsequent cleanup passes.

Existing managed containers with the old shared-network template are recreated
when next requested. A legacy container without a project ownership label is
accepted for migration only if its original managed/user labels and exact data
bind match this deployment. Foreign names, mounts, networks, or endpoints cause
a failure rather than being adopted or modified. Current managed containers with
old image/resource settings are also recreated with their existing data bind.

Unmanaged hand-created Actual containers are now refused by this routing path;
move them to the managed lifecycle explicitly before relying on this proxy.

## Image pins and updates

Cloudflared, Caddy, Actual, and both Python base images are pinned to multi-platform
registry digests resolved on 2026-10-06. The pins support the architectures listed
by the upstream manifest indexes, including amd64 and arm64. `ACTUAL_IMAGE`, if
set, must have this format:

```text
actualbudget/actual-server@sha256:<64 lowercase hexadecimal characters>
```

To update a pin, select an upstream version, inspect its registry manifest, and
copy the top-level `digest` into the relevant references:

```bash
docker buildx imagetools inspect actualbudget/actual-server:<selected-version> \
  --format '{{json .Manifest}}'
```

Actual's reference appears in Compose, the gateway's `DEFAULT_IMAGE`, the local
development Compose file, the environment example, and the Docker integration
test. Caddy is referenced by Compose and the integration test. Python is in both
Dockerfiles. Cloudflared is in Compose. Update these together, review upstream
release notes, scan the new images, and repeat the checks below before rollout.
Digest pinning prevents silent tag changes; it does not establish that an image
has no vulnerabilities. Image CVE scanning is not part of the Python pip audit.

## Validate and apply

Before rollout, preserve the existing `.env` and encryption key, back up
`bridge-data` or its named volume as appropriate, and back up `actual-data/users`.
If `.env` contains `ACTUAL_IMAGE` with a floating tag, replace it with a verified
digest or remove that override to use the pinned default. Budget migrations caused
by an Actual version update need a restorable backup too.

Run these checks with Python 3.11+ and the bridge requirements installed:

```bash
python -m unittest discover -s tests
docker compose config --quiet
docker compose -f compose.yaml -f .devcontainer/docker-compose.yml config --quiet
python tests/docker_integration.py
```

The integration script builds and starts a uniquely named disposable project with
temporary data and test authentication. It never loads `.env`. It checks both users'
Actual routing, SimpleFIN claim/Basic-auth access, cross-tenant DNS and IP isolation,
gateway isolation and policy rejection, resource settings, crash recovery, idle
network cleanup, and data-preserving recreation. It cleans up its own resources.
It does not call Plaid or prove the production Cloudflare policy is configured.

During a maintenance window, deploy from this repository:

```bash
docker compose build flask_bridge provisioner docker_gateway
docker compose up -d
docker compose ps
docker compose logs --tail=100 docker_gateway provisioner flask_bridge
```

Stopping the old provisioner removes its disposable managed containers while
retaining their data. The new gateway recreates them on demand. Running `up -d`
for the full project also applies the pinned Caddy/Cloudflared images. Do not use
`down --volumes` for production.

Confirm one real user's dashboard, Actual login, and bank sync after rollout. Verify
that their Actual container has exactly one private tenant network, no published
ports, and the configured memory/CPU/process limits. Monitor memory failures during
large imports and adjust the existing `ACTUAL_*_LIMIT` settings if necessary.

Storage encryption, disk quotas, backup controls, and HTTPS for connections crossing
untrusted networks remain host/deployment responsibilities. Shared trusted services
and the Docker host are still part of every tenant's trust boundary.
