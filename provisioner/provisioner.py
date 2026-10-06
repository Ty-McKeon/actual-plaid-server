"""Starts each user's Actual Budget container on demand and stops it when idle.

The unprivileged provisioner relays to a private policy gateway. Only that gateway
holds the Docker socket and owns the lifecycle below. It accepts exactly one
operation on request: make sure the container `actual_<name>` exists and is running,
built from a fixed template in which the name is the only variable. The bridge
calls it for every user it routes; nothing here trusts or parses user input beyond
that name.

Containers are disposable. Each user's data lives in `actual-data/users/<name>` on
the host, so idle containers are removed and recreated on the next request.

HTTP API (internal network only):
  POST /containers/<name>/ensure   create and start the user's container if needed
  GET  /health                     liveness check
"""

import http.client
import json
import logging
import math
import os
import re
import signal
import socket
import threading
import time
import weakref
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlencode, urlsplit

logger = logging.getLogger("provisioner")

# Must match NAME_PATTERN in bridge/services/routing_service.py
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
ROUTE_PATTERN = re.compile(r"/containers/([^/]+)/ensure")
CONTAINER_PREFIX = "actual_"

# Marks the containers this service created; it never touches any other
MANAGED_LABEL = "actual-plaid-server.managed"
NAME_LABEL = "actual-plaid-server.user"
OWNER_LABEL = "actual-plaid-server.project"
DEFAULT_IMAGE = "actualbudget/actual-server@sha256:24645e971da6bb1a1f953d6859e307a0b29be1e8b4130a35b3220c209b5b860c"

# Where compose mounts the host's per-user data folder into this container
DATA_MOUNT = "/actual-data"
STOP_TIMEOUT_SECONDS = 10


class DockerError(Exception):
    """The Docker Engine API answered with an error."""

    def __init__(self, status: int, message: str):
        super().__init__(f"Docker API error {status}: {message}")
        self.status = status


