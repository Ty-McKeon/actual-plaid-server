"""Tests for scripts/actual-users.py against a temporary copy of the project."""

import contextlib
import importlib.util
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "scripts" / "actual-users.py"


class ActualUsersTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        shutil.copy(PROJECT / "compose.yaml", self.root)
        self.override = self.root / "compose.override.yaml"

        # The script works out its paths when it is loaded
        environ = {"ACTUAL_USERS_ROOT": str(self.root)}
        with patch.dict(os.environ, environ):
            os.environ.pop("COMPOSE_PROFILES", None)
            spec = importlib.util.spec_from_file_location("actual_users", SCRIPT)
            self.script = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.script)

        patcher = patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("COMPOSE_PROFILES", None)

    def run_script(self, *argv):
        """Runs a command and returns what it printed, or the error it exited with."""
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            try:
                self.script.main(list(argv))
            except SystemExit as err:
                return str(err)
        return output.getvalue()

    def test_defines_a_container_and_private_network_for_each_user(self):
        self.run_script("add", "alice", "Alice@Example.com")
        self.run_script("add", "alice", "alice.alt@example.com")
        output = self.run_script("add", "bob", "bob@example.com")
        self.assertIn("docker compose up -d --remove-orphans", output)

        self.assertEqual(
            self.script.load_users(),
            {
                "alice@example.com": "alice",
                "alice.alt@example.com": "alice",
                "bob@example.com": "bob",
            },
        )
        override = self.override.read_text()
        self.assertEqual(override.count("container_name:"), 2)
        for name in ("alice", "bob"):
            self.assertIn(f"- ./actual-data/users/{name}:/data", override)
            # Its own network, shared only with Caddy and the bridge
            self.assertEqual(override.count(f"- actual_static_{name}\n"), 3)
            self.assertIn(f"\n  actual_static_{name}:\n", override)
        self.assertNotIn("budget_net", override)

    def test_containers_get_the_pinned_image_and_the_same_limits_as_the_gateway(self):
        self.run_script("add", "alice", "alice@example.com")
        override = self.override.read_text()

        self.assertRegex(
            override,
            r"image: \$\{ACTUAL_IMAGE:-actualbudget/actual-server@sha256:[a-f0-9]{64}\}",
        )
        for setting in (
            "mem_limit: ${ACTUAL_MEMORY_LIMIT_MB:-512}m",
            "cpus: ${ACTUAL_CPU_LIMIT:-1}",
            "pids_limit: ${ACTUAL_PIDS_LIMIT:-256}",
            "cap_drop: [NET_RAW]",
            "security_opt: [no-new-privileges:true]",
        ):
            self.assertIn(setting, override)
        self.assertNotIn("ports:", override)
        self.assertNotIn("docker.sock", override)

    def test_removing_a_user_removes_their_container(self):
        self.run_script("add", "alice", "alice@example.com")
        self.run_script("add", "bob", "bob@example.com")
        self.run_script("remove", "alice")

        override = self.override.read_text()
        self.assertNotIn("alice", override)
        self.assertIn("actual_bob", override)

        self.run_script("remove", "bob")
        self.assertIn("services: {}", self.override.read_text())
        self.assertIn("no user named bob", self.run_script("remove", "bob"))

    def test_rejects_bad_names_and_conflicting_assignments(self):
        for name in ("Alice", "a_b", "../x", "a" * 32, ""):
            with self.subTest(name=name):
                self.assertIn("NAME must be", self.run_script("add", name, "a@b.c"))
        self.assertIn("EMAIL", self.run_script("add", "alice", "not-an-email"))

        self.run_script("add", "alice", "alice@example.com")
        self.assertIn(
            "already assigned to alice",
            self.run_script("add", "bob", "alice@example.com"),
        )
        self.assertNotIn("actual_bob", self.override.read_text())

    def test_enabling_the_provisioner_removes_the_static_containers(self):
        self.run_script("add", "alice", "alice@example.com")
        self.assertTrue(self.override.exists())

        for line in (
            "COMPOSE_PROFILES=provisioner",
            'COMPOSE_PROFILES="a,provisioner"',
            "COMPOSE_PROFILES=provisioner # optional mode",
            'COMPOSE_PROFILES="a,provisioner" # optional mode',
        ):
            with self.subTest(line=line):
                (self.root / ".env").write_text(f"OTHER=1\n{line}\n")
                self.assertIn("was removed", self.run_script("sync"))
                self.assertFalse(self.override.exists())

                # Names are still recorded, but no containers are defined
                self.run_script("add", "bob", "bob@example.com")
                self.assertFalse(self.override.exists())
                self.assertIn("Provisioner enabled", self.run_script("list"))

                (self.root / ".env").write_text("OTHER=1\n")
                self.run_script("sync")
                self.assertIn("actual_bob", self.override.read_text())

    def test_the_environment_overrides_the_env_file(self):
        (self.root / ".env").write_text("COMPOSE_PROFILES=provisioner\n")
        os.environ["COMPOSE_PROFILES"] = ""
        self.run_script("add", "alice", "alice@example.com")
        self.assertTrue(self.override.exists())

    def test_never_overwrites_an_override_it_did_not_generate(self):
        self.override.write_text("services: {}\n")
        self.assertIn("was not generated", self.run_script("add", "a", "a@b.c"))
        self.assertEqual(self.override.read_text(), "services: {}\n")
        self.assertFalse(self.script.USERS_FILE.exists())

        (self.root / ".env").write_text("COMPOSE_PROFILES=provisioner\n")
        self.assertIn("was not generated", self.run_script("sync"))
        self.assertTrue(self.override.exists())

    def test_failed_sync_preserves_existing_assignments(self):
        self.run_script("add", "alice", "alice@example.com")
        before = self.script.USERS_FILE.read_text()
        self.override.write_text("services: {}\n")
        for command in (("add", "bob", "bob@example.com"), ("remove", "alice")):
            with self.subTest(command=command):
                self.assertIn("was not generated", self.run_script(*command))
                self.assertEqual(self.script.USERS_FILE.read_text(), before)

    def test_failed_generation_does_not_publish_assignment(self):
        self.script.COMPOSE_FILE.write_text("services: {}\n")
        self.assertIn("could not find", self.run_script("add", "alice", "a@b.c"))
        self.assertFalse(self.script.USERS_FILE.exists())
        self.assertFalse(self.override.exists())

    def test_failed_override_write_does_not_publish_assignment(self):
        original = self.script.write_atomically

        def write(path, content):
            if path == self.override:
                raise OSError("disk full")
            original(path, content)

        with (
            patch.object(self.script, "write_atomically", side_effect=write),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            self.run_script("add", "alice", "a@b.c")
        self.assertFalse(self.script.USERS_FILE.exists())


if __name__ == "__main__":
    unittest.main()
