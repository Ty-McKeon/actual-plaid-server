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
                 Flask bridge   └── actual_<user>   one container per user
                      ┆
                 provisioner ── docker_gateway ── Docker     (optional)
```

| Service | Role |
| --- | --- |
| `cloudflared` | Connects the stack to Cloudflare. The only way in. |
| `flask_bridge` | Dashboard for linking banks, the SimpleFIN endpoints Actual syncs from, and the check that maps a signed-in user to their Actual container. |
| `caddy` | Serves the single Actual hostname and proxies each request to the signed-in user's own container. |
| `actual_<user>` | One Actual Budget server per user, on its own private network. |
| `provisioner` | Optional. Unprivileged relay between the bridge and the gateway. |
| `docker_gateway` | Optional. The only service with Docker access. Creates each user's Actual container from a fixed template, and removes it when idle. |

Each user's data lives on disk in `actual-data/users/<name>`. There are two ways
their container comes to exist:

- **Provisioner off (default).** You add each user with a script, which defines
  their container in the Compose configuration. Containers run permanently and
  nothing in the stack has access to Docker.
- **Provisioner on.** A user's container is created on their first visit, removed
  after a period without requests, and recreated on their next visit. This saves
  memory and allows self-service, at the cost of running one service with access
  to the Docker socket. See [The provisioner](#the-provisioner).

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
| `PLAID_TRANSACTION_HISTORY_DAYS` | `730` | Days of history Plaid fetches when a bank is first linked (1 to 730). Fixed at that moment for each bank. |
| `PLAID_REDIRECT_URI` | unset | Address banks send the user back to after their sign-in page. See [Linking from a phone](#linking-from-a-phone). |
| `COMPOSE_PROFILES` | unset | Set to `provisioner` to create containers on demand. |
| `ACTUAL_SELF_SERVICE` | `true` | Provisioner only. Give anyone Access lets in a container on their first visit. |
| `ACTUAL_IDLE_MINUTES` | `30` | Provisioner only. Remove a user's container after this long without requests. |
| `ACTUAL_MAX_CONTAINERS` | `10` | Provisioner only. Most Actual containers running at once. |
| `ACTUAL_MEMORY_LIMIT_MB` | `512` | Memory limit per Actual container. |
| `ACTUAL_CPU_LIMIT` | `1` | CPU limit per Actual container. |
| `ACTUAL_PIDS_LIMIT` | `256` | Process limit per Actual container. |
| `ACTUAL_IMAGE` | pinned digest | Official Actual image to run, pinned by digest. |

### 3. Start

Add at least one user (see [Managing users](#managing-users)), then:

```bash
docker compose up -d --build
docker compose logs --tail=50 flask_bridge
```

Container limits are only enforced if the host's kernel supports them. On older
Raspberry Pi OS releases the memory cgroup has to be enabled in the kernel boot
settings first.

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

### Reconnecting a bank

Banks that use their own sign-in page (OAuth), such as Capital One or Chase, ask
the user to approve access again from time to time, typically once a year. The
dashboard shows each institution's status: **Expiring soon** in the last 30 days
before the bank's deadline, and **Reconnect required** once syncing has stopped.

Use **Reconnect** on that institution to sign in again. It keeps the same
connection, so the accounts already linked in Actual carry on syncing. Do not
disconnect and link the bank again instead: that creates new accounts, and each
one would have to be relinked in Actual.

In Production, these banks only appear in Plaid Link once the user's Plaid account
has completed Plaid's OAuth registration in the Plaid dashboard.

### Linking from a phone

By default the bank's sign-in opens in a pop-up, which works on a computer but
can be blocked on phones, especially inside another app's built-in browser. To
make it reliable there, give Plaid an address to send the user back to:

1. Set `PLAID_REDIRECT_URI` in `.env` to the bridge's public address followed by
   `/oauth-return`, for example `https://bridge.example.com/oauth-return`, and
   recreate the bridge with `docker compose up -d flask_bridge`.
2. Each user adds the same address under **Allowed redirect URIs** in their own
   Plaid dashboard (Developers, then API).

A user who has not done step 2 can still link through the pop-up; the bridge
falls back to it when Plaid rejects the address.

Only one bank connection can be in progress per browser profile, across tabs.
Finish that connection or use **Cancel pending connection** before starting
another. If returning from a bank fails temporarily, reload the return page to
retry. Continuation and saving are restricted to the user who started the flow.

Link sessions and the last successfully fetched account snapshots are encrypted
in the bridge database. On a reconnect failure, snapshots retain their original
balance timestamp and report that attention is needed. If no snapshot exists yet,
sync returns a temporary failure until the bank is reconnected.

