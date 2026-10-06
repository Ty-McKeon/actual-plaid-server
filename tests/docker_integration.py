"""Opt-in real Docker checks. Uses a unique project and disposable data only.

Run: python3 tests/docker_integration.py
Requires Docker Compose, registry access, and permission to build/create containers.
Does not load .env or touch the deployed project's containers or data.
"""

import base64
import http.client
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
ACTUAL = "actualbudget/actual-server@sha256:24645e971da6bb1a1f953d6859e307a0b29be1e8b4130a35b3220c209b5b860c"
CADDY = "caddy:2-alpine@sha256:d8542f48d34a9cf4e4c11a478865229840e87e4c96ea3f439101f31a5d35f75f"


def run(*args, check=True, timeout=300):
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False
    )
    if check and result.returncode:
        raise RuntimeError(
            f"Command failed: {args[0:3]}\n{result.stderr[-3000:]}\n{result.stdout[-3000:]}"
        )
    return result.stdout.strip()


def request(url, method="GET", data=None, headers=None):
    with urlopen(
        Request(url, data=data, method=method, headers=headers or {}), timeout=60
    ) as response:
        return response.status, response.read()


def wait_for(check, timeout=60):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (
            URLError,
            RuntimeError,
            AssertionError,
            http.client.HTTPException,
            OSError,
        ) as err:
            last = err
        time.sleep(1)
    raise AssertionError(f"Timed out waiting for Docker check: {last}")


def token(email):
    # Unverified tokens are accepted only by this isolated, explicit development fixture.
    encode = lambda value: (
        base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    )
    return (
        encode({"alg": "HS256"})
        + "."
        + encode({"sub": email, "email": email})
        + ".c2ln"
    )


