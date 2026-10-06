"""Tests for the provisioner against an in-memory stand-in for the Docker API."""

import copy
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "provisioner"))

from provisioner import (
    MANAGED_LABEL,
    NAME_LABEL,
    OWNER_LABEL,
    Docker,
    DockerError,
    LimitReached,
    Provisioner,
    make_handler,
)


class FakeDocker:
    """Just enough of the Docker client to observe what the provisioner does."""

    def __init__(self):
        self.containers = {}
        self.images = {"actual:test"}
        self.calls = []
        self.networks = {}
        self.network_calls = []
        self.services = {
            service: {
                "Id": service,
                "Config": {
                    "Labels": {
                        "com.docker.compose.project": "test-project",
                        "com.docker.compose.service": service,
                    }
                },
            }
            for service in ("caddy", "flask_bridge")
        }

    def add(self, name, labels=None, running=True):
        self.containers[name] = {
            "Name": "/" + name,
            "Config": {"Labels": labels or {}},
            "State": {"Running": running},
        }

    def inspect_container(self, name):
        return self.containers.get(name) or self.services.get(name)

    def list_containers(self, label):
        key, value = label.split("=")
        return [
            {
                "Labels": info["Config"]["Labels"],
                "State": "running" if info["State"]["Running"] else "exited",
            }
            for info in self.containers.values()
            if info["Config"]["Labels"].get(key) == value
        ]

    def image_exists(self, image):
        return image in self.images

    def pull_image(self, image):
        self.calls.append(("pull", image))
        self.images.add(image)

    def create_container(self, name, config):
        self.calls.append(("create", name, config))
        self.add(name, config["Labels"], running=False)
        self.containers[name]["Config"].update(copy.deepcopy(config))
        self.containers[name]["HostConfig"] = copy.deepcopy(config["HostConfig"])
        networks = copy.deepcopy(config["NetworkingConfig"]["EndpointsConfig"])
        self.containers[name]["NetworkSettings"] = {"Networks": networks}
        for network in networks:
            self.networks[network]["Containers"][name] = {}

    def start_container(self, name):
        self.calls.append(("start", name))
        self.containers[name]["State"]["Running"] = True

    def remove_container(self, name):
        self.calls.append(("remove", name))
        if self.containers.pop(name, None) is None:
            raise DockerError(404, "no such container")
        for network in self.networks.values():
            network["Containers"].pop(name, None)

    def inspect_network(self, name):
        return self.networks.get(name)

    def list_networks(self, labels):
        filters = dict(label.split("=", 1) for label in labels)
        return [
            copy.deepcopy(n)
            for n in self.networks.values()
            if all(n["Labels"].get(k) == v for k, v in filters.items())
        ]

    def create_network(self, name, labels):
        self.network_calls.append(("create", name))
        self.networks[name] = {
            "Name": name,
            "Driver": "bridge",
            "Internal": False,
            "Labels": dict(labels),
            "Containers": {},
        }

    def service_container(self, project, service):
        return self.services[service]["Id"]

    def connect_network(self, network, container, alias):
        self.network_calls.append(("connect", network, container, alias))
        self.networks[network]["Containers"][container] = {"Aliases": [alias]}

    def disconnect_network(self, network, container):
        self.network_calls.append(("disconnect", network, container))
        self.networks[network]["Containers"].pop(container, None)

    def remove_network(self, network):
        self.network_calls.append(("remove", network))
        if self.networks[network]["Containers"]:
            raise DockerError(409, "active endpoints")
        del self.networks[network]


