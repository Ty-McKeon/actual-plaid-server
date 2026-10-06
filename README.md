# Actual Budget Plaid Bridge

A self-hosted bridge that lets [Actual Budget](https://actualbudget.org) sync bank
transactions through [Plaid](https://plaid.com), for a small group of people who
each get their own private Actual Budget server.

Actual Budget cannot talk to Plaid directly, but it can sync from any server that
speaks the SimpleFIN protocol. The bridge is that server: each user links their
banks with Plaid in the bridge's dashboard, and Actual Budget pulls accounts and
transactions from the bridge as if it were SimpleFIN.

Everything sits behind a Cloudflare Tunnel and Cloudflare Access, so nothing is
exposed on the host and no ports need forwarding.

## How it works

```
                 Cloudflare Access + Tunnel
                           │
                      cloudflared
                      │         │
        bridge hostname         Actual hostname
                      │         │
                      │       Caddy ── asks the bridge "whose request is this?"
                      │         │
                 Flask bridge   └── actual_<user>   one container per user,
                      │                              started on demand
                 provisioner ── docker_gateway ── Docker
```

| Service | Role |
| --- | --- |
| `cloudflared` | Connects the stack to Cloudflare. The only way in. |
| `flask_bridge` | Dashboard for linking banks, the SimpleFIN endpoints Actual syncs from, and the check that maps a signed-in user to their Actual container. |
| `caddy` | Serves the single Actual hostname and proxies each request to the signed-in user's own container. |
| `provisioner` | Unprivileged relay between the bridge and the gateway. |
| `docker_gateway` | The only service with Docker access. Creates each user's Actual container from a fixed template, and removes it when idle. |
| `actual_<user>` | One Actual Budget server per user, on its own private network. |

A user's Actual container is created on their first visit, removed after a period
without requests, and recreated on their next visit. Their data stays on disk in
`actual-data/users/<name>` throughout.

See [DOCKER_DEPLOYMENT.md](DOCKER_DEPLOYMENT.md) for the network layout, the
isolation between users, and the rollout procedure.

## Requirements

- A Linux host (or Docker Desktop) with Docker and Docker Compose
- A domain on Cloudflare, with a Cloudflare Tunnel and Cloudflare Access (Zero Trust)
- A Plaid account. Each user enters their own Plaid client ID and secret in the
  dashboard; Sandbox and Production are supported, for US institutions.

## Setup

### 1. Cloudflare

1. Create a tunnel and copy its token.
2. Add two public hostnames to the tunnel:

   | Hostname | Service |
   | --- | --- |
   | `bridge.example.com` | `http://flask_bridge:8080` |
   | `budget.example.com` | `http://caddy:80` |

3. Protect both hostnames with Cloudflare Access, with a policy that allows only
   the people who should use the system. Note the Application Audience (AUD) tag
   of each Access application, and your team domain.

### 2. Configuration

```bash
git clone https://github.com/Ty-McKeon/actual-plaid-server.git
cd actual-plaid-server
cp .env.example .env
```

Fill in `.env`:

| Variable | Purpose |
| --- | --- |
| `CLOUDFLARE_TOKEN` | Tunnel token for `cloudflared`. |
| `ENCRYPTION_KEY` | Encrypts Plaid secrets and bank access tokens in the database. Required. Generate it with the command in `.env.example`. |
| `CLOUDFLARE_TEAM_DOMAIN` | Your Access team domain, for example `myteam.cloudflareaccess.com`. |
| `CLOUDFLARE_AUD` | AUD tag of the Access application protecting the bridge hostname. |
| `CLOUDFLARE_ACTUAL_AUD` | AUD tag of the Access application protecting the Actual hostname. Leave blank if one application covers both. |

**Back up `ENCRYPTION_KEY`.** If it is lost or changed, the stored Plaid
credentials cannot be decrypted and every bank has to be linked again.

Optional settings, with their defaults, are described in `.env.example`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `ACTUAL_SELF_SERVICE` | `true` | Give anyone Access lets in a container on their first visit. |
| `ACTUAL_IDLE_MINUTES` | `30` | Remove a user's container after this long without requests. |
| `ACTUAL_MAX_CONTAINERS` | `10` | Most Actual containers running at once. |
| `ACTUAL_MEMORY_LIMIT_MB` | `512` | Memory limit per Actual container. |
| `ACTUAL_CPU_LIMIT` | `1` | CPU limit per Actual container. |
| `ACTUAL_PIDS_LIMIT` | `256` | Process limit per Actual container. |
| `ACTUAL_IMAGE` | pinned digest | Official Actual image to run, pinned by digest. |

### 3. Start

```bash
docker compose up -d --build
docker compose logs --tail=50 docker_gateway flask_bridge
```

The gateway logs a warning at startup if the host does not enforce one of the
container limits. On older Raspberry Pi OS releases the memory cgroup has to be
enabled in the kernel boot settings first.

## Using it

Each user does this once:

1. Open the bridge hostname and sign in through Cloudflare Access.
2. Enter their Plaid client ID and secret, then link their bank accounts.
3. Generate a SimpleFIN setup token. It is single-use and expires after 24 hours
   if unclaimed.
4. Open the Actual hostname, set a password for their Actual server, and create
   or import a budget.
5. In Actual, go to bank sync, set up SimpleFIN, and paste the setup token. Their
   accounts can then be linked and synced from Actual.

Every user should set a strong Actual password. It is what protects their server
from anything else running on the host.

## Managing users

With self-service on, nobody needs adding: a user's container is named after a
hash of their verified email, such as `actual_u-5ff860bf1190596c`.

To give someone a readable name, let several email addresses share one
container, or allow specific people in when self-service is off, assign them a
name:

```bash
scripts/actual-users.py add alice alice@example.com
scripts/actual-users.py list
scripts/actual-users.py remove alice
```

Assignments are stored in `config/actual-users.json` and take effect
immediately. Assign a name before the user's first visit; changing it later
means renaming their folder in `actual-data/users/`. Removing a user keeps their
data.

The email must also be allowed by the Access policy for the Actual hostname.

## Data and backups

| Location | Contents |
| --- | --- |
| `bridge-data` (Docker volume) | The bridge's database: encrypted Plaid credentials, linked institutions, SimpleFIN credentials. |
| `actual-data/users/<name>/` | Each user's Actual Budget server data. |
| `config/actual-users.json` | Name assignments. |
| `.env` | The tunnel token and the encryption key. |

Back up all four. To copy the bridge database out of its volume:

```bash
docker cp flask_bridge:/data/bridge.db ./bridge-backup.db
```

Do not run `docker compose down --volumes` on a deployment you care about; it
deletes the bridge database.

## Updating

Images are pinned by digest, so nothing updates on its own. To update, change the
pinned digests as described in [DOCKER_DEPLOYMENT.md](DOCKER_DEPLOYMENT.md), back
up your data, then:

```bash
git pull
docker compose up -d --build
```

Restarting the gateway removes the running Actual containers. They are recreated
on each user's next request, with their data intact.

## Development

The repository includes a VS Code dev container that runs the bridge with Flask's
debugger and a single Actual Budget server, without the tunnel, Caddy or the
provisioner. In development mode (`FLASK_DEBUG=1`) the Cloudflare login is
bypassed, so never set it on a deployed instance. `ENCRYPTION_KEY` is still
required.

Run the tests with Python 3.11 or later and `bridge/requirements.txt` installed:

```bash
python -m unittest discover -s tests
```

`tests/docker_integration.py` exercises the whole stack against real Docker in a
disposable project: per-user routing, isolation between users, SimpleFIN sync,
idle cleanup and data persistence. It needs Docker and registry access, and does
not read `.env` or touch a running deployment.

```bash
python tests/docker_integration.py
```

Code is formatted and linted with [Ruff](https://docs.astral.sh/ruff/).

## Security notes

- Plaid secrets and bank access tokens are encrypted at rest; SimpleFIN passwords
  are stored only as hashes.
- Access tokens are verified against Cloudflare's signing keys, including the
  audience of the specific Access application.
- Each Actual container runs on its own network with memory, CPU and process
  limits, and cannot reach another user's container.
- Only `docker_gateway` can reach Docker, and it accepts a user name and nothing
  else. Docker access is equivalent to root on the host, so keep the host itself
  restricted.
- Access logs redact SimpleFIN claim IDs and leave out query strings.

Budget files and the bridge's user metadata are not encrypted by this project.
Protect the host's storage and your backups accordingly.

## License

Copyright (C) 2026 Tyler McKeon

This program is free software: you can redistribute it and/or modify it under the
terms of the GNU Affero General Public License, version 3, as published by the
Free Software Foundation. It is distributed without any warranty; see
[LICENSE](LICENSE) for the full terms.

If you run a modified version of this software as a network service, the license
requires you to offer its source code to the users of that service.