def main():
    project = "actualaudit-" + uuid.uuid4().hex[:10]
    tenants = ["it-" + uuid.uuid4().hex[:10] + suffix for suffix in ("a", "b")]
    print(f"Testing disposable project {project}", flush=True)
    with tempfile.TemporaryDirectory(prefix="actual-docker-audit-") as folder:
        directory = Path(folder)
        users = directory / "users"
        users.mkdir()
        assignments = directory / "config"
        assignments.mkdir()
        (assignments / "actual-users.json").write_text(
            json.dumps(dict(zip(("alice@example.com", "bob@example.com"), tenants)))
        )
        configuration = {
            "services": {
                "flask_bridge": {
                    "build": {"context": str(ROOT / "bridge"), "target": "production"},
                    "environment": {
                        "FLASK_DEBUG": "1",
                        "ENCRYPTION_KEY": base64.urlsafe_b64encode(
                            os.urandom(32)
                        ).decode(),
                        "DATABASE_URI": "sqlite:////data/bridge.db",
                        "ACTUAL_USERS_FILE": "/config/actual-users.json",
                        "ACTUAL_PROVISIONER_URL": "http://provisioner:8090",
                        "BASE_URL": "http://flask_bridge:8080",
                    },
                    "volumes": ["bridge-test:/data", f"{assignments}:/config:ro"],
                    "networks": ["edge", "provisioner_net"],
                    "ports": ["127.0.0.1::8080"],
                },
                "caddy": {
                    "image": CADDY,
                    "volumes": [f"{ROOT / 'caddy/Caddyfile'}:/etc/caddy/Caddyfile:ro"],
                    "networks": ["edge"],
                    "ports": ["127.0.0.1::80"],
                },
                "provisioner": {
                    "build": {"context": str(ROOT / "provisioner"), "target": "relay"},
                    "environment": {"DOCKER_GATEWAY_URL": "http://docker_gateway:8091"},
                    "networks": ["provisioner_net", "control"],
                    "read_only": True,
                    "cap_drop": ["ALL"],
                    "security_opt": ["no-new-privileges:true"],
                },
                "docker_gateway": {
                    "build": {
                        "context": str(ROOT / "provisioner"),
                        "target": "gateway",
                    },
                    "environment": {
                        "ACTUAL_IMAGE": ACTUAL,
                        "ACTUAL_IDLE_MINUTES": "0.5",
                        "ACTUAL_REAP_INTERVAL_SECONDS": "1",
                        "ACTUAL_MAX_CONTAINERS": "2",
                    },
                    "volumes": [
                        "/var/run/docker.sock:/var/run/docker.sock",
                        f"{users}:/actual-data:ro",
                    ],
                    "networks": ["control"],
                    "stop_grace_period": "45s",
                    "read_only": True,
                    "cap_drop": ["ALL"],
                    "security_opt": ["no-new-privileges:true"],
                },
            },
            "networks": {
                "edge": {},
                "provisioner_net": {"internal": True},
                "control": {"internal": True},
            },
            "volumes": {"bridge-test": {}},
        }
        compose_file = directory / "compose.json"
        compose_file.write_text(json.dumps(configuration))
        compose = [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "-p",
            project,
            "-f",
            str(compose_file),
        ]
        ids = {}
        try:
            print("Building and starting test services", flush=True)
            run(*compose, "up", "-d", "--build", timeout=900)
            for service in configuration["services"]:
                ids[service] = run(*compose, "ps", "-q", service)
            bridge_url = "http://" + run(*compose, "port", "flask_bridge", "8080")
            caddy_url = "http://" + run(*compose, "port", "caddy", "80")
            wait_for(lambda: request(bridge_url + "/api/plaid/items")[0] == 200)
            for email in ("alice@example.com", "bob@example.com"):
                wait_for(
                    lambda email=email: (
                        request(
                            caddy_url + "/",
                            headers={"Cf-Access-Jwt-Assertion": token(email)},
                        )[0]
                        == 200
                    ),
                    timeout=180,
                )
            print("Both users routed to running Actual servers", flush=True)
            a, b = ["actual_" + name for name in tenants]
            inspect = lambda name: json.loads(run("docker", "inspect", name))[0]
            for name, container in zip(tenants, (a, b)):
                info = inspect(container)
                assert set(info["NetworkSettings"]["Networks"]) == {
                    f"{project}_actual_net_{name}"
                }
                assert info["HostConfig"]["Memory"] == 512 * 1024 * 1024
                assert info["HostConfig"]["NanoCpus"] == 1_000_000_000
                assert info["HostConfig"]["PidsLimit"] == 256
                assert (
                    not info["HostConfig"]["Privileged"]
                    and not info["HostConfig"]["PortBindings"]
                )
            node = lambda container, code: run(
                "docker", "exec", container, "node", "-e", code, timeout=15
            )
            # Test direct IP access too: failed DNS alone does not prove isolation.
            bob_ip = next(iter(inspect(b)["NetworkSettings"]["Networks"].values()))[
                "IPAddress"
            ]
            gateway_ip = next(
                iter(
                    inspect(ids["docker_gateway"])["NetworkSettings"][
                        "Networks"
                    ].values()
                )
            )["IPAddress"]
            for host, port in (
                (b, 5006),
                (bob_ip, 5006),
                ("docker_gateway", 8091),
                (gateway_ip, 8091),
                ("provisioner", 8090),
            ):
                node(
                    a,
                    f"fetch('http://{host}:{port}/', {{signal: AbortSignal.timeout(1500)}}).then(() => process.exit(1)).catch(() => process.exit(0))",
                )
            assert inspect(ids["provisioner"])["Config"]["User"] == "65534:65534"
            assert not inspect(ids["provisioner"])["Mounts"]
            print(
                "Cross-tenant and gateway access blocked; relay has no socket",
                flush=True,
            )
            # Exercise real bridge claim exchange and Basic-auth sync from an Actual container.
            status, body = request(
                bridge_url + "/simplefin/token",
                "POST",
                headers={"Sec-Fetch-Site": "same-origin"},
            )
            assert status == 201
            claim = urlsplit(json.loads(body)["claim_url"]).path
            _, body = request(bridge_url + claim, "POST")
            access = urlsplit(body.decode())
            auth = (
                "Basic "
                + base64.b64encode(
                    f"{access.username}:{access.password}".encode()
                ).decode()
            )
            node(
                a,
                f"fetch('http://flask_bridge:8080/simplefin/accounts', {{headers: {{Authorization: {json.dumps(auth)}}}}}).then(async r => {{ const b = await r.json(); if(r.status !== 200 || !Array.isArray(b.accounts)) process.exit(1); }})",
            )
            # Bodies and generic Docker APIs cannot pass through the socket boundary.
            reject = f"import urllib.request,urllib.error; r=urllib.request.Request('http://docker_gateway:8091/containers/{tenants[0]}/ensure',data=b'{{\"HostConfig\":{{\"Privileged\":true}}}}');\ntry: urllib.request.urlopen(r); raise SystemExit(1)\nexcept urllib.error.HTTPError as e: assert e.code == 400"
            run("docker", "exec", ids["provisioner"], "python", "-c", reject)
            reject_api = "import urllib.request,urllib.error; r=urllib.request.Request('http://docker_gateway:8091/containers/create',method='POST');\ntry: urllib.request.urlopen(r); raise SystemExit(1)\nexcept urllib.error.HTTPError as e: assert e.code == 404"
            run("docker", "exec", ids["provisioner"], "python", "-c", reject_api)
            node(a, "require('fs').writeFileSync('/data/audit-marker', 'preserved')")
            first_id = inspect(a)["Id"]
            print("SimpleFIN claim/sync and gateway policy checks passed", flush=True)
            # Crash recovery: existing tenant networks and data survive an ungraceful gateway restart.
            run("docker", "kill", "--signal", "KILL", ids["docker_gateway"])
            run(*compose, "up", "-d", "docker_gateway")
            wait_for(
                lambda: (
                    request(
                        bridge_url + "/auth/route",
                        headers={"Cf-Access-Jwt-Assertion": token("alice@example.com")},
                    )[0]
                    == 204
                )
            )
            assert inspect(a)["Id"] == first_id
            print("Gateway crash recovery retained the existing tenant", flush=True)
            wait_for(
                lambda: (
                    not run("docker", "ps", "-aq", "--filter", f"name=^/{a}$")
                    and not run("docker", "ps", "-aq", "--filter", f"name=^/{b}$")
                ),
                timeout=50,
            )
            for name in tenants:
                # Container removal precedes endpoint detachment/network deletion.
                wait_for(
                    lambda name=name: (
                        not run(
                            "docker",
                            "network",
                            "ls",
                            "-q",
                            "--filter",
                            f"name=^{project}_actual_net_{name}$",
                        )
                    ),
                    timeout=15,
                )
            print("Idle reaper removed tenant containers and networks", flush=True)
            wait_for(
                lambda: (
                    request(
                        caddy_url + "/",
                        headers={"Cf-Access-Jwt-Assertion": token("alice@example.com")},
                    )[0]
                    == 200
                )
            )
            assert inspect(a)["Id"] != first_id
            assert (
                node(
                    a,
                    "process.stdout.write(require('fs').readFileSync('/data/audit-marker', 'utf8'))",
                )
                == "preserved"
            )
            print(
                "Recreation preserved user data. All Docker integration checks passed.",
                flush=True,
            )
        except Exception:
            print(run(*compose, "logs", "--tail", "50", check=False), flush=True)
            raise
        finally:
            # Only this unique project's resources and temporary data are disposable.
            run(*compose, "down", "--volumes", timeout=120, check=False)
            # Also recover test-only resources if the gateway crashed before graceful shutdown.
            for name in tenants:
                container = "actual_" + name
                raw = run("docker", "inspect", container, check=False)
                if raw and raw != "[]":
                    info = json.loads(raw)[0]
                    if (
                        info["Config"]["Labels"].get("actual-plaid-server.project")
                        == project
                    ):
                        run("docker", "rm", "-f", container)
                network = f"{project}_actual_net_{name}"
                run("docker", "network", "rm", network, check=False)


if __name__ == "__main__":
    main()