class ProvisionerTests(unittest.TestCase):
    def setUp(self):
        self.docker = FakeDocker()
        self.provisioner = Provisioner(
            self.docker,
            image="actual:test",
            network="project_actual_net",
            data_root="/srv/actual-data/users",
            idle_seconds=600,
            max_containers=2,
        )

    def test_creates_a_container_from_the_fixed_template(self):
        self.assertTrue(self.provisioner.ensure("alice"))

        kind, name, config = self.docker.calls[0]
        self.assertEqual((kind, name), ("create", "actual_alice"))
        self.assertEqual(
            config,
            {
                "Image": "actual:test",
                "Labels": {
                    MANAGED_LABEL: "true",
                    NAME_LABEL: "alice",
                    OWNER_LABEL: "test-project",
                },
                "HostConfig": {
                    "Binds": ["/srv/actual-data/users/alice:/data"],
                    "NetworkMode": "project_actual_net_alice",
                    "Memory": 512 * 1024 * 1024,
                    "NanoCpus": 1_000_000_000,
                    "PidsLimit": 256,
                    "CapDrop": ["NET_RAW"],
                    "SecurityOpt": ["no-new-privileges:true"],
                },
                "NetworkingConfig": {
                    "EndpointsConfig": {
                        "project_actual_net_alice": {"Aliases": ["actual_alice"]}
                    }
                },
            },
        )
        self.assertEqual(self.docker.calls[1], ("start", "actual_alice"))

    def test_rejects_names_that_could_escape_the_template(self):
        for name in ["", "../etc", "a/b", "a:b", "Alice", "-a", "a" * 32, "a b", "a_b"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.provisioner.ensure(name)
        self.assertEqual(self.docker.calls, [])

    def test_leaves_a_running_container_alone_and_restarts_a_stopped_one(self):
        self.provisioner.ensure("alice")
        self.docker.calls.clear()

        self.assertFalse(self.provisioner.ensure("alice"))
        self.assertEqual(self.docker.calls, [])

        self.docker.containers["actual_alice"]["State"]["Running"] = False
        self.assertFalse(self.provisioner.ensure("alice"))
        self.assertEqual(self.docker.calls, [("start", "actual_alice")])

    def test_pulls_the_image_only_when_it_is_missing(self):
        self.docker.images.clear()
        self.provisioner.ensure("alice")
        self.provisioner.ensure("bob")
        pulls = [call for call in self.docker.calls if call[0] == "pull"]
        self.assertEqual(pulls, [("pull", "actual:test")])

    def test_refuses_to_exceed_the_container_limit(self):
        self.provisioner.ensure("alice")
        self.provisioner.ensure("bob")
        with self.assertRaises(LimitReached):
            self.provisioner.ensure("carol")
        self.assertNotIn("actual_carol", self.docker.containers)

        # Existing users are unaffected by the limit
        self.assertFalse(self.provisioner.ensure("alice"))

    def test_concurrent_creation_respects_capacity(self):
        self.provisioner.max_containers = 1
        start = threading.Barrier(2)
        outcomes = []

        def ensure(name):
            start.wait(timeout=5)
            try:
                self.provisioner.ensure(name)
                outcomes.append("created")
            except LimitReached:
                outcomes.append("limited")

        threads = [threading.Thread(target=ensure, args=(n,)) for n in ("alice", "bob")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(outcomes, ["created", "limited"])
        self.assertEqual(len(self.docker.containers), 1)

    def test_reap_rechecks_activity_after_its_snapshot(self):
        self.provisioner.ensure("alice")
        self.provisioner.last_seen["alice"] -= 601
        remove = self.provisioner.remove

        def refreshed_remove(name, **kwargs):
            self.provisioner.ensure(name)
            remove(name, **kwargs)

        with patch.object(self.provisioner, "remove", side_effect=refreshed_remove):
            self.provisioner.reap()
        self.assertIn("actual_alice", self.docker.containers)

    def test_remove_checks_current_ownership(self):
        self.docker.add("actual_alice")
        with self.assertRaises(DockerError):
            self.provisioner.remove("alice")
        self.assertIn("actual_alice", self.docker.containers)

    def test_image_pull_detects_errors_inside_http_200(self):
        response = Mock(status=200)
        response.__iter__ = Mock(return_value=iter([b'{"error":"pull denied"}\n']))
        connection = Mock()
        connection.getresponse.return_value = response
        with (
            patch("provisioner.UnixHTTPConnection", return_value=connection),
            self.assertRaises(DockerError),
        ):
            Docker().pull_image("actual:test")
        connection.close.assert_called_once()

    def test_tenants_use_separate_networks_with_only_trusted_peers(self):
        self.provisioner.ensure("alice")
        self.provisioner.ensure("bob")
        for user, sibling in (("alice", "bob"), ("bob", "alice")):
            network = self.docker.networks[f"project_actual_net_{user}"]
            self.assertEqual(
                set(network["Containers"]), {"caddy", "flask_bridge", f"actual_{user}"}
            )
            self.assertNotIn(f"actual_{sibling}", network["Containers"])
        self.provisioner.remove_all()
        self.assertEqual(self.docker.networks, {})

    def test_recovers_orphan_network_after_partial_creation(self):
        with (
            patch.object(
                self.docker, "create_container", side_effect=DockerError(500, "failed")
            ),
            self.assertRaises(DockerError),
        ):
            self.provisioner.ensure("alice")
        self.assertEqual(self.docker.networks, {})
        self.provisioner._ensure_network("alice")
        self.provisioner.reap()
        self.assertEqual(self.docker.networks, {})

    def test_reconnects_replaced_bridge_after_restart(self):
        self.provisioner.ensure("alice")
        self.docker.networks["project_actual_net_alice"]["Containers"].pop(
            "flask_bridge"
        )
        self.assertFalse(self.provisioner.ensure("alice"))
        self.assertIn(
            "flask_bridge",
            self.docker.networks["project_actual_net_alice"]["Containers"],
        )

    def test_refuses_network_name_collision(self):
        self.docker.create_network(
            "project_actual_net_alice", {OWNER_LABEL: "another-project"}
        )
        with self.assertRaises(DockerError):
            self.provisioner.ensure("alice")
        self.assertNotIn("actual_alice", self.docker.containers)
        self.assertIn("project_actual_net_alice", self.docker.networks)

    def test_never_disconnects_foreign_endpoints(self):
        self.provisioner._ensure_network("alice")
        self.docker.add("foreign")
        self.docker.networks["project_actual_net_alice"]["Containers"]["foreign"] = {}
        with self.assertRaises(DockerError):
            self.provisioner.ensure("alice")
        with self.assertRaises(DockerError):
            self.provisioner.remove("alice")
        self.assertIn(
            "foreign", self.docker.networks["project_actual_net_alice"]["Containers"]
        )

    def test_legacy_container_is_recreated_with_same_data_bind(self):
        self.docker.add("actual_alice", {MANAGED_LABEL: "true", NAME_LABEL: "alice"})
        bind = "/srv/actual-data/users/alice:/data"
        self.docker.containers["actual_alice"]["HostConfig"] = {
            "Binds": [bind],
            "NetworkMode": "shared",
        }
        self.assertTrue(self.provisioner.ensure("alice"))
        info = self.docker.containers["actual_alice"]
        self.assertEqual(info["HostConfig"]["Binds"], [bind])
        self.assertEqual(
            set(info["NetworkSettings"]["Networks"]), {"project_actual_net_alice"}
        )

    def test_legacy_container_from_another_data_root_is_rejected(self):
        self.docker.add("actual_alice", {MANAGED_LABEL: "true", NAME_LABEL: "alice"})
        self.docker.containers["actual_alice"]["HostConfig"] = {
            "Binds": ["/someone-else/alice:/data"]
        }
        with self.assertRaises(DockerError):
            self.provisioner.ensure("alice")
        self.assertEqual(self.docker.calls, [])

    def test_gateway_rejects_docker_api_and_payloads(self):
        payloads = [
            {"Image": "malicious", "HostConfig": {"Privileged": True}},
            {"HostConfig": {"Binds": ["/:/host"], "NetworkMode": "host"}},
            {"HostConfig": {"Devices": [{"PathOnHost": "/dev/sda"}]}},
        ]
        for payload in payloads:
            self.assertEqual(
                self.call(
                    "POST", "/containers/alice/ensure", json.dumps(payload).encode()
                )[0],
                400,
            )
        for path in (
            "/containers/create",
            "/networks/create",
            "/images/create",
            "/containers/alice/remove",
        ):
            self.assertEqual(self.call("POST", path)[0], 404)
        self.assertEqual(self.docker.calls, [])

    def test_resource_limits_are_configurable(self):
        provisioner = Provisioner(
            self.docker,
            image="actual:test",
            network="project_actual_net",
            data_root="/srv/actual-data/users",
            idle_seconds=600,
            max_containers=2,
            memory_mb=1024,
            cpus=0.5,
            pids=64,
        )
        provisioner.ensure("alice")

        host_config = self.docker.calls[0][2]["HostConfig"]
        self.assertEqual(host_config["Memory"], 1024 * 1024 * 1024)
        self.assertEqual(host_config["NanoCpus"], 500_000_000)
        self.assertEqual(host_config["PidsLimit"], 64)

        for limits in [
            {"memory_mb": 0},
            {"cpus": -1},
            {"pids": 0},
            {"cpus": float("nan")},
        ]:
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                Provisioner(
                    self.docker, "actual:test", "net", "/data", 600, 2, **limits
                )

    def test_reports_limits_the_host_does_not_enforce(self):
        self.docker.info = lambda: {
            "MemoryLimit": False,
            "CpuCfsQuota": True,
            "PidsLimit": False,
        }
        self.assertEqual(
            self.provisioner.unenforced_limits(), ["memory", "process count"]
        )

        self.docker.info = lambda: {"MemoryLimit": True, "CpuCfsQuota": True}
        self.assertEqual(self.provisioner.unenforced_limits(), [])

    def test_never_manages_containers_it_did_not_create(self):
        self.docker.add("actual_server", running=False)

        with self.assertRaises(DockerError):
            self.provisioner.ensure("server")
        self.provisioner.reap()
        self.provisioner.remove_all()

        self.assertEqual(self.docker.calls, [])
        self.assertIn("actual_server", self.docker.containers)

    def test_reaps_idle_and_stopped_containers_only(self):
        for name in ["active", "idle"]:
            self.provisioner.ensure(name)
        self.provisioner.max_containers = 3
        self.provisioner.ensure("crashed")
        self.provisioner.last_seen["idle"] -= 601
        self.docker.containers["actual_crashed"]["State"]["Running"] = False

        self.provisioner.reap()

        self.assertEqual(list(self.docker.containers), ["actual_active"])
        self.assertNotIn("idle", self.provisioner.last_seen)

    def test_adopts_containers_left_by_a_previous_run(self):
        labels = {
            MANAGED_LABEL: "true",
            NAME_LABEL: "alice",
            OWNER_LABEL: "test-project",
        }
        self.docker.add("actual_alice", labels)

        # Counted as seen when this run started, so not reaped straight away
        self.provisioner.reap()
        self.assertIn("actual_alice", self.docker.containers)

        self.provisioner.started_at -= 601
        self.provisioner.reap()
        self.assertNotIn("actual_alice", self.docker.containers)

    def test_remove_all_clears_every_managed_container(self):
        self.provisioner.ensure("alice")
        self.provisioner.ensure("bob")
        self.provisioner.remove_all()
        self.assertEqual(self.docker.containers, {})

    def call(self, method, path, data=None):
        """Sends a request to the HTTP API and returns the status and JSON body."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.provisioner))
        self.addCleanup(server.server_close)
        threading.Thread(target=server.handle_request).start()

        url = f"http://127.0.0.1:{server.server_port}{path}"
        request = urllib.request.Request(url, method=method, data=data)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as err:
            return err.code, json.load(err)

    def test_http_api_reports_the_outcome_of_each_request(self):
        self.assertEqual(self.call("GET", "/health"), (200, {"status": "ok"}))
        self.assertEqual(
            self.call("POST", "/containers/alice/ensure"),
            (200, {"status": "running", "created": True}),
        )
        self.assertEqual(
            self.call("POST", "/containers/alice/ensure"),
            (200, {"status": "running", "created": False}),
        )
        self.assertEqual(self.call("POST", "/containers/Bad_Name/ensure")[0], 400)
        self.assertEqual(self.call("POST", "/containers/a/b/ensure")[0], 404)
        self.assertEqual(self.call("POST", "/containers/alice/remove")[0], 404)

        self.provisioner.ensure("bob")
        self.assertEqual(self.call("POST", "/containers/carol/ensure")[0], 429)

        self.docker.start_container = None  # any Docker failure
        self.docker.containers["actual_bob"]["State"]["Running"] = False
        self.assertEqual(self.call("POST", "/containers/bob/ensure")[0], 502)


if __name__ == "__main__":
    unittest.main()