Rebuilding the bridge adds these two tables automatically; existing tables and
credentials are preserved. Back up the bridge database before updating, and
reload open dashboard tabs after the update to pick up the new Link workflow.

## Managing users

Install the user-management script's dependency in a virtual environment once:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r scripts/requirements.txt
source .venv/bin/activate
```

Activate that environment when managing users. Users are added by name:

```bash
scripts/actual-users.py add alice alice@example.com
scripts/actual-users.py list
docker compose up -d --remove-orphans
```

The script records the assignment in `config/actual-users.json`, which the bridge
reads to route requests, and writes `compose.override.yaml`, which defines a
container and a private network for each user. Docker Compose picks that file up
automatically, so the last command starts the new user's container or removes a
deleted one. It also restarts Caddy and the bridge to attach them to the user's
network, which interrupts everyone for a few seconds.

Several email addresses can share one container by giving them the same name.
Removing a user keeps their data in `actual-data/users/<name>`. The email must
also be allowed by the Access policy for the Actual hostname.

To remove an assignment and its running container:

```bash
scripts/actual-users.py remove alice
docker compose up -d --remove-orphans
```

This removes access to Actual through the proxy. Remove the email from the
Cloudflare Access policies and revoke their SimpleFIN credentials too when
offboarding someone from the whole service.

## The provisioner

The provisioner creates each user's container on demand instead. It is off by
default because it needs one service, `docker_gateway`, to hold the Docker
socket, and access to Docker is equivalent to root on the host. The gateway is
kept on an internal network, accepts only a user name, and builds every container
from a fixed template; see [DOCKER_DEPLOYMENT.md](DOCKER_DEPLOYMENT.md).

Turn it on if memory is tight or you want self-service:

```bash
echo "COMPOSE_PROFILES=provisioner" >> .env
scripts/actual-users.py sync
docker compose up -d --build --remove-orphans
```

With it on:

- A container is created on a user's first visit and removed after
  `ACTUAL_IDLE_MINUTES` without requests. The first request after that takes a
  couple of seconds.
- With `ACTUAL_SELF_SERVICE=true`, anyone the Access policy lets in gets a
  container without being added first, named after a hash of their verified
  email, such as `actual_u-5ff860bf1190596c`. Set it to `false` to serve only the
  users added with the script.
- `scripts/actual-users.py` only assigns names, and its changes take effect
  immediately. Assign a name before a user's first visit; changing it later means
  renaming their folder in `actual-data/users/`.
- The gateway logs a warning at startup if the host does not enforce one of the
  container limits.

To turn it off again, stop its services before removing the setting. Deleting the
line from `.env` alone leaves them running:

```bash
docker compose --profile provisioner rm -sf docker_gateway provisioner
# remove COMPOSE_PROFILES=provisioner from .env, then:
scripts/actual-users.py sync
docker compose up -d --remove-orphans
```

Self-service users are not carried over: add each one by name with the script,
using their existing folder name in `actual-data/users/` to keep their data.

## Data and backups

| Location | Contents |
| --- | --- |
| `bridge-data` (Docker volume) | The bridge's database: encrypted Plaid credentials, linked institutions, SimpleFIN credentials. |
| `actual-data/users/<name>/` | Each user's Actual Budget server data. |
| `config/actual-users.json` | Name assignments. |
| `.env` | The tunnel token and the encryption key. |

Back up all four. The bridge uses SQLite WAL mode, so copying only its live
`bridge.db` file can miss recent writes. Create a consistent snapshot with
SQLite's online backup API, then copy that snapshot out of the volume:

```bash
docker exec flask_bridge python -c '
import sqlite3
with sqlite3.connect("file:/data/bridge.db?mode=ro", uri=True) as source:
    with sqlite3.connect("/data/bridge-backup.db") as target:
        source.backup(target)
        assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
'
docker cp flask_bridge:/data/bridge-backup.db ./bridge-backup.db
```

Run one backup at a time and protect the exported file. For the Actual data
directories, stop the user containers during copying or use a consistent storage
snapshot. Verify restoration before relying on the backups.

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

With the optional provisioner enabled, restarting the gateway removes the running
Actual containers. They are recreated on each user's next request, with their
data intact.

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

Frontend Link regression tests use Node 18 or later:

```bash
node --test tests/test_plaid_frontend.cjs
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
- By default no service has access to Docker. With the provisioner enabled, only
  `docker_gateway` does, and it accepts a user name and nothing else. Docker
  access is equivalent to root on the host, so keep the host itself restricted.
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