class LimitReached(Exception):
    """Creating another container would exceed the configured maximum."""


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP over the Docker Unix socket."""

    def __init__(self, socket_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class Docker:
    """The handful of Docker Engine API calls this service needs."""

    def __init__(self, socket_path: str = "/var/run/docker.sock"):
        self.socket_path = socket_path

    def request(self, method, path, query=None, body=None, timeout=60):
        """Returns the decoded JSON response, or None when there is no body."""
        if query:
            path = f"{path}?{urlencode(query)}"
        payload = json.dumps(body) if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}

        connection = UnixHTTPConnection(self.socket_path, timeout)
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            if path.startswith("/images/create?") and response.status < 400:
                # Docker reports pull failures inside a successful HTTP stream.
                for line in response:
                    if line.strip():
                        progress = json.loads(line)
                        if progress.get("error") or progress.get("errorDetail"):
                            raise DockerError(502, "Docker image pull failed")
                return None
            raw = response.read()
        finally:
            connection.close()

        if response.status >= 400:
            try:
                message = json.loads(raw).get("message", "")
            except ValueError:
                message = raw.decode(errors="replace")[:200]
            raise DockerError(response.status, message)

        try:
            return json.loads(raw) if raw else None
        except ValueError:
            # Streaming endpoints (image pulls) return one JSON document per line
            return None

    def info(self):
        return self.request("GET", "/info")

    def inspect_container(self, name):
        try:
            return self.request("GET", f"/containers/{quote(name, safe='')}/json")
        except DockerError as err:
            if err.status == 404:
                return None
            raise

    def list_containers(self, label):
        filters = json.dumps({"label": [label]})
        return self.request("GET", "/containers/json", {"all": 1, "filters": filters})

    def list_networks(self, labels):
        return self.request(
            "GET", "/networks", {"filters": json.dumps({"label": labels})}
        )

    def inspect_network(self, name):
        try:
            return self.request("GET", f"/networks/{quote(name, safe='')}")
        except DockerError as err:
            if err.status == 404:
                return None
            raise

    def create_network(self, name, labels):
        return self.request(
            "POST",
            "/networks/create",
            body={
                "Name": name,
                "Driver": "bridge",
                "Internal": False,
                "Labels": labels,
            },
        )

    def connect_network(self, network, container, alias):
        return self.request(
            "POST",
            f"/networks/{quote(network, safe='')}/connect",
            body={
                "Container": container,
                "EndpointConfig": {"Aliases": [alias]},
            },
        )

    def disconnect_network(self, network, container):
        return self.request(
            "POST",
            f"/networks/{quote(network, safe='')}/disconnect",
            body={
                "Container": container,
                "Force": True,
            },
        )

    def remove_network(self, network):
        return self.request("DELETE", f"/networks/{quote(network, safe='')}")

    def service_container(self, project, service):
        filters = json.dumps(
            {
                "label": [
                    f"com.docker.compose.project={project}",
                    f"com.docker.compose.service={service}",
                ]
            }
        )
        matches = self.request(
            "GET", "/containers/json", {"all": 1, "filters": filters}
        )
        if len(matches) != 1 or matches[0]["State"] != "running":
            raise DockerError(503, f"Expected one running {service} service")
        return matches[0]["Id"]

    def image_exists(self, image):
        try:
            self.request("GET", f"/images/{quote(image, safe='/:@')}/json")
            return True
        except DockerError as err:
            if err.status == 404:
                return False
            raise

    def pull_image(self, image):
        # Without a tag or digest the API would pull every tag of the repository
        if "@" not in image and ":" not in image.rsplit("/", 1)[-1]:
            image = f"{image}:latest"
        self.request("POST", "/images/create", {"fromImage": image}, timeout=900)

    def create_container(self, name, config):
        return self.request("POST", "/containers/create", {"name": name}, config)

    def start_container(self, name):
        self.request("POST", f"/containers/{quote(name, safe='')}/start")

    def remove_container(self, name):
        """Stops and deletes a container; a container that is already gone is fine."""
        try:
            self.request(
                "POST",
                f"/containers/{quote(name, safe='')}/stop",
                {"t": STOP_TIMEOUT_SECONDS},
            )
            self.request("DELETE", f"/containers/{quote(name, safe='')}", {"force": 1})
        except DockerError as err:
            if err.status != 404:
                raise


class Provisioner:
    """Creates, tracks and reaps the per-user Actual Budget containers."""

    def __init__(
        self,
        docker,
        image,
        network,
        data_root,
        idle_seconds,
        max_containers,
        memory_mb=512,
        cpus=1.0,
        pids=256,
        project="test-project",
    ):
        self.docker = docker
        self.image = image
        self.network = network
        self.project = project
        self.data_root = data_root
        self.idle_seconds = idle_seconds
        self.max_containers = max_containers
        # Resource limits applied to every user container
        self.memory_mb = memory_mb
        self.cpus = cpus
        self.pids = pids

        settings = (idle_seconds, max_containers, memory_mb, cpus, pids)
        if not all(math.isfinite(value) and value > 0 for value in settings):
            raise ValueError(
                "Idle timeout, container limit and resource limits must be "
                "positive and finite."
            )

        self.started_at = time.monotonic()
        self.last_seen: dict[str, float] = {}
        # Unknown names cannot accumulate locks indefinitely. Active callers keep
        # strong references; the guard still gives concurrent callers the same lock.
        self._locks = weakref.WeakValueDictionary()
        self._locks_guard = threading.Lock()
        # Capacity checks and creation must be one operation across all users.
        self._capacity_lock = threading.Lock()

    @classmethod
    def from_environment(cls, docker):
        """Works out the network and host data folder from this container's own setup."""
        me = docker.inspect_container(socket.gethostname())
        if me is None:
            raise RuntimeError("Could not inspect this container through the socket.")

        data_root = next(
            (m["Source"] for m in me["Mounts"] if m["Destination"] == DATA_MOUNT), None
        )
        if not data_root:
            raise RuntimeError(f"Mount the per-user data folder at {DATA_MOUNT}.")

        project = me["Config"]["Labels"].get("com.docker.compose.project", "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project):
            raise RuntimeError("The gateway must belong to a Docker Compose project.")
        image = os.getenv("ACTUAL_IMAGE") or DEFAULT_IMAGE
        if not re.fullmatch(r"actualbudget/actual-server@sha256:[a-f0-9]{64}", image):
            raise RuntimeError("ACTUAL_IMAGE must pin an official Actual image digest.")

        return cls(
            docker,
            image=image,
            network=f"{project}_actual_net",
            project=project,
            data_root=data_root,
            idle_seconds=float(os.getenv("ACTUAL_IDLE_MINUTES") or 30) * 60,
            max_containers=int(os.getenv("ACTUAL_MAX_CONTAINERS") or 10),
            memory_mb=int(os.getenv("ACTUAL_MEMORY_LIMIT_MB") or 512),
            cpus=float(os.getenv("ACTUAL_CPU_LIMIT") or 1),
            pids=int(os.getenv("ACTUAL_PIDS_LIMIT") or 256),
        )

    def unenforced_limits(self):
        """Names the resource limits this Docker host accepts but does not enforce.

        Some kernels, notably on older Raspberry Pi OS releases, ship with memory
        accounting switched off. Docker then ignores the limit without failing.
        """
        info = self.docker.info()
        supported = {
            "memory": info.get("MemoryLimit"),
            "CPU": info.get("CpuCfsQuota"),
            "process count": info.get("PidsLimit"),
        }
        return [limit for limit, enforced in supported.items() if enforced is False]

    def _lock(self, name):
        with self._locks_guard:
            return self._locks.setdefault(name, threading.Lock())

    def _labels(self, name):
        return {MANAGED_LABEL: "true", NAME_LABEL: name, OWNER_LABEL: self.project}

    def _owns(self, info, name):
        labels = info.get("Config", {}).get("Labels") or {}
        if labels.get(MANAGED_LABEL) != "true" or labels.get(NAME_LABEL) != name:
            return False
        if labels.get(OWNER_LABEL) == self.project:
            return True
        # Legacy resources lacked a project label. Their exact data bind must match
        # this deployment before they may be removed and recreated, never started.
        return OWNER_LABEL not in labels and info.get("HostConfig", {}).get(
            "Binds"
        ) == [f"{self.data_root}/{name}:/data"]

    def _managed(self):
        containers = self.docker.list_containers(f"{MANAGED_LABEL}=true")
        managed = {}
        for container in containers:
            name = container.get("Labels", {}).get(NAME_LABEL)
            if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
                continue
            info = self.docker.inspect_container(CONTAINER_PREFIX + name)
            if info and self._owns(info, name):
                managed[name] = container
        return managed

    def _networks(self):
        return self.docker.list_networks(
            [
                f"{MANAGED_LABEL}=true",
                f"{OWNER_LABEL}={self.project}",
            ]
        )

    def _network_name(self, name):
        return f"{self.network}_{name}"

    def _inspect_network(self, name):
        network = self.docker.inspect_network(self._network_name(name))
        if network and (
            network.get("Labels") != self._labels(name)
            or network.get("Driver") != "bridge"
            or network.get("Internal")
        ):
            raise DockerError(409, "Tenant network ownership or configuration mismatch")
        return network

    def _ensure_network(self, name):
        network_name = self._network_name(name)
        network = self._inspect_network(name)
        if network is None:
            self.docker.create_network(network_name, self._labels(name))
        else:
            for container in network.get("Containers", {}):
                info = self.docker.inspect_container(container)
                labels = (info or {}).get("Config", {}).get("Labels") or {}
                trusted = labels.get(
                    "com.docker.compose.project"
                ) == self.project and labels.get("com.docker.compose.service") in (
                    "caddy",
                    "flask_bridge",
                )
                tenant = (info or {}).get("Name", "").lstrip(
                    "/"
                ) == CONTAINER_PREFIX + name and self._owns(info, name)
                if not trusted and not tenant:
                    raise DockerError(409, "Unexpected endpoint on tenant network")
        # Resolve by project/service labels each time so replaced proxy/bridge
        # containers are reattached after a Compose update.
        for service in ("caddy", "flask_bridge"):
            container = self.docker.service_container(self.project, service)
            network = self._inspect_network(name)
            if container not in network.get("Containers", {}):
                self.docker.connect_network(network_name, container, service)

    def _cleanup_network(self, name):
        network = self._inspect_network(name)
        if network is None:
            return
        # Never detach arbitrary endpoints, even from an owned network.
        for container in list(network.get("Containers", {})):
            info = self.docker.inspect_container(container)
            labels = (info or {}).get("Config", {}).get("Labels") or {}
            if labels.get("com.docker.compose.project") != self.project or labels.get(
                "com.docker.compose.service"
            ) not in ("caddy", "flask_bridge"):
                raise DockerError(409, "Unexpected endpoint on tenant network")
        for container in list(network.get("Containers", {})):
            self.docker.disconnect_network(self._network_name(name), container)
        self.docker.remove_network(self._network_name(name))

    def _template(self, name):
        """The container definition. `name` is validated and the only variable."""
        container = CONTAINER_PREFIX + name
        return {
            "Image": self.image,
            "Labels": self._labels(name),
            "HostConfig": {
                "Binds": [f"{self.data_root}/{name}:/data"],
                "NetworkMode": self._network_name(name),
                "Memory": self.memory_mb * 1024 * 1024,
                "NanoCpus": int(self.cpus * 1_000_000_000),
                "PidsLimit": self.pids,
                "CapDrop": ["NET_RAW"],
                "SecurityOpt": ["no-new-privileges:true"],
            },
            "NetworkingConfig": {
                "EndpointsConfig": {self._network_name(name): {"Aliases": [container]}}
            },
        }

    def ensure(self, name):
        """Makes sure the user's container is running. Returns True if it was created."""
        if not NAME_PATTERN.fullmatch(name):
            raise ValueError("invalid name")

        container = CONTAINER_PREFIX + name
        with self._lock(name):
            created = False
            info = self.docker.inspect_container(container)

            if info is not None and not self._owns(info, name):
                raise DockerError(409, "Container name belongs to another owner")
            # Recreate legacy/shared-network containers and old configurations. The
            # data bind is preserved; never attach a tenant to both shared and private networks.
            if info is not None and not self._matches_template(info, name):
                self.docker.remove_container(container)
                info = None

            if info is None:
                with self._capacity_lock:
                    if len(self._managed()) >= self.max_containers:
                        raise LimitReached(f"limit of {self.max_containers} reached")
                    if (
                        self._inspect_network(name) is None
                        and len(self._networks()) >= self.max_containers
                    ):
                        raise LimitReached(
                            f"network limit of {self.max_containers} reached"
                        )
                    if not self.docker.image_exists(self.image):
                        logger.info("Pulling %s", self.image)
                        self.docker.pull_image(self.image)
                    try:
                        self._ensure_network(name)
                        self.docker.create_container(container, self._template(name))
                    except Exception:
                        if self.docker.inspect_container(container) is None:
                            try:
                                self._cleanup_network(name)
                            except DockerError:
                                logger.exception(
                                    "Deferred network cleanup for %s", name
                                )
                        raise
                self.docker.start_container(container)
                created = True
                logger.info("Created %s", container)
            else:
                self._ensure_network(name)
                if not info["State"]["Running"]:
                    self.docker.start_container(container)
                    logger.info("Started %s", container)

            self.last_seen[name] = time.monotonic()
            return created

    def _matches_template(self, info, name):
        template = self._template(name)
        if info["Config"].get("Image") != self.image:
            return False
        if info["Config"].get("Labels") != self._labels(name):
            return False
        host = info.get("HostConfig", {})
        if any(host.get(key) != value for key, value in template["HostConfig"].items()):
            return False
        if (
            host.get("Privileged")
            or host.get("Devices")
            or host.get("CapAdd")
            or host.get("PortBindings")
        ):
            return False
        networks = info.get("NetworkSettings", {}).get("Networks", {})
        return set(networks) == {self._network_name(name)}

    def remove(self, name, idle_only=False):
        if not NAME_PATTERN.fullmatch(name):
            raise ValueError("invalid name")
        with self._lock(name):
            info = self.docker.inspect_container(CONTAINER_PREFIX + name)
            if info is not None and not self._owns(info, name):
                raise DockerError(409, "Container ownership mismatch")
            if idle_only and info and info["State"]["Running"]:
                idle = time.monotonic() - self.last_seen.get(name, self.started_at)
                if idle < self.idle_seconds:
                    return False
            if info is not None:
                self.docker.remove_container(CONTAINER_PREFIX + name)
            self._cleanup_network(name)
            self.last_seen.pop(name, None)
            return True

    def reap(self):
        """Removes containers that have stopped or seen no requests for a while."""
        now = time.monotonic()
        for name, summary in self._managed().items():
            idle = now - self.last_seen.get(name, self.started_at)
            if summary["State"] == "running" and idle < self.idle_seconds:
                continue
            try:
                if self.remove(name, idle_only=True):
                    logger.info("Removed idle %s%s", CONTAINER_PREFIX, name)
            except DockerError:
                logger.exception("Could not remove %s%s", CONTAINER_PREFIX, name)

        self.reap_orphan_networks()

    def reap_orphan_networks(self):
        for network in self._networks():
            name = (network.get("Labels") or {}).get(NAME_LABEL)
            if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
                continue
            with self._lock(name):
                if self.docker.inspect_container(CONTAINER_PREFIX + name) is None:
                    try:
                        self._cleanup_network(name)
                    except DockerError:
                        logger.exception("Could not clean orphan network for %s", name)

    def remove_all(self):
        """Removes every managed container, so the network can be torn down."""
        threads = [
            threading.Thread(target=self.remove, args=(name,))
            for name in self._managed()
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.reap_orphan_networks()


def make_handler(provisioner):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def reply(self, code, **body):
            payload = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path == "/health":
                self.reply(200, status="ok")
            else:
                self.reply(404, error="not found")

        def do_POST(self):
            if (
                self.headers.get("Transfer-Encoding")
                or self.headers.get("Content-Length", "0") != "0"
            ):
                self.reply(400, error="request bodies are not accepted")
                self.close_connection = True
                return
            match = ROUTE_PATTERN.fullmatch(self.path)
            if not match:
                self.reply(404, error="not found")
                return

            try:
                created = provisioner.ensure(match.group(1))
            except ValueError:
                self.reply(400, error="invalid name")
            except LimitReached as err:
                self.reply(429, error=str(err))
            except Exception:
                logger.exception("Could not provide a container for %s", self.path)
                self.reply(502, error="container could not be started")
            else:
                self.reply(200, status="running", created=created)

        def log_message(self, format, *args):
            # One line per routed request would drown out the useful messages
            pass

    return Handler


class BoundedHTTPServer(ThreadingHTTPServer):
    """Bound simultaneous requests, including slow image pulls and clients."""

    daemon_threads = False

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n"
                )
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class GatewayClient:
    """A fixed-operation client, never a Docker Engine API proxy."""

    def __init__(self):
        parsed = urlsplit(os.getenv("DOCKER_GATEWAY_URL", "http://docker_gateway:8091"))
        if (
            parsed.scheme != "http"
            or not parsed.hostname
            or parsed.path not in ("", "/")
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("DOCKER_GATEWAY_URL must be an internal HTTP origin")
        self.host, self.port = parsed.hostname, parsed.port or 80

    def ensure(self, name):
        if not NAME_PATTERN.fullmatch(name):
            raise ValueError("invalid name")
        connection = http.client.HTTPConnection(self.host, self.port, timeout=55)
        try:
            connection.request(
                "POST", f"/containers/{name}/ensure", headers={"Content-Length": "0"}
            )
            response = connection.getresponse()
            body = json.loads(response.read())
            if response.status == 429:
                raise LimitReached(body.get("error", "capacity reached"))
            if response.status != 200:
                raise DockerError(response.status, "Gateway could not start tenant")
            return body["created"]
        finally:
            connection.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    server = BoundedHTTPServer(("0.0.0.0", 8090), make_handler(GatewayClient()))

    def stop(signum, frame):
        threading.Thread(target=server.shutdown).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    logger.info("Unprivileged relay listening on :8090")
    try:
        server.serve_forever()
    finally:
        server.server_close()


def gateway_main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    docker = Docker()
    provisioner = Provisioner.from_environment(docker)
    logger.info(
        "Image %s, network %s, data in %s, idle limit %.0fs, at most %d containers",
        provisioner.image,
        provisioner.network,
        provisioner.data_root,
        provisioner.idle_seconds,
        provisioner.max_containers,
    )
    logger.info(
        "Each container is limited to %d MiB of memory, %g CPUs and %d processes",
        provisioner.memory_mb,
        provisioner.cpus,
        provisioner.pids,
    )
    for limit in provisioner.unenforced_limits():
        logger.warning(
            "This host does not enforce the %s limit, so containers are not "
            "restricted by it. Enable the matching cgroup controller in the kernel.",
            limit,
        )

    def reap_forever():
        interval = float(os.getenv("ACTUAL_REAP_INTERVAL_SECONDS") or 60)
        while True:
            time.sleep(interval)
            try:
                provisioner.reap()
            except Exception:
                logger.exception("Reaping idle containers failed")

    def pull_image():
        # Fetch the image ahead of the first request so that one is not slow
        try:
            if not docker.image_exists(provisioner.image):
                logger.info("Pulling %s", provisioner.image)
                docker.pull_image(provisioner.image)
        except Exception:
            logger.exception("Could not pull %s", provisioner.image)

    threading.Thread(target=reap_forever, daemon=True).start()
    threading.Thread(target=pull_image, daemon=True).start()

    server = BoundedHTTPServer(("0.0.0.0", 8091), make_handler(provisioner))

    def stop(signum, frame):
        # shutdown() must not run on the thread that is serving requests
        threading.Thread(target=server.shutdown).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    logger.info("Policy gateway listening on :8091")
    try:
        server.serve_forever()
    finally:
        server.server_close()

    logger.info("Stopping, removing user containers")
    provisioner.remove_all()


if __name__ == "__main__":
    main()
