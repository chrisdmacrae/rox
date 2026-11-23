import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
LOADER = importlib.machinery.SourceFileLoader("rox", str(ROOT / "rox"))
SPEC = importlib.util.spec_from_loader("rox", LOADER)
rox = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rox
LOADER.exec_module(rox)
REAL_DEFAULT_ENGINE = rox.default_engine
REAL_ENSURE_RUNTIME = rox.ensure_runtime
REAL_GUEST_STATE = rox.guest_state
REAL_REACHABLE = rox.reachable
REAL_SUGGEST_APP_HOST = rox.suggest_app_host
REAL_LOCAL_APP = rox.local_app
REAL_RESTORE_GIT_AGENT = rox.restore_git_agent


def base_image(version=None, image_id="sha256:base"):
    """`image inspect` output for a rox:linux image built from the given toolkit version."""
    labels = {"io.rox.toolkit": rox.toolkit_version() if version is None else version}
    return json.dumps([{"Id": image_id, "Config": {"Labels": labels}}])


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        # Keep the launcher's real config, keys and Git cache out of tests.
        state = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state"),
                                        "XDG_CONFIG_HOME": str(self.root / "config-home"),
                                        "XDG_CACHE_HOME": str(self.root / "cache-home"),
                                        "HOME": str(self.root / "home")})
        state.start()
        self.addCleanup(state.stop)
        # Keep prompts out of tests run from a terminal.
        stdin = patch.object(rox.sys, "stdin", io.StringIO())
        stdin.start()
        self.addCleanup(stdin.stop)
        # The default container engine depends on the platform, so pin it for repeatable tests.
        engine = patch.object(rox, "default_engine", return_value="docker")
        engine.start()
        self.addCleanup(engine.stop)
        # Treat guests as running and reachable, so commands don't ask a real engine or tunnel.
        # Tests of those checks call the REAL_ copies.
        for name, value in (("guest_state", "running"), ("reachable", True), ("suggest_app_host", None),
                            ("local_app", None), ("restore_git_agent", False)):
            runtime = patch.object(rox, name, return_value=value)
            runtime.start()
            self.addCleanup(runtime.stop)
        # Launches read .rox.sh and .rox.json from the branch. Tests opt in to a real fetch.
        branch = patch.object(rox, "read_branch", return_value=None)
        branch.start()
        self.addCleanup(branch.stop)

    def make_repo(self):
        repo = self.root / "sample project"
        nested = repo / "subdir"
        if nested.is_dir():
            return nested
        repo.mkdir()
        git = ["git", "-C", str(repo)]
        git_commands = [
            ["init", "--quiet", "--initial-branch=local-feature"],
            ["remote", "add", "origin", "https://git.example.test/main/repo.git"],
            ["remote", "add", "fork", "git@git.example.test:fork/repo.git"],
            ["config", "branch.local-feature.remote", "fork"],
            ["config", "branch.local-feature.merge", "refs/heads/published-feature"],
        ]
        for command in git_commands:
            subprocess.run([*git, *command], check=True)
        nested.mkdir()
        return nested

    def test_nested_checkout_uses_the_current_branch_upstream(self):
        nested = self.make_repo()
        self.assertEqual(
            rox.project_from(nested, None, None, None),
            ("sample project", "git@git.example.test:fork/repo.git", "published-feature"),
        )
        self.assertEqual(
            rox.project_from(nested, "origin", None, None),
            ("sample project", "https://git.example.test/main/repo.git", "local-feature"),
        )
        for url in ("file:///tmp/repo", "ext::echo data", "http://git.example.test/repo",
                    "https://secret@git.example.test/repo"):
            with self.subTest(url=url), self.assertRaises(rox.Error):
                rox.project_from(nested, None, url, None)

    def test_bootstrap_runs_from_the_guest_checkout_without_clone_credentials(self):
        workspace = self.root / "guest project"
        workspace.mkdir()
        hook = workspace / ".rox.sh"
        hook.write_text(
            'set -eu\n'
            'test -z "${SSH_AUTH_SOCK:-}"\n'
            'test "$PWD" = "$ROX_PROJECT_DIR"\n'
            'printf "%s %s %s\\n" "$ROX_NAME" "$ROX_BACKEND" "$REX_SERVER" > started\n'
        )
        socket_path = str(self.root / "rex.sock")
        args = ["bash", str(ROOT / "guest/bootstrap.sh"), "setup", str(workspace),
                "rv-fixture-000011112222", "macos", socket_path]
        env = {**os.environ, "SSH_AUTH_SOCK": "/tmp/test-agent.sock"}
        subprocess.run([*args, "true"], check=True, env=env)
        self.assertFalse((workspace / "started").exists())
        subprocess.run([*args, "false"], check=True, env=env, capture_output=True)
        self.assertEqual(
            (workspace / "started").read_text(),
            f"rv-fixture-000011112222 macos unix://{socket_path}\n",
        )
        hook.write_text("exit 17\n")
        self.assertEqual(subprocess.run([*args, "false"], env=env, capture_output=True).returncode, 17)

    def test_remote_launch_resolves_local_project_and_uses_target_paths(self):
        nested = self.make_repo()
        rex = ["rex", "--autostart=false", "-S", "buildbox"]
        real_run = rox.run
        requests = []

        def command(argv, **kwargs):
            if argv[0] == "git":
                return real_run(argv, **kwargs)
            requests.append(argv)
            if argv[4] == "new":
                return subprocess.CompletedProcess(argv, 0, json.dumps({
                    "session_id": "session:launch",
                    "initial_windows": [{"block_ids": ["block:provision"]}],
                }), "")
            if argv[4] == "wait":
                return subprocess.CompletedProcess(argv, 0)
            return subprocess.CompletedProcess(argv, 0, "Workspace rv-sample-000011112222\n", "")

        captured = io.StringIO()
        with patch.object(rox, "run", side_effect=command), contextlib.redirect_stdout(captured):
            rox.main([
                "up", "container", "--host", "buildbox", "--cwd", str(nested),
                "--engine", "podman", "--image", "custom:latest", "--boot-timeout", "60",
                "--config", "~/Rex Config/rox.json", "--git-key", "~/.ssh/remote key",
                "--git-known-hosts", "~/.ssh/git_hosts", "--no-hook",
                "--", "agent", "'an argument with spaces' $HOME",
            ])
        self.assertEqual(requests, [
            [*rex, "new", "--window", "Provision", "--keep-open", "--json",
             "rox sample project", "--", *rox.REMOTE_ROX, "up", "container",
             "--project=sample project", "--repo=git@git.example.test:fork/repo.git",
             "--branch=published-feature", "--boot-timeout=60",
             "--config=~/Rex Config/rox.json", "--image=custom:latest", "--engine=podman",
             "--git-known-hosts=~/.ssh/git_hosts", "--git-key=~/.ssh/remote key", "--no-hook",
             "--", "agent", "'an argument with spaces' $HOME"],
            [*rex, "wait", "-s", "session:launch", "block:provision"],
            [*rex, "capture", "-s", "session:launch", "-b", "block:provision", "--trim", "--unwrap"],
            # The Provision session closes once it succeeds.
            [*rex, "kill", "session:launch"],
        ])
        self.assertIn("on buildbox in Rex session session:launch", captured.getvalue())
        self.assertIn("Workspace rv-sample-000011112222", captured.getvalue())
        self.assertFalse(rox.state_root().exists())

    def test_explicit_project_can_launch_without_a_checkout(self):
        with patch.object(rox, "dispatch", return_value="") as launch:
            rox.main([
                "up", "linux", "-S", "buildbox", "--cwd", str(self.root / "missing"),
                "--project", "fixture", "--repo", "https://git.example.test/repo.git", "--branch", "main",
            ])
        self.assertEqual(launch.call_args.args[1], "fixture")
        self.assertEqual(launch.call_args.args[2][2:5], [
            "--project=fixture", "--repo=https://git.example.test/repo.git", "--branch=main"])

    def test_empty_remote_host_never_falls_back_to_local_launch(self):
        with patch.object(rox, "resolve_project") as resolve:
            with self.assertRaisesRegex(rox.Error, "--host needs a Rex host label"):
                rox.main(["up", "container", "--host", ""])
        resolve.assert_not_called()

    def test_remote_launch_reports_provision_failure_and_preserves_logs(self):
        real_run = rox.run

        def command(argv, **kwargs):
            if argv[0] == "git":
                return real_run(argv, **kwargs)
            if argv[4] == "new":
                return subprocess.CompletedProcess(argv, 0, json.dumps({
                    "session_id": "session:launch",
                    "initial_windows": [{"block_ids": ["block:provision"]}],
                }), "")
            if argv[4] == "wait":
                return subprocess.CompletedProcess(argv, 127)
            return subprocess.CompletedProcess(argv, 0, "rox: command not found\n", "")

        captured = io.StringIO()
        with patch.object(rox, "run", side_effect=command), contextlib.redirect_stdout(captured):
            with self.assertRaisesRegex(rox.Error, r"status 127.*session:launch on buildbox"):
                rox.main(["up", "linux", "--host", "buildbox", "--cwd", str(self.make_repo())])
        self.assertIn("rox: command not found", captured.getvalue())

    def test_shell_attaches_to_guest_checkout_without_agent_forwarding(self):
        guest = rox.Guest("rv-fixture-000011112222", "container", "image", "workspace",
                          address="127.0.0.1", port=44000, phase="ready",
                          guest_socket="/home/workspace/.rox/rv-fixture-000011112222/r.sock",
                          projects=[rox.Project("fixture", "fixture", "", "main",
                                                workspace="/home/workspace/project with spaces")])
        with patch.object(rox.Guest, "load", return_value=guest), patch.object(rox.os, "execvpe") as exec_run:
            rox.main(["shell", guest.name])
        executable, argv, env = exec_run.call_args.args
        self.assertEqual(executable, "ssh")
        self.assertIn("-t", argv)
        self.assertIn("ForwardAgent=no", argv)
        self.assertEqual(argv[-3:-1], ["--", "127.0.0.1"])
        self.assertEqual(shlex.split(argv[-1])[-1], guest.projects[0].workspace)
        self.assertEqual(env["TERM"], "xterm-256color")

    def test_first_container_launch_builds_the_default_image_before_provisioning(self):
        calls = []
        built = False
        real_run = rox.run

        def command(argv, **kwargs):
            nonlocal built
            if argv[0] == "git":
                return real_run(argv, **kwargs)
            calls.append(argv)
            if argv[1:3] == ["image", "inspect"]:
                if not built:
                    return subprocess.CompletedProcess(argv, 1, "", "image not found")
                return subprocess.CompletedProcess(argv, 0, base_image(), "")
            if argv[1] == "build":
                self.assertFalse(list(rox.state_root().glob("*/guest.json")))
                built = True
            return subprocess.CompletedProcess(argv, 0, "", "")

        cwd = self.make_repo()
        with (
            patch.object(rox, "run", side_effect=command),
            patch.object(rox.shutil, "which", return_value="/fixture"),
            patch.object(rox, "git_host_entries", return_value=""),
            patch.object(rox, "prepare_project_image", return_value=""),
            patch.object(rox, "launch_guest", side_effect=rox.Error("launch fixture")),
            patch.object(rox.Guest, "runtime", property(lambda guest: self.root / "runtime" / guest.name)),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaisesRegex(rox.Error, "launch fixture"):
                rox.main(["up", "container", "--engine", "podman", "--cwd", str(cwd)])
            rox.prepare_container_image("podman", "rox:linux")
            rox.prepare_container_image("podman", "registry.example.test/custom:latest")

        self.assertEqual(calls, [
            ["podman", "info"],
            ["podman", "image", "inspect", "rox:linux"],
            ["podman", "build", "--file", str(ROOT / "Containerfile"), "--tag", "rox:linux", str(ROOT)],
            ["podman", "image", "inspect", "rox:linux"],
        ])

    def test_base_image_is_rebuilt_when_its_toolkit_version_changes(self):
        rox.private_directory(rox.state_root())
        self.assertTrue(rox.toolkit_version())
        for inspect, builds in ((base_image(), 0), (base_image("1"), 1),
                                (json.dumps([{"Id": "sha256:old", "Config": {"Labels": None}}]), 1)):
            calls = []

            def command(argv, **kwargs):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, inspect if argv[1] == "image" else "", "")

            with self.subTest(inspect=inspect), patch.object(rox, "run", side_effect=command), \
                    contextlib.redirect_stdout(io.StringIO()):
                rox.prepare_container_image("container", "rox:linux")
            self.assertEqual(sum(argv[1] == "build" for argv in calls), builds)

    def test_container_sshd_allows_only_local_forwarding(self):
        entrypoint = (ROOT / "guest" / "container-entrypoint.sh").read_text()
        self.assertIn("AllowTcpForwarding=local", entrypoint)
        self.assertNotIn("GatewayPorts", entrypoint)

    def test_forward_tunnels_launcher_localhost_to_guest_localhost(self):
        rox.private_directory(rox.state_root())
        guest = rox.Guest("rv-fixture-000011112222", "container", "rox:linux", "workspace", engine="container",
                          identity=str(self.root / "login"), address="192.168.64.9", phase="ready")
        guest.directory.mkdir(mode=0o700)
        guest.save()
        with (
            patch.object(rox.shutil, "which", return_value="/fixture"),
            patch.object(rox.os, "execvp") as execvp,
            contextlib.redirect_stdout(io.StringIO()) as printed,
        ):
            rox.main(["forward", guest.name, "5173", "8080:3000"])
        argv = execvp.call_args.args[1]
        self.assertEqual(argv[-2:], ["--", "192.168.64.9"])
        tunnels = [argv[index + 1] for index, arg in enumerate(argv) if arg == "-L"]
        self.assertEqual(tunnels, ["127.0.0.1:5173:127.0.0.1:5173", "127.0.0.1:8080:127.0.0.1:3000"])
        self.assertIn("-N", argv)
        self.assertIn("ExitOnForwardFailure=yes", argv)
        self.assertIn("http://localhost:8080 to port 3000", printed.getvalue())

    def test_forward_rejects_invalid_ports_and_guests_that_are_not_ready(self):
        for spec in ("0", "70000", "web", "1:2:3"):
            with self.subTest(spec=spec), self.assertRaises(SystemExit), \
                    contextlib.redirect_stderr(io.StringIO()):
                rox.main(["forward", "rv-fixture-000011112222", spec])
        rox.private_directory(rox.state_root())
        guest = rox.Guest("rv-fixture-000011112222", "linux", "image", "admin", phase="failed")
        guest.directory.mkdir(mode=0o700)
        guest.save()
        with self.assertRaisesRegex(rox.Error, "not ready"):
            rox.main(["forward", guest.name, "3000"])

    def test_default_container_image_run_uses_local_storage(self):
        guest = rox.Guest("rv-fixture-000011112222", "container", "rox:linux", "workspace", engine="docker",
                          identity=str(self.root / "login"))
        (self.root / "login.pub").write_text("ssh-ed25519 AAAA fixture\n")
        with (
            patch.object(rox, "run"),
            patch.object(rox, "output", side_effect=[
                "container-id", "ssh-ed25519 AAAA host", "127.0.0.1:33000",
            ]) as output,
            patch.object(rox, "pin_host"),
            patch.object(rox.Guest, "save"),
        ):
            self.assertEqual(rox.launch_guest(guest, 5), ("127.0.0.1", 33000))
        launch = output.call_args_list[0].args[0]
        self.assertEqual(launch[:4], ["docker", "run", "-d", "--pull=never"])
        self.assertEqual(launch[-1], "rox:linux")
        # The engine brings the container back after it restarts, unless rox stop stopped it.
        self.assertIn("--restart unless-stopped", " ".join(launch))

    def test_clone_reuses_only_the_matching_trusted_host_entries(self):
        ssh_dir = self.root / ".ssh"
        ssh_dir.mkdir()
        (ssh_dir / "known_hosts").write_text(
            "git.example.test ssh-ed25519 AAAA\n"
            "unrelated.example.test ssh-ed25519 AQAA\n"
            "[git.example.test]:2222 ssh-ed25519 AgAA\n"
        )
        with patch.object(rox.Path, "home", return_value=self.root):
            self.assertEqual(rox.git_host_entries("git@git.example.test:repo.git", None),
                             "git.example.test ssh-ed25519 AAAA\n")
            self.assertEqual(rox.git_host_entries("ssh://git@git.example.test:2222/repo.git", None),
                             "[git.example.test]:2222 ssh-ed25519 AgAA\n")
            self.assertEqual(rox.git_host_entries("git@unknown.example.test:repo.git", None), "")

    def test_key_commands_create_config_and_deduplicate_paths(self):
        config_root = self.root / "config"
        config = config_root / "rox" / "config.json"
        key = self.root / "Git SSH key"
        key.write_text("key fixture")
        alias = self.root / "key alias"
        alias.symlink_to(key)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config_root)}), contextlib.redirect_stdout(io.StringIO()):
            rox.main(["key", "remove", str(key)])
            self.assertFalse(config.exists())
            rox.main(["key", "add", str(key)])
            rox.main(["key", "add", str(alias)])
            self.assertEqual(json.loads(config.read_text()), {"git_keys": [str(key)]})
            rox.main(["key", "remove", str(alias)])
            self.assertEqual(json.loads(config.read_text()), {"git_keys": []})
            self.assertEqual(key.read_text(), "key fixture")

    def test_key_commands_preserve_existing_config_and_remove_missing_keys(self):
        config = self.root / "host config.json"
        old_key = self.root / "old key"
        new_key = self.root / "new key"
        settings = {"git_known_hosts": "~/.ssh/known_hosts", "container": {"engine": "podman"}}
        config.write_text(json.dumps({**settings, "git_keys": [str(old_key)]}))
        before = config.read_text()
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(rox.Error, "File does not exist"):
                rox.main(["key", "add", str(new_key), "--config", str(config)])
            self.assertEqual(config.read_text(), before)
            new_key.touch()
            rox.main(["key", "add", str(new_key), "--config", str(config)])
            rox.main(["key", "remove", str(old_key), "--config", str(config)])
        self.assertEqual(json.loads(config.read_text()), {**settings, "git_keys": [str(new_key)]})

    def test_config_command_creates_the_config_and_opens_it_in_the_editor(self):
        config = self.root / "config-home" / "rox" / "config.json"
        editor = self.root / "fake editor"
        editor.write_text("#!/bin/sh\nprintf '%s\\n' \"$1\" > \"$(dirname \"$0\")/edited\"\n")
        editor.chmod(0o755)
        with patch.dict(os.environ, {"VISUAL": "", "EDITOR": f"{shlex.quote(str(editor))} --wait"}):
            rox.main(["config"])
        self.assertEqual(config.read_text(), "{}\n")
        self.assertEqual((self.root / "edited").read_text(), "--wait\n")

    def test_config_command_reports_invalid_json_and_a_missing_editor(self):
        config = self.root / "custom config.json"
        with patch.dict(os.environ, {"VISUAL": "sh -c 'echo \"{nope\" > \"$0\"'"}):
            with self.assertRaisesRegex(rox.Error, "isn't valid JSON"):
                rox.main(["config", "--config", str(config)])
        with patch.dict(os.environ, {"VISUAL": "no-such-editor-for-rox"}):
            with self.assertRaisesRegex(rox.Error, "Could not find the editor no-such-editor-for-rox"):
                rox.main(["config", "--config", str(config)])

    def test_config_tailscale_saves_a_private_key_and_a_valid_tailscale_section(self):
        config = self.root / "config-home" / "rox" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"git_keys": ["~/.ssh/id"]}))
        with patch.object(rox.sys, "stdin", io.StringIO("tskey-auth-first\n")), \
                contextlib.redirect_stdout(io.StringIO()):
            rox.main(["config", "tailscale"])
        key = config.with_name("tailscale-authkey")
        self.assertEqual(key.read_text(), "tskey-auth-first\n")
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        saved = json.loads(config.read_text())
        self.assertEqual(saved, {"git_keys": ["~/.ssh/id"],
                                 "tailscale": {"auth_key_file": str(key), "tags": ["tag:rox-guest"]}})
        self.assertEqual(rox.tailscale_settings(saved, config, False),
                         {"key": key, "tags": ["tag:rox-guest"], "control_url": ""})

        # Running it again replaces the key in the configured file and keeps the control server.
        custom = self.root / "custom key"
        saved["tailscale"].update(auth_key_file=str(custom), control_url="https://hs.test")
        config.write_text(json.dumps(saved))
        with patch.object(rox.sys, "stdin", io.StringIO("tskey-auth-second\n")), \
                contextlib.redirect_stdout(io.StringIO()):
            rox.main(["config", "tailscale", "--tag", "tag:a", "--tag", "tag:b"])
        self.assertEqual(custom.read_text(), "tskey-auth-second\n")
        self.assertEqual(json.loads(config.read_text())["tailscale"],
                         {"auth_key_file": str(custom), "tags": ["tag:a", "tag:b"], "control_url": "https://hs.test"})

    def test_config_tailscale_rejects_bad_input_without_writing(self):
        config = self.root / "alt config.json"
        for argv, stdin, message in (
            (["--tag", "rox-guest"], "tskey-auth-x\n", "tag such as tag:rox-guest"),
            (["--control-url", "http://hs.test"], "tskey-auth-x\n", "https:// URL"),
            ([], "\n", "Expected a Tailscale auth key"),
        ):
            with self.subTest(argv=argv), patch.object(rox.sys, "stdin", io.StringIO(stdin)), \
                    self.assertRaisesRegex(rox.Error, message):
                rox.main(["config", "tailscale", "--config", str(config), *argv])
        self.assertFalse(config.exists())
        self.assertFalse(config.with_name("tailscale-authkey").exists())

    def saved_guest(self, name="rv-fixture-000011112222", **fields):
        rox.private_directory(rox.state_root())
        guest = rox.Guest(name, "container", "rox:linux", "workspace", **{
            "engine": "container", "identity": str(self.root / "login"), "address": "192.168.64.9",
            "guest_socket": "/home/workspace/.rox/x/r.sock", "phase": "ready", "registered": True, **fields})
        guest.directory.mkdir(mode=0o700)
        guest.save()
        return guest

    def test_ensure_runtime_starts_apple_container_services_and_explains_a_stopped_engine(self):
        results = {("container", "system", "status"): 1, ("container", "system", "start"): 0}
        commands = []

        def fake_run(argv, **kwargs):
            commands.append(argv)
            return subprocess.CompletedProcess(argv, results.get(tuple(argv[:3]), 0), "", "")

        with patch.object(rox, "run", side_effect=fake_run), contextlib.redirect_stdout(io.StringIO()):
            REAL_ENSURE_RUNTIME("container")
            self.assertEqual(commands[-1], ["container", "system", "start", "--disable-kernel-install"])
            results[("container", "system", "start")] = 1
            with self.assertRaisesRegex(rox.Error, "Could not start Apple's container services"):
                REAL_ENSURE_RUNTIME("container")
            commands.clear()
            results[("container", "system", "status")] = 0
            REAL_ENSURE_RUNTIME("container")
            self.assertEqual(commands, [["container", "system", "status"]])
            REAL_ENSURE_RUNTIME("")
        with patch.object(rox, "output", side_effect=rox.Error("Cannot connect")):
            with self.assertRaisesRegex(rox.Error, "docker isn't running"):
                REAL_ENSURE_RUNTIME("docker")

    def test_guest_state_reads_apple_container_docker_and_tart(self):
        apple = rox.Guest("rv-fixture-000011112222", "container", "rox:linux", "workspace", engine="container")
        docker = rox.Guest("rv-fixture-000011112222", "container", "rox:linux", "workspace",
                           engine="docker", container_id="abc")
        vm = rox.Guest("rv-fixture-000011112222", "linux", "rox-ubuntu", "admin")
        replies = {}

        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, *replies[argv[0]])

        cases = [
            (apple, "container", (0, json.dumps([{"status": {"state": "running", "networks": []}}]), ""), "running"),
            (apple, "container", (0, json.dumps([{"status": {"state": "stopped", "networks": []}}]), ""), "stopped"),
            (apple, "container", (1, "", "Error: container not found: rv-fixture"), "missing"),
            (docker, "docker", (0, "true\n", ""), "running"),
            (docker, "docker", (0, "false\n", ""), "stopped"),
            (docker, "docker", (1, "", "Error: No such object: abc"), "missing"),
        ]
        with patch.object(rox, "run", side_effect=fake_run):
            for guest, tool, reply, state in cases:
                replies[tool] = reply
                with self.subTest(tool=tool, state=state):
                    self.assertEqual(REAL_GUEST_STATE(guest), state)
            replies["container"] = (1, "", "XPC connection error")
            with self.assertRaisesRegex(rox.Error, "XPC connection error"):
                REAL_GUEST_STATE(apple)
        for listing, state in (([{"Name": vm.name, "Running": False}], "stopped"),
                               ([{"Name": vm.name, "Running": True}], "running"), ([], "missing")):
            with self.subTest(tart=state), patch.object(rox, "output", return_value=json.dumps(listing)):
                self.assertEqual(REAL_GUEST_STATE(vm), state)

    def test_resume_starts_a_stopped_guest_at_its_new_address_and_reconnects(self):
        guest = self.saved_guest()
        with (
            patch.object(rox, "ensure_runtime") as runtime,
            patch.object(rox, "guest_state", return_value="stopped"),
            patch.object(rox, "output", return_value="") as command,
            patch.object(rox, "apple_container_address", return_value=("192.168.64.12", 22)),
            patch.object(rox, "check_guest_os"),
            patch.object(rox, "close_tunnel"),
            patch.object(rox, "connect") as connect,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertTrue(rox.resume(guest, boot_timeout=5))
        runtime.assert_called_once_with("container")
        self.assertEqual(command.call_args.args[0], ["container", "start", guest.name])
        self.assertEqual(rox.Guest.load(guest.name).address, "192.168.64.12")
        connect.assert_called_once_with(guest, None)

    def test_resume_leaves_a_healthy_guest_alone_and_reports_a_deleted_one(self):
        guest = self.saved_guest()
        with patch.object(rox, "ensure_runtime"), patch.object(rox, "connect") as connect:
            self.assertFalse(rox.resume(guest))
            connect.assert_not_called()
            with patch.object(rox, "reachable", return_value=False), patch.object(rox, "close_tunnel"):
                self.assertTrue(rox.resume(guest))
            connect.assert_called_once()
            with patch.object(rox, "guest_state", return_value="missing"), \
                    self.assertRaisesRegex(rox.Error, "no longer exists. Remove the workspace with rox down"):
                rox.resume(guest)

    def test_stop_keeps_a_guest_stopped_until_start(self):
        guest = self.saved_guest()
        with (
            patch.object(rox, "output", return_value="") as command,
            patch.object(rox, "close_tunnel"),
            contextlib.redirect_stdout(io.StringIO()) as printed,
        ):
            rox.main(["stop", guest.name])
            self.assertEqual(command.call_args.args[0], ["container", "stop", guest.name])
            self.assertIn(f"rox start {guest.name}", printed.getvalue())
            self.assertTrue(rox.Guest.load(guest.name).stopped)
            self.assertEqual(rox.local_records()[0]["phase"], "stopped")
            for argv in (["shell", guest.name], ["forward", guest.name, "3000"]):
                with self.subTest(argv=argv), patch.object(rox.shutil, "which", return_value="/fixture"), \
                        self.assertRaisesRegex(rox.Error, "is stopped. Start it with rox start"):
                    rox.main(argv)
            with patch.object(rox, "resume") as resume:
                rox.main(["start", guest.name])
            self.assertEqual(resume.call_args.kwargs, {"force": False})
        reopened = rox.Guest.load(guest.name)
        self.assertFalse(reopened.stopped)
        self.assertEqual(reopened.phase, "ready")

    def test_shell_restores_a_guest_whose_tunnel_went_away(self):
        guest = self.saved_guest()
        with (
            patch.object(rox, "reachable", return_value=False),
            patch.object(rox, "resume") as resume,
            patch.object(rox.os, "execvpe"),
        ):
            rox.main(["shell", guest.name])
        self.assertEqual(resume.call_args.args[0].name, guest.name)

    def test_service_tick_restores_wanted_guests_and_backs_off_failures(self):
        healthy = self.saved_guest("rv-healthy-000011112222")
        broken = self.saved_guest("rv-broken-000011112222")
        self.saved_guest("rv-stopped-000011112222", stopped=True)
        self.saved_guest("rv-failed-000011112222", phase="failed")
        attempts = []

        def fake_resume(guest, tailnet=None):
            attempts.append(guest.name)
            if guest.name == broken.name:
                raise rox.Error("container did not report an IPv4 address")
            return True

        retry = {}
        with patch.object(rox, "resume", side_effect=fake_resume), \
                contextlib.redirect_stdout(io.StringIO()) as log:
            rox.service_tick(retry, 1000)
            self.assertEqual(sorted(attempts), [broken.name, healthy.name])
            self.assertEqual(retry, {broken.name: (1030, 30)})
            self.assertIn(f"Restored {healthy.name}", log.getvalue())
            self.assertIn(f"Could not restore {broken.name}, trying again in 30s", log.getvalue())
            attempts.clear()
            rox.service_tick(retry, 1010)
            self.assertEqual(attempts, [healthy.name])
            rox.service_tick(retry, 1031)
            self.assertEqual(retry[broken.name], (1091, 60))
            # A guest another command is working on is skipped, not counted as a failure.
            with rox.guest_lock(healthy.directory):
                attempts.clear()
                rox.service_tick(retry, 2000)
            self.assertEqual(attempts, [broken.name])

    def test_service_install_writes_a_launch_agent_on_macos(self):
        commands = []

        def fake_run(argv, **kwargs):
            commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with (
            patch.object(rox.sys, "platform", "darwin"),
            patch.object(rox.Path, "home", return_value=self.root),
            patch.object(rox.shutil, "which", return_value="/fixture"),
            patch.object(rox, "run", side_effect=fake_run),
            contextlib.redirect_stdout(io.StringIO()) as printed,
        ):
            rox.main(["service", "install"])
            plist = self.root / "Library" / "LaunchAgents" / "io.rox.service.plist"
            agent = rox.plistlib.loads(plist.read_bytes())
            self.assertEqual(agent["ProgramArguments"], [str(rox.HERE / "rox"), "service", "run"])
            self.assertTrue(agent["RunAtLoad"] and agent["KeepAlive"] and agent["AbandonProcessGroup"])
            self.assertIn(str(self.root / ".local" / "bin"), agent["EnvironmentVariables"]["PATH"])
            self.assertEqual(agent["EnvironmentVariables"]["XDG_STATE_HOME"], str(self.root / "state"))
            self.assertEqual(agent["StandardErrorPath"], str(rox.state_root() / "service.log"))
            domain = f"gui/{os.getuid()}"
            self.assertEqual(commands[-2:], [["launchctl", "bootout", f"{domain}/io.rox.service"],
                                             ["launchctl", "bootstrap", domain, str(plist)]])
            self.assertIn("starts at login", printed.getvalue())
            rox.main(["service", "uninstall"])
        self.assertFalse(plist.exists())

    def test_service_install_writes_a_systemd_user_unit_on_linux(self):
        with (
            patch.object(rox.sys, "platform", "linux"),
            patch.object(rox.shutil, "which", return_value="/fixture"),
            patch.object(rox, "output", return_value="") as command,
            contextlib.redirect_stdout(io.StringIO()) as printed,
        ):
            rox.main(["service", "install"])
        unit = (self.root / "config-home" / "systemd" / "user" / "rox.service").read_text()
        self.assertIn(f'ExecStart="{rox.HERE / "rox"}" "service" "run"', unit)
        self.assertIn("KillMode=process", unit)
        self.assertIn("Restart=always", unit)
        self.assertEqual(command.call_args.args[0], ["systemctl", "--user", "restart", "rox.service"])
        self.assertIn("loginctl enable-linger", printed.getvalue())

    def remote_listing(self, **fields):
        record = {"name": "rv-remote-000011112222", "backend": "container", "phase": "ready",
                  "tailnet": "rv-remote-000011112222.tail.ts.net", "socket": "/home/workspace/.rox/x/r.sock",
                  "home": "/home/workspace", "user": "workspace",
                  "checkouts": [{"id": "web", "name": "web", "workspace": "/home/workspace/web"}], **fields}
        return patch.object(rox, "gather_records", return_value=(
            {"this machine": [], "mini": [record]}, {"offline-box": "timed out"}))

    def test_commands_find_a_guest_on_another_rex_host(self):
        with self.remote_listing(), patch.object(rox, "rex_output", return_value=(0, "Stopped it")) as remote, \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            for command, expected in ((["stop"], ["stop"]), (["start"], ["start"]),
                                      (["reconnect"], ["reconnect", "--no-app-host"]),
                                      (["update-agents"], ["update-agents"])):
                with self.subTest(command=command):
                    rox.main([*command, "rv-remote-000011112222"])
                    host, label, argv = remote.call_args.args
                    self.assertEqual(host, "mini")
                    self.assertEqual(argv, [*rox.REMOTE_ROX, expected[0], "rv-remote-000011112222", *expected[1:]])
        self.assertIn("Stopped it", printed.getvalue())
        with self.remote_listing(), patch.object(rox, "rex_output", return_value=(1, "boom")), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(rox.Error, "rox stop exited with status 1 on mini"):
            rox.main(["stop", "rv-remote-000011112222"])

    def test_down_and_add_run_on_the_host_that_has_the_guest(self):
        with self.remote_listing(), patch.object(rox, "down_remote") as down:
            rox.main(["down", "rv-remote-000011112222"])
        self.assertEqual(down.call_args.args[0].host, "mini")
        with self.remote_listing(), patch.object(rox, "add") as add:
            rox.main(["add", "rv-remote-000011112222", "--repo", "https://git.example.test/a.git", "--branch", "main"])
        self.assertEqual(add.call_args.args[0].host, "mini")

    def test_an_unknown_guest_names_the_hosts_that_could_not_be_asked(self):
        with self.remote_listing(), self.assertRaisesRegex(
                rox.Error, "No managed workspace named rv-gone-000011112222 here or on any Rex host. "
                           "Couldn't ask offline-box"):
            rox.main(["shell", "rv-gone-000011112222"])

    def test_remote_shell_uses_the_tailnet_and_falls_back_to_the_rex_app(self):
        with self.remote_listing(), patch.object(rox, "remote_shell") as remote, \
                patch.object(rox, "app_shell") as app:
            rox.main(["shell", "rv-remote-000011112222", "web"])
        self.assertEqual(remote.call_args.args[0], "mini")
        self.assertEqual(remote.call_args.args[2], "web")
        app.assert_not_called()
        with self.remote_listing(tailnet=""), patch.object(rox, "remote_shell") as remote, \
                patch.object(rox, "app_shell") as app:
            rox.main(["shell", "rv-remote-000011112222", "-S", "mini"])
        remote.assert_not_called()
        self.assertEqual(app.call_args.args, ("mini", "rv-remote-000011112222", None))

    def test_remote_shell_authorizes_a_key_and_ssh_goes_through_the_tailnet_tunnel(self):
        commands = []

        def fake_output(argv, **kwargs):
            commands.append(argv)
            return "[127.0.0.1]:40022 ssh-ed25519 AAAAhost" if argv[0] == "ssh-keyscan" else ""

        @contextlib.contextmanager
        def fake_forward(server, forwards):
            commands.append(["forward", server, forwards])
            yield

        record = {"name": "rv-remote-000011112222", "tailnet": "rv-remote-000011112222.tail.ts.net",
                  "socket": "/home/workspace/.rox/x/r.sock", "home": "/home/workspace", "user": "workspace",
                  "checkouts": [{"id": "web", "name": "web", "workspace": "/home/workspace/web"}]}
        with (
            patch.object(rox.shutil, "which", return_value="/fixture"),
            patch.object(rox, "run", side_effect=lambda argv, **kwargs: Path(argv[-1] + ".pub").write_text("ssh-ed25519 AAAAmine rox\n")),
            patch.object(rox, "rex_output", return_value=(0, "")) as authorize,
            patch.object(rox, "output", side_effect=fake_output),
            patch.object(rox, "tailnet_forward", side_effect=fake_forward),
            patch.object(rox, "free_port", return_value=40022),
            patch.object(rox.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as ssh,
        ):
            rox.remote_shell("mini", record)
        server, _, argv = authorize.call_args.args
        self.assertEqual(server, record["tailnet"])
        self.assertEqual(argv[-1], "ssh-ed25519 AAAAmine rox")
        self.assertIn(["forward", record["tailnet"], [(40022, 22)]], commands)
        known_hosts = rox.state_root() / "remote" / "rv-remote-000011112222.known_hosts"
        self.assertEqual(known_hosts.read_text(), "rv-remote-000011112222 ssh-ed25519 AAAAhost\n")
        argv = ssh.call_args.args[0]
        self.assertEqual(argv[argv.index("-p") + 1], "40022")
        self.assertEqual(argv[argv.index("-l") + 1], "workspace")
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertEqual(shlex.split(argv[-1])[-1], "/home/workspace/web")
        self.assertEqual(shlex.split(argv[-1])[1], "REX_SERVER=unix:///home/workspace/.rox/x/r.sock")
        with self.assertRaisesRegex(rox.Error, "Upgrade rox on mini"):
            rox.remote_shell("mini", {**record, "socket": ""})

    def test_remote_forward_tunnels_through_the_guest_rex_server(self):
        with self.remote_listing(), patch.object(rox.os, "execvp") as execvp, \
                contextlib.redirect_stdout(io.StringIO()):
            rox.main(["forward", "rv-remote-000011112222", "5173", "8080:3000"])
        self.assertEqual(execvp.call_args.args[1], [
            "rex", "--autostart=false", "-S", "rv-remote-000011112222.tail.ts.net", "forward", "new",
            "-L", "127.0.0.1:5173:127.0.0.1:5173", "-L", "127.0.0.1:8080:127.0.0.1:3000"])
        with self.remote_listing(tailnet=""), self.assertRaisesRegex(rox.Error, "forward its ports from mini"):
            rox.main(["forward", "rv-remote-000011112222", "5173"])

    def test_ls_json_reports_what_a_remote_shell_needs(self):
        guest = self.saved_guest()
        guest.projects = [rox.Project("web", "web", "", "main", workspace="/home/workspace/web", phase="ready")]
        guest.save()
        record = rox.local_records()[0]
        self.assertEqual(record["socket"], guest.guest_socket)
        self.assertEqual(record["user"], "workspace")
        self.assertEqual(record["checkouts"], [{"id": "web", "name": "web", "workspace": "/home/workspace/web"}])

    FUNNEL_STATUS = {
        "TCP": {"10000": {"HTTPS": True}},
        "Web": {"box.tail.ts.net:10000": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:51460"}}}},
        "AllowFunnel": {"box.tail.ts.net:10000": True},
    }

    def fake_tailscale(self, funnel_status=None):
        """Patch the Tailscale CLI, returning the commands it was asked to run."""
        commands = []
        status = funnel_status if funnel_status is not None else {}

        def fake_json(*args):
            return status if args == ("funnel", "status") else {"Self": {"DNSName": "box.tail.ts.net."}}

        def fake_run(argv, **kwargs):
            commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        patches = [patch.object(rox, "tailscale_cli", return_value="tailscale"),
                   patch.object(rox, "tailscale_json", side_effect=fake_json),
                   patch.object(rox, "open_funnel_tunnel", return_value=False),
                   patch.object(rox, "close_funnel_tunnel"),
                   patch.object(rox, "free_port", return_value=51460),
                   patch.object(rox, "run", side_effect=fake_run)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return commands

    def test_tailscale_runs_as_the_cli_and_explains_output_that_isnt_json(self):
        seen = []

        def fake_run(argv, **kwargs):
            seen.append((argv, kwargs.get("env", {}).get("TAILSCALE_BE_CLI")))
            text = "The Tailscale GUI failed to start" if argv[1] == "status" else '{"Self": {}}'
            return subprocess.CompletedProcess(argv, 0, text, "")

        with patch.object(rox, "tailscale_cli", return_value="/Applications/Tailscale.app/Contents/MacOS/Tailscale"), \
                patch.object(rox, "run", side_effect=fake_run):
            self.assertEqual(rox.tailscale_json("funnel", "status"), {"Self": {}})
            with self.assertRaisesRegex(rox.Error, "tailscale status didn't print JSON: The Tailscale GUI failed"):
                rox.tailscale_json("status")
        self.assertEqual({flag for _, flag in seen}, {"1"})

    def test_funnel_handlers_read_what_tailscale_serves_and_whether_its_public(self):
        with patch.object(rox, "tailscale_json", return_value=self.FUNNEL_STATUS):
            self.assertEqual(rox.funnel_handlers(), {(10000, "/"): ("http://127.0.0.1:51460", True)})

    def test_funnel_tunnels_the_guest_port_and_publishes_it(self):
        guest = self.saved_guest()
        commands = self.fake_tailscale()
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            rox.main(["funnel", guest.name, "5173", "--https", "10000", "--path", "app"])
        self.assertEqual(commands[-1], ["tailscale", "funnel", "--bg", "--yes", "--https=10000", "--set-path=/app",
                                        "http://127.0.0.1:51460"])
        self.assertEqual(rox.Guest.load(guest.name).funnels, [
            {"port": 5173, "https": 10000, "path": "/app", "local": 51460,
             "url": "https://box.tail.ts.net:10000/app"}])
        self.assertIn("Funneling https://box.tail.ts.net:10000/app to port 5173", printed.getvalue())
        self.assertIn("Anyone on the internet can open it", printed.getvalue())
        self.assertEqual(rox.local_records()[0]["funnels"], [{"url": "https://box.tail.ts.net:10000/app", "port": 5173}])
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            rox.main(["funnel", guest.name])
        self.assertIn("https://box.tail.ts.net:10000/app  ->  port 5173", printed.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            rox.main(["funnel", guest.name, "--off"])
        self.assertEqual(commands[-1], ["tailscale", "funnel", "--yes", "--https=10000", "--set-path=/app", "off"])
        self.assertEqual(rox.Guest.load(guest.name).funnels, [])

    def test_funnel_refuses_an_address_another_guest_or_tailscale_already_serves(self):
        self.saved_guest("rv-other-000011112222", funnels=[
            {"port": 3000, "https": 443, "path": "/", "local": 50000, "url": "https://box.tail.ts.net/"}])
        guest = self.saved_guest()
        self.fake_tailscale(self.FUNNEL_STATUS)
        with self.assertRaisesRegex(rox.Error, "rv-other-000011112222 already funnels port 443 at /"):
            rox.main(["funnel", guest.name, "5173"])
        with self.assertRaisesRegex(rox.Error, "Tailscale already serves http://127.0.0.1:51460 on port 10000 at /"):
            rox.main(["funnel", guest.name, "5173", "--https", "10000"])
        for argv in (["0"], ["5173", "--https", "8080"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                rox.main(["funnel", guest.name, *argv])

    def test_resume_restores_funnels_tailscale_no_longer_serves(self):
        entry = {"port": 8000, "https": 10000, "path": "/", "local": 51460, "url": "https://box.tail.ts.net:10000/"}
        guest = self.saved_guest(funnels=[dict(entry)])
        commands = self.fake_tailscale(self.FUNNEL_STATUS)
        with patch.object(rox, "ensure_runtime"):
            self.assertFalse(rox.resume(guest))
            self.assertEqual(commands, [])
            with patch.object(rox, "tailscale_json", return_value={}):
                self.assertTrue(rox.resume(guest))
        self.assertEqual(commands[-1][:2], ["tailscale", "funnel"])
        # A funnel problem is reported without keeping the guest from coming back.
        with patch.object(rox, "ensure_runtime"), patch.object(rox, "tailscale_json", side_effect=rox.Error("no cli")), \
                contextlib.redirect_stderr(io.StringIO()) as problems:
            self.assertFalse(rox.resume(guest))
        self.assertIn("Could not restore the funnels", problems.getvalue())

    def test_stop_takes_funnels_offline_and_keeps_them(self):
        entry = {"port": 8000, "https": 443, "path": "/", "local": 51460, "url": "https://box.tail.ts.net/"}
        guest = self.saved_guest(funnels=[entry])
        commands = self.fake_tailscale()
        with patch.object(rox, "output", return_value=""), patch.object(rox, "close_tunnel"), \
                contextlib.redirect_stdout(io.StringIO()):
            rox.main(["stop", guest.name])
        self.assertIn(["tailscale", "funnel", "--yes", "--https=443", "off"], commands)
        self.assertEqual(rox.Guest.load(guest.name).funnels, [entry])

    def test_funnel_runs_on_the_host_that_has_the_guest(self):
        with self.remote_listing(), patch.object(rox, "rex_output", return_value=(0, "")) as remote, \
                contextlib.redirect_stdout(io.StringIO()):
            rox.main(["funnel", "rv-remote-000011112222", "5173", "--https", "8443"])
        self.assertEqual(remote.call_args.args[2], [*rox.REMOTE_ROX, "funnel", "rv-remote-000011112222", "5173",
                                                    "--https=8443", "--path=/"])

    def fake_app_servers(self, connected_after=0):
        """Fake the Rex app here and on a guest's server, which the app reaches after some checks."""
        here = {"client_id": "client:here", "info": {"kind": "app", "label": "laptop"},
                "connection_state": "connected", "principal": {"transport": "unix"}}
        there = {"client_id": "client:there", "info": {"kind": "app", "label": "laptop"},
                 "connection_state": "connected", "principal": {"transport": "tcp"}}
        other = {"client_id": "client:phone", "info": {"kind": "app", "label": "phone"},
                 "connection_state": "connected", "principal": {"transport": "tcp"}}
        checks = {"count": 0}
        commands = []

        def fake_clients(server=None):
            if server is None:
                return [here]
            checks["count"] += 1
            return [other, there] if checks["count"] > connected_after else [other]

        def fake_run(argv, **kwargs):
            commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        for item in (patch.object(rox, "app_clients", side_effect=fake_clients),
                     patch.object(rox, "local_app", REAL_LOCAL_APP),
                     patch.object(rox, "run", side_effect=fake_run),
                     patch.object(rox.time, "sleep")):
            item.start()
            self.addCleanup(item.stop)
        return commands

    def test_open_in_app_selects_the_session_when_the_app_is_already_on_the_guest(self):
        commands = self.fake_app_servers()
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            rox.open_in_app("rv-x", "rv-x.tail.ts.net", "session:guest")
        self.assertEqual(commands, [["rex", "--autostart=false", "-S", "rv-x.tail.ts.net", "-C", "client:there",
                                     "do", "session.select", "session_id=session:guest"]])
        self.assertIn("Splits and new tabs there run in the guest", printed.getvalue())

    def test_open_in_app_adds_the_guest_to_the_app_and_waits_for_it_to_connect(self):
        commands = self.fake_app_servers(connected_after=2)
        with patch.object(rox, "add_app_host") as added, contextlib.redirect_stdout(io.StringIO()) as printed:
            rox.open_in_app("rv-x", "rv-x.tail.ts.net", "session:guest")
        self.assertEqual(added.call_args.args, ("rv-x", "rv-x.tail.ts.net"))
        self.assertEqual(commands, [["rex", "--autostart=false", "-S", "rv-x.tail.ts.net", "-C", "client:there",
                                     "do", "session.select", "session_id=session:guest"]])
        self.assertIn("Waiting for the Rex app to connect to rv-x", printed.getvalue())

    def test_open_needs_the_app_here(self):
        with self.assertRaisesRegex(rox.Error, "Open the Rex app on this machine"):
            rox.open_in_app("rv-x", "rv-x.tail.ts.net", "session:guest")

    def test_rox_open_picks_a_local_or_remote_guests_session(self):
        guest = self.saved_guest()
        guest.projects = [rox.Project("web", "web", "", "main", workspace="/w", session_id="session:web", phase="ready")]
        guest.save()
        with patch.object(rox, "open_in_app") as opened:
            rox.main(["open", guest.name])
        self.assertEqual(opened.call_args.args, (guest.name, guest.endpoint, "session:web", guest.endpoint))
        with self.remote_listing(sessions={"": "session:home", "web": "session:remote-web"}), \
                patch.object(rox, "open_in_app") as opened:
            rox.main(["open", "rv-remote-000011112222", "web"])
        self.assertEqual(opened.call_args.args,
                         ("rv-remote-000011112222", "rv-remote-000011112222.tail.ts.net", "session:remote-web",
                          "rv-remote-000011112222.tail.ts.net"))
        with self.remote_listing(), self.assertRaisesRegex(rox.Error, "Upgrade rox on mini"):
            rox.main(["open", "rv-remote-000011112222"])
        with self.remote_listing(tailnet="", sessions={}), self.assertRaisesRegex(rox.Error, "isn't on the tailnet"):
            rox.main(["open", "rv-remote-000011112222"])

    def test_open_shell_uses_the_app_and_falls_back_to_a_guest_tab(self):
        guest = self.saved_guest(session_id="session:guest")
        with patch.object(rox, "local_app", return_value={"client_id": "client:here"}), \
                patch.object(rox, "open_in_app") as opened, patch.object(rox, "open_app_shell") as tab:
            rox.open_or_tab("session:mine", guest)
        self.assertEqual(opened.call_args.args, (guest.name, guest.endpoint, "session:guest", guest.endpoint))
        tab.assert_not_called()
        with patch.object(rox, "open_in_app") as opened, patch.object(rox, "open_app_shell") as tab:
            rox.open_or_tab("session:mine", guest)
        opened.assert_not_called()
        self.assertEqual(tab.call_args.args, ("session:mine", guest.name, None))

    def test_remote_up_opens_the_guest_here_or_suggests_rox_shell(self):
        provisioned = ("Workspace  rv-remote-000011112222\nTailnet    rv-remote-000011112222.tail.ts.net\n"
                       "Session    session:guest\n")
        with patch.object(rox, "local_app", return_value={"client_id": "client:here"}), \
                patch.object(rox, "open_in_app") as opened:
            rox.open_remote_guest(provisioned, "mini")
        self.assertEqual(opened.call_args.args,
                         ("rv-remote-000011112222", "rv-remote-000011112222.tail.ts.net", "session:guest"))
        for output_text in (provisioned, provisioned.replace("Tailnet", "Other")):
            with self.subTest(output_text=output_text), patch.object(rox, "open_in_app") as opened, \
                    contextlib.redirect_stdout(io.StringIO()) as printed:
                rox.open_remote_guest(output_text, "mini")
            opened.assert_not_called()
            self.assertIn("rox shell rv-remote-000011112222", printed.getvalue())

    def test_guest_shells_point_rex_at_the_guest_session(self):
        guest = self.saved_guest(address="127.0.0.1", session_id="session:home")
        guest.projects = [rox.Project("web", "web", "", "main", workspace="/home/workspace/web",
                                      session_id="session:web", phase="ready")]
        guest.save()
        with patch.object(rox.os, "execvpe") as exec_run:
            rox.main(["shell", guest.name])
        command = shlex.split(exec_run.call_args.args[1][-1])
        self.assertIn("REX_SESSION=session:web", command)
        self.assertEqual(rox.local_records()[0]["sessions"], {"": "session:home", "web": "session:web"})

    def test_reconnect_registers_a_retained_guest_that_never_created_a_session(self):
        name = "rv-retained-000011112222"
        guest = rox.Guest(name, "container", "image", "workspace",
                          engine="docker", guest_socket="/home/workspace/r.sock", phase="failed")
        guest.directory.mkdir(parents=True)
        guest.save()
        state_file = guest.directory / "guest.json"
        state = json.loads(state_file.read_text())
        state_file.write_text(json.dumps({**state, "register_host": False}))
        with (
            patch.object(rox, "container_address", return_value=("127.0.0.1", 33000)),
            patch.object(rox, "guest_stage") as stage,
            patch.object(rox, "open_tunnel"),
            patch.object(rox, "run", return_value=subprocess.CompletedProcess([], 0, "[]", "")) as command,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            rox.main(["reconnect", name])
        self.assertEqual(stage.call_args.args[1:], ("server", guest.guest_socket))
        self.assertEqual(command.call_args.args[0], ["rex", "hosts", "add", name, guest.endpoint])
        reopened = rox.Guest.load(name)
        self.assertTrue(reopened.registered)
        self.assertEqual(reopened.phase, "ready")

    def test_tunnel_eof_checks_guest_and_falls_back_to_ssh_stdio(self):
        guest = rox.Guest("rv-fixture-000011112222", "container", "image", "workspace",
                          guest_socket="/home/workspace/r.sock")
        calls = []

        def probe(argv, **kwargs):
            calls.append(argv)
            if argv[-2:] == ["ls", "--json"]:
                raise rox.Error("WebSocket handshake: EOF")
            return "{}"

        with (
            patch.object(rox, "open_tunnel", side_effect=lambda guest: calls.append("tunnel")),
            patch.object(rox, "output", side_effect=probe),
            patch.object(rox, "guest_stage", side_effect=lambda guest, *args: calls.append(args)),
            patch.object(rox, "close_tunnel", side_effect=lambda guest: calls.append("close")),
            patch.object(rox, "open_relay", side_effect=lambda guest: calls.append("relay")),
        ):
            rox.attach_guest(guest, "session:fixture")
        self.assertEqual(calls, [
            "tunnel",
            [*rox.rex_on(guest), "ls", "--json"],
            ("server", guest.guest_socket),
            "close",
            "relay",
            [*rox.rex_on(guest), "session", "inspect", "session:fixture", "--json"],
        ])

    def provision(self, backend, fail_stage=None, open_shell=False, prebuilt="", engine=None):
        nested = self.make_repo()
        key = self.root / "git_key"
        key.touch()
        identity = self.root / "vm_key"
        identity.touch()
        host_key = self.root / "vm_host_key.pub"
        host_key.write_text("ssh-ed25519 AAAA fixture\n")
        config = self.root / "config.json"
        config.write_text(json.dumps({
            "git_keys": [str(key)],
            backend: {"image": "fixture", "identity": str(identity), "host_key": str(host_key),
                      **({"engine": engine} if engine else {})},
        }))

        commands = []
        stages = []
        active_agent = None

        @contextlib.contextmanager
        def agent(guest, keys):
            nonlocal active_agent
            self.assertEqual(keys, [key])
            active_agent = guest.runtime / "agent.sock"
            try:
                yield active_agent
            finally:
                active_agent = None

        def stage(guest, name, *args, agent=None, capture=False):
            stages.append(name)
            if name == "prepare":
                return f"/home/guest/.rox/{guest.name}\n/home/guest/.rox/{guest.name}/r.sock"
            if name == "clone":
                self.assertIsNotNone(agent)
                self.assertEqual(agent, active_agent)
                self.assertTrue(rox.Guest.load(guest.name).registered)
            else:
                self.assertIsNone(active_agent)
            if name == fail_stage:
                raise rox.Error(f"Fixture {name} failed")
            if name == "setup":
                self.assertEqual(args[-1], "prebuilt" if prebuilt else "false")
            if name == "session":
                self.assertIn(f"unix:///home/guest/.rox/{guest.name}/r.sock", args)
                self.assertEqual(args[-3:], ("--", "agent", "an argument with spaces"))
                return '{"session_id":"session:fixture"}'
            return None

        real_run = rox.run

        def command(argv, capture=False, check=True, **kwargs):
            if argv[0] == "git":
                return real_run(argv, capture=capture, check=check, **kwargs)
            commands.append(argv)
            stdout = ""
            if argv[1:2] == ["ps"]:
                stdout = "managed-container"
            elif argv[1:2] == ["list"]:
                guest = next(rox.state_root().glob("*/guest.json"))
                stdout = json.dumps([{"Name": guest.parent.name, "Running": True}])
            elif argv[2:4] == ["window", "new"]:
                stdout = json.dumps({"window_id": "window:guest"})
            elif argv[4:6] == ["window", "ls"]:
                stdout = json.dumps({"windows": [{"window_id": "window:mine"}]})
            return subprocess.CompletedProcess(argv, 0, stdout, "")

        fake_runtime = property(lambda guest: self.root / "runtime" / guest.name)
        with (
            patch.dict(os.environ, {"REX_SESSION": "session:launcher"}),
            patch.object(rox.Guest, "runtime", fake_runtime),
            patch.object(rox, "run", side_effect=command),
            patch.object(rox, "git_host_entries", return_value=""),
            patch.object(rox, "prepare_project_image", side_effect=lambda *a: prebuilt),
            patch.object(rox, "git_agent", side_effect=agent),
            patch.object(rox, "guest_stage", side_effect=stage),
            patch.object(rox, "check_guest_os"),
            patch.object(rox, "launch_guest", return_value=("127.0.0.1", 22)),
            patch.object(rox, "open_tunnel"),
            patch.object(rox.shutil, "which", return_value="/fixture"),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            argv = ["up", backend, "--cwd", str(nested), "--config", str(config),
                    "--", "agent", "an argument with spaces"]
            if open_shell:
                argv.insert(2, "--open-shell")
            if fail_stage:
                with self.assertRaisesRegex(rox.Error, f"Fixture {fail_stage} failed"):
                    rox.main(argv)
            else:
                rox.main(argv)
            name = next(rox.state_root().glob("*/guest.json")).parent.name
            state = rox.Guest.load(name)
            # A failed clone or hook leaves the guest itself usable.
            self.assertEqual(state.phase, "ready")
            self.assertEqual((state.image, state.prebuilt), (prebuilt or "fixture", bool(prebuilt)))
            self.assertTrue(state.registered)
            self.assertEqual(commands.count(["rex", "hosts", "add", name, state.endpoint]), 1)
            [project] = state.projects
            self.assertEqual((project.id, project.name, project.branch),
                             ("sample-project", "sample project", "published-feature"))
            self.assertEqual(project.workspace, f"/home/guest/.rox/image/project" if prebuilt
                             else f"/home/guest/.rox/{name}/projects/sample-project")
            self.assertEqual(project.phase, "failed" if fail_stage else "ready")
            if not fail_stage:
                self.assertEqual(project.session_id, "session:fixture")
                self.assertEqual(state.session_id, "")
                if open_shell:
                    self.assertIn(["rex", "--autostart=false", "window", "new", "-s", "session:launcher",
                                   "--keep-open", "--json", "Guest", "--", str(ROOT / "rox"), "shell", name,
                                   "sample-project"], commands)
                    self.assertEqual(rox.Guest.load(name).shell_windows,
                                     [{"session": "session:launcher", "window": "window:guest"}])
            rox.main(["down", name])
            rex = ["rex", "--autostart=false", "--timeout", "5s"]
            if open_shell and not fail_stage:
                # The Guest tab closes, and the session stays because it has other tabs.
                self.assertIn([*rex, "window", "close", "-s", "session:launcher", "window:guest"], commands)
                self.assertNotIn([*rex, "kill", "session:launcher"], commands)
            self.assertFalse(state.directory.exists())
            self.assertIn(["rex", "hosts", "remove", name], commands)
            if engine == "container":
                self.assertIn(["container", "delete", "--force", name], commands)
                self.assertIn(["container", "system", "status"], commands)
            elif backend == "container":
                self.assertIn(["docker", "rm", "-f", "-v", "managed-container"], commands)
            else:
                self.assertIn(["tart", "stop", name], commands)
                self.assertIn(["tart", "delete", name], commands)
        return stages

    def test_all_backends_register_a_ready_rex_session(self):
        for backend in ("container", "linux", "macos"):
            with self.subTest(backend=backend):
                self.assertEqual(self.provision(backend), ["prepare", "server", "clone", "setup", "session"])

    def test_apple_container_engine_provisions_and_removes_its_container(self):
        self.assertEqual(self.provision("container", engine="container"),
                         ["prepare", "server", "clone", "setup", "session"])

    def test_default_engine_is_apple_container_only_on_apple_silicon_macs(self):
        for platform_name, machine, expected in (
            ("darwin", "arm64", "container"), ("darwin", "x86_64", "docker"), ("linux", "aarch64", "docker"),
        ):
            with (
                patch.object(rox.sys, "platform", platform_name),
                patch.object(rox.platform, "machine", return_value=machine),
                patch.object(rox, "default_engine", REAL_DEFAULT_ENGINE),
            ):
                self.assertEqual(rox.default_engine(), expected)

    def test_apple_container_launch_connects_to_the_container_ip(self):
        guest = rox.Guest("rv-fixture-000011112222", "container", "rox:linux", "workspace", engine="container",
                          identity=str(self.root / "login"), cpus=2, memory="4G")
        (self.root / "login.pub").write_text("ssh-ed25519 AAAA fixture\n")
        # The shape container 1.7 prints: the configured network has no address, the running one does.
        inspect = json.dumps([{"id": guest.name, "configuration": {"networks": [
            {"network": "default", "options": {"hostname": guest.name}}]}, "status": {
            "state": "running", "networks": [{"network": "default", "ipv4Address": "192.168.64.7/24",
                                              "ipv4Gateway": "192.168.64.1"}]}}])
        with (
            patch.object(rox, "run"),
            patch.object(rox, "output", side_effect=[
                "rv-fixture-000011112222", "ssh-ed25519 AAAA host", inspect,
            ]) as output,
            patch.object(rox, "pin_host") as pin,
            patch.object(rox.Guest, "save"),
        ):
            self.assertEqual(rox.launch_guest(guest, 5), ("192.168.64.7", 22))
        launch = output.call_args_list[0].args[0]
        self.assertEqual(launch[:5], ["container", "run", "--detach", "--name", guest.name])
        self.assertIn("--cpus=2", launch)
        self.assertIn("--memory=4G", launch)
        self.assertNotIn("--publish", launch)
        self.assertEqual(launch[-1], "rox:linux")
        self.assertEqual(output.call_args_list[1].args[0],
                         ["container", "exec", guest.name, "cat", "/etc/ssh/ssh_host_ed25519_key.pub"])
        pin.assert_called_once_with(guest, "ssh-ed25519 AAAA host")

    def test_image_info_reads_apple_container_labels(self):
        shape = [{"name": "rox:fixture", "index": {"digest": "sha256:index"}, "variants": [
            {"platform": {"os": "linux", "architecture": "arm64"},
             "config": {"config": {"Labels": {"io.rox.hook": "abc"}}}}]}]
        with patch.object(rox, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(shape), "")):
            self.assertEqual(rox.image_info("container", "rox:fixture"), ("sha256:index", {"io.rox.hook": "abc"}))

    def test_container_resource_settings_are_validated(self):
        cwd = self.make_repo()
        for settings, message in (
            ({"memory": "lots"}, "size such as 4G"),
            ({"cpus": -1}, "whole number of CPUs"),
        ):
            config = self.root / "config.json"
            config.write_text(json.dumps({"container": settings}))
            with self.subTest(settings=settings), patch.object(rox.shutil, "which", return_value="/fixture"):
                with self.assertRaisesRegex(rox.Error, message):
                    rox.main(["up", "container", "--cwd", str(cwd), "--config", str(config)])
        config.write_text(json.dumps({}))
        with self.assertRaisesRegex(rox.Error, "size such as 4G or 512M for --memory"):
            rox.main(["up", "container", "--memory", "4096", "--cwd", str(cwd), "--config", str(config)])

    def launch_record(self, argv, launcher=None, project=None):
        """Run rox up until the guest would boot, and return its saved record."""
        cwd = self.make_repo()
        config = self.root / "config.json"
        identity, host_key = self.root / "vm_key", self.root / "vm_host_key.pub"
        identity.touch()
        host_key.write_text("ssh-ed25519 AAAA fixture\n")
        vm = {"image": "rox-ubuntu", "identity": str(identity), "host_key": str(host_key)}
        config.write_text(json.dumps(launcher or {"linux": vm, "macos": vm}))
        branch = rox.Branch(self.root, "commit", "", project) if project is not None else None
        real_run, real_output = rox.run, rox.output

        def command(argv, **kwargs):
            if argv[0] == "git":
                return real_run(argv, **kwargs)
            return subprocess.CompletedProcess(argv, 0, base_image() if argv[1:3] == ["image", "inspect"] else "", "")

        with (
            patch.object(rox, "read_branch", return_value=branch),
            patch.object(rox, "run", side_effect=command),
            patch.object(rox, "output", side_effect=lambda argv, **kw: real_output(argv, **kw) if argv[0] == "git" else ""),
            patch.object(rox, "git_host_entries", return_value=""),
            patch.object(rox, "prepare_project_image", return_value=""),
            patch.object(rox, "launch_guest", side_effect=rox.Error("stop before boot")),
            patch.object(rox.Guest, "runtime", property(lambda guest: self.root / "runtime" / guest.name)),
            patch.object(rox.shutil, "which", return_value="/fixture"),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaisesRegex(rox.Error, "stop before boot"):
                rox.main(["up", *argv, "--cwd", str(cwd), "--config", str(config)])
        [record] = rox.state_root().glob("*/guest.json")
        return json.loads(record.read_text())

    def test_project_config_picks_the_backend_and_overrides_the_launcher_config(self):
        record = self.launch_record([], project={
            "backend": "container", "container": {"image": "ghcr.io/acme/agent:1", "memory": "8G"}},
            launcher={"container": {"engine": "podman", "memory": "2G", "cpus": 2}})
        self.assertEqual((record["backend"], record["engine"], record["image"]),
                         ("container", "podman", "ghcr.io/acme/agent:1"))
        self.assertEqual((record["cpus"], record["memory"]), (2, "8G"))

    def test_command_line_overrides_the_project_config(self):
        record = self.launch_record(
            ["linux", "--image", "rox-ubuntu-gpu", "--cpus", "8", "--memory", "16G"],
            project={"backend": "container", "linux": {"image": "rox-ubuntu-small", "cpus": 2}})
        self.assertEqual((record["backend"], record["image"], record["cpus"], record["memory"]),
                         ("linux", "rox-ubuntu-gpu", 8, "16G"))

    def test_up_needs_a_backend_from_the_command_line_or_project_config(self):
        cwd = self.make_repo()
        with patch.object(rox.shutil, "which", return_value="/fixture"), \
                patch.object(rox, "git_host_entries", return_value=""):
            with self.assertRaisesRegex(rox.Error, "Choose a backend"):
                rox.main(["up", "--cwd", str(cwd)])

    def test_project_config_is_read_from_the_branch_and_limited_to_project_settings(self):
        origin, git = self.make_origin(hook="")
        (origin / ".rox.json").write_text(json.dumps({"backend": "linux", "linux": {"memory": "8G"}}))
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "--quiet", "-m", "settings"], check=True)
        with contextlib.redirect_stdout(io.StringIO()):
            mirror, commit, _ = rox.fetch_branch(str(origin), "main", dict(os.environ))
        self.assertEqual(rox.project_settings(mirror, commit), {"backend": "linux", "linux": {"memory": "8G"}})
        self.assertEqual(rox.project_settings(mirror, f"{commit}~1"), {})
        for settings, message in (
            ({"container": {"engine": "docker"}}, "only be set in the rox config on the launch machine"),
            ({"linux": {"identity": "~/.ssh/id"}}, "only be set in the rox config on the launch machine"),
            ({"git_keys": []}, "only be set in the rox config on the launch machine"),
            ({"container": {"disk": "10G"}}, "Unknown setting container.disk"),
            ({"backend": "windows"}, "Expected backend"),
            ([], "Expected a JSON object"),
        ):
            (origin / ".rox.json").write_text(json.dumps(settings))
            subprocess.run([*git, "commit", "--quiet", "-am", "change"], check=True)
            with contextlib.redirect_stdout(io.StringIO()):
                mirror, commit, _ = rox.fetch_branch(str(origin), "main", dict(os.environ))
            with self.subTest(settings=settings), self.assertRaisesRegex(rox.Error, message):
                rox.project_settings(mirror, commit)

    def test_vm_resources_are_set_on_the_clone_before_it_boots(self):
        guest = rox.Guest("rv-fixture-000011112222", "linux", "rox-ubuntu", "admin", cpus=6, memory="8G")
        guest.directory.mkdir(parents=True, mode=0o700)
        commands = []
        with (
            patch.object(rox, "run", side_effect=lambda argv, **kwargs: commands.append(argv)),
            patch.object(rox.subprocess, "Popen", side_effect=rox.Error("stop before boot")),
            self.assertRaisesRegex(rox.Error, "stop before boot"),
        ):
            rox.launch_guest(guest, 5)
        self.assertEqual(commands, [["tart", "clone", "rox-ubuntu", guest.name],
                                    ["tart", "set", guest.name, "--cpu=6", "--memory=8192"]])
        self.assertEqual([rox.memory_megabytes(size) for size in ("512M", "2g", "1T", "2048K")],
                         [512, 2048, 1048576, 2])

    def test_docker_resources_are_passed_to_run(self):
        guest = rox.Guest("rv-fixture-000011112222", "container", "rox:linux", "workspace", engine="docker",
                          identity=str(self.root / "login"), cpus=2, memory="4G")
        (self.root / "login.pub").write_text("ssh-ed25519 AAAA fixture\n")
        with (
            patch.object(rox, "run"),
            patch.object(rox, "output", side_effect=["id", "ssh-ed25519 AAAA host", "127.0.0.1:33000"]) as output,
            patch.object(rox, "pin_host"),
            patch.object(rox.Guest, "save"),
        ):
            rox.launch_guest(guest, 5)
        launch = output.call_args_list[0].args[0]
        self.assertEqual(launch[-3:], ["--cpus=2", "--memory=4G", "rox:linux"])

    def test_remote_launch_forwards_resources_and_lets_the_launcher_pick_the_backend(self):
        cwd = self.make_repo()
        with patch.object(rox, "dispatch", return_value="") as dispatch:
            rox.main(["up", "--host", "builder", "--cpus", "4", "--memory", "8G", "--cwd", str(cwd)])
        launch = dispatch.call_args.args[2]
        self.assertEqual(launch[0], "up")
        self.assertTrue(launch[1].startswith("--project="))
        args = dispatch.call_args.args[0]
        self.assertEqual((args.cpus, args.memory), (4, "8G"))

    def test_failed_clone_or_hook_keeps_the_rex_host_registered(self):
        self.assertEqual(self.provision("linux", fail_stage="clone"), ["prepare", "server", "clone"])
        self.assertEqual(self.provision("linux", fail_stage="setup"), ["prepare", "server", "clone", "setup"])

    def test_hosted_provisioning_opens_guest_tab(self):
        self.assertEqual(self.provision("linux", open_shell=True), ["prepare", "server", "clone", "setup", "session"])

    def test_prebuilt_image_skips_the_hook_at_boot(self):
        self.assertEqual(self.provision("container", prebuilt="rox:sample-project"),
                         ["prepare", "server", "clone", "setup", "session"])
        self.assertEqual(self.provision("linux", prebuilt="rox-sample-project-linux"),
                         ["prepare", "server", "clone", "setup", "session"])

    def make_origin(self, hook="touch baked\n"):
        origin = self.root / "origin"
        git = ["git", "-C", str(origin)]
        subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(origin)], check=True)
        subprocess.run([*git, "config", "user.email", "fixture@example.test"], check=True)
        subprocess.run([*git, "config", "user.name", "Fixture"], check=True)
        (origin / "README").write_text("one\n")
        if hook:
            (origin / ".rox.sh").write_text(hook)
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "--quiet", "-m", "one"], check=True)
        return origin, git

    def guest_home(self):
        # The bootstrap only allows SSH and HTTPS remotes; a git shim lets it fetch a local fixture.
        home = self.root / "home"
        shim = home / ".local" / "bin" / "git"
        shim.parent.mkdir(parents=True)
        shim.write_text(f'#!/bin/sh\nunset GIT_ALLOW_PROTOCOL\nexec {shutil.which("git")} "$@"\n')
        shim.chmod(0o755)
        return home

    def test_bootstrap_bakes_then_boots_from_the_image_checkout(self):
        origin, git = self.make_origin('printf "%s %s %s" "$ROX_PHASE" "$ROX_NAME" "${REX_SERVER:-none}" > baked\n')
        home = self.guest_home()
        # prepare only checks that the guest has Rex, so a stub keeps the test off the host's install.
        tools = self.root / "guest-tools"
        tools.mkdir()
        (tools / "rex").write_text("#!/bin/sh\nexit 0\n")
        (tools / "rex").chmod(0o755)
        env = {**os.environ, "HOME": str(home), "REX_SERVER": "unix:///leak",
               "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}"}
        bootstrap = ["bash", str(ROOT / "guest/bootstrap.sh")]
        workspace = subprocess.run([*bootstrap, "bake-prepare"], env=env, check=True,
                                   capture_output=True, text=True).stdout.strip()
        self.assertEqual(workspace, str(home / ".rox/image/project"))
        subprocess.run([*bootstrap, "clone", workspace, str(origin), "main", "no"], env=env, check=True,
                       capture_output=True)
        subprocess.run([*bootstrap, "bake", workspace, "linux", "rox-fixture-linux"], env=env, check=True,
                       capture_output=True)
        self.assertEqual((Path(workspace) / "baked").read_text(), "build rox-fixture-linux none")

        # A later boot finds the baked checkout and fast-forwards it, keeping prepared files.
        (origin / "README").write_text("two\n")
        subprocess.run([*git, "commit", "--quiet", "-am", "two"], check=True)
        paths = subprocess.run([*bootstrap, "prepare", "rv-fixture-000011112222"], env=env, check=True,
                               capture_output=True, text=True).stdout.splitlines()
        self.assertEqual(paths, [str(home / ".rox/rv-fixture-000011112222"),
                                 str(home / ".rox/rv-fixture-000011112222/r.sock")])
        subprocess.run([*bootstrap, "clone", workspace, str(origin), "main", "no"], env=env, check=True,
                       capture_output=True)
        self.assertEqual((Path(workspace) / "README").read_text(), "two\n")
        self.assertTrue((Path(workspace) / "baked").exists())
        upstream = subprocess.run(["git", "-C", workspace, "rev-parse", "--abbrev-ref", "@{u}"],
                                  check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(upstream, "origin/main")
        setup = subprocess.run([*bootstrap, "setup", workspace, "rv-fixture-000011112222", "linux",
                                "/tmp/r.sock", "prebuilt"], env=env, capture_output=True, text=True)
        self.assertEqual(setup.returncode, 0)
        self.assertIn("Skipping .rox.sh", setup.stderr)
        self.assertEqual((Path(workspace) / "baked").read_text(), "build rox-fixture-linux none")

    def test_mirror_detects_the_hook_and_exports_a_checkout(self):
        origin, git = self.make_origin()
        cache = patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")})
        cache.start()
        self.addCleanup(cache.stop)
        with contextlib.redirect_stdout(io.StringIO()):
            mirror, commit, hook = rox.fetch_branch(str(origin), "main", dict(os.environ))
            blob = subprocess.run([*git, "rev-parse", "HEAD:.rox.sh"], check=True,
                                  capture_output=True, text=True).stdout.strip()
            self.assertEqual(hook, blob)
            checkout = self.root / "export"
            self.assertEqual(rox.export_checkout(mirror, "git@git.example.test:repo.git", "main", checkout),
                             commit)
            self.assertEqual((checkout / ".rox.sh").read_text(), "touch baked\n")
            remote = subprocess.run(["git", "-C", str(checkout), "remote", "get-url", "origin"],
                                    check=True, capture_output=True, text=True).stdout.strip()
            self.assertEqual(remote, "git@git.example.test:repo.git")
            subprocess.run([*git, "rm", "--quiet", ".rox.sh"], check=True)
            subprocess.run([*git, "commit", "--quiet", "-m", "drop hook"], check=True)
            self.assertEqual(rox.fetch_branch(str(origin), "main", dict(os.environ))[2], "")

    def test_container_project_image_is_reused_until_the_hook_or_base_changes(self):
        origin, _ = self.make_origin()
        cache = patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")})
        cache.start()
        self.addCleanup(cache.stop)
        rox.private_directory(rox.state_root())
        with contextlib.redirect_stdout(io.StringIO()):
            mirror, _, hook = rox.fetch_branch(str(origin), "main", dict(os.environ))
        images = {"rox:linux": ("sha256:base", {})}
        builds = []
        real_run = rox.run

        def command(argv, **kwargs):
            if argv[0] == "git":
                return real_run(argv, **kwargs)
            if argv[1:3] == ["image", "inspect"]:
                if argv[3] not in images:
                    return subprocess.CompletedProcess(argv, 1, "", "missing")
                image_id, labels = images[argv[3]]
                return subprocess.CompletedProcess(argv, 0, json.dumps([{"Id": image_id, "Config": {"Labels": labels}}]), "")
            if argv[1] == "build":
                context = Path(argv[-1])
                containerfile = (context / "Containerfile").read_text()
                self.assertIn("FROM rox:linux", containerfile)
                self.assertIn('"bake", "/home/workspace/.rox/image/project", "container", "rox:fixture"', containerfile)
                self.assertTrue((context / "project/.rox.sh").is_file())
                labels = dict(arg.removeprefix("--label=").split("=", 1) for arg in argv if arg.startswith("--label="))
                builds.append(labels)
                images["rox:fixture"] = (f"sha256:build{len(builds)}", labels)
            return subprocess.CompletedProcess(argv, 0, "", "")

        def build(rebuild=False):
            rox.build_container_image("podman", "rox:linux", "rox:fixture", mirror, "git@x.test:r.git",
                                      "main", hook, rebuild)

        with patch.object(rox, "run", side_effect=command), contextlib.redirect_stdout(io.StringIO()):
            build()
            self.assertEqual(builds[0]["io.rox.hook"], hook)
            self.assertEqual(builds[0]["io.rox.base"], "sha256:base")
            self.assertEqual(builds[0]["io.rox.branch"], "main")
            build()
            self.assertEqual(len(builds), 1)
            build(rebuild=True)
            self.assertEqual(len(builds), 2)
            images["rox:linux"] = ("sha256:newbase", {})
            build()
            self.assertEqual(len(builds), 3)

    def test_project_image_names_avoid_the_base_image(self):
        self.assertEqual(rox.project_image_name("container", "My App", "rox:linux"), "rox:my-app")
        self.assertEqual(rox.project_image_name("container", "linux", "rox:linux"), "rox:linux-project")
        self.assertEqual(rox.project_image_name("macos", "web.app", "rox-macos"), "rox-web-app-macos")

    def test_rebuild_requires_a_project_image(self):
        with self.assertRaisesRegex(rox.Error, "--rebuild needs a project image"):
            rox.main(["up", "container", "--no-hook", "--rebuild", "--project", "fixture",
                      "--repo", "https://git.example.test/repo.git", "--branch", "main"])

    APP_CLIENTS = {"clients": [
        {"client_id": "client:cli", "info": {"kind": "cli"}, "connection_state": "connected",
         "principal": {"transport": "unix"}},
        {"client_id": "client:lost", "info": {"kind": "app"}, "connection_state": "lost",
         "principal": {"transport": "unix"}},
        {"client_id": "client:app", "info": {"kind": "app"}, "connection_state": "connected",
         "principal": {"transport": "unix"}},
    ]}

    def app_host_commands(self, argv, clients=None):
        """Run argv with a fake Rex app and clipboard, and return the commands and clipboard input."""
        calls = []
        clients = self.APP_CLIENTS if clients is None else clients

        def command(args, **kwargs):
            calls.append((args, kwargs.get("input")))
            stdout = json.dumps(clients) if args[-3:] == ["client", "ls", "--json"] else ""
            return subprocess.CompletedProcess(args, 0, stdout, "")

        with (
            patch.object(rox.shutil, "which", return_value="/usr/bin/pbcopy"),
            patch.object(rox, "run", side_effect=command),
            patch.object(rox, "local_app", REAL_LOCAL_APP),
            contextlib.redirect_stdout(io.StringIO()) as printed,
        ):
            rox.main(argv)
        return calls, printed.getvalue()

    def test_add_remote_host_copies_a_local_guest_and_opens_the_app_dialog(self):
        guest = self.saved_guest()
        calls, printed = self.app_host_commands(["add-remote-host", guest.name])
        self.assertEqual([argv for argv, _ in calls], [
            ["rex", "--autostart=false", "--timeout", "5s", "client", "ls", "--json"],
            ["pbcopy"],
            ["rex", "--autostart=false", "-C", "client:app", "do", "client.host.add"],
        ])
        self.assertEqual(calls[1][1], guest.endpoint)
        self.assertIn(f"Copied {guest.endpoint}. Paste it into Add Remote Host", printed)
        guest.tailnet_name = "rv-fixture-000011112222.tail.ts.net"
        guest.save()
        calls, _ = self.app_host_commands(["add-remote-host", guest.name])
        self.assertEqual(calls[1][1], guest.tailnet_name)

    def test_add_remote_host_uses_a_remote_guests_tailnet_name(self):
        with self.remote_listing():
            calls, printed = self.app_host_commands(["add-remote-host", "rv-remote-000011112222"])
        self.assertEqual(calls[1][1], "rv-remote-000011112222.tail.ts.net")
        with self.remote_listing(tailnet=""), self.assertRaisesRegex(rox.Error, "only mini can reach it"):
            self.app_host_commands(["add-remote-host", "rv-remote-000011112222"])

    def test_add_remote_host_needs_the_app_open_here(self):
        guest = self.saved_guest()
        remote_app = {"clients": [{"client_id": "client:app", "info": {"kind": "app"},
                                   "connection_state": "connected", "principal": {"transport": "tcp"}}]}
        with self.assertRaisesRegex(rox.Error, "Open the Rex app on this machine"):
            self.app_host_commands(["add-remote-host", guest.name], remote_app)
        unregistered = self.saved_guest("rv-new-000011112222", registered=False)
        with self.assertRaisesRegex(rox.Error, "isn't a Rex host yet"):
            self.app_host_commands(["add-remote-host", unregistered.name])

    def test_up_suggests_add_remote_host_only_when_an_app_is_open_here(self):
        for client, suggested in (("client:app", True), (None, False)):
            with self.subTest(client=client), patch.object(rox, "local_app_client", return_value=client), \
                    contextlib.redirect_stdout(io.StringIO()) as printed:
                REAL_SUGGEST_APP_HOST("rv-fixture-000011112222")
            self.assertEqual("run rox add-remote-host rv-fixture-000011112222" in printed.getvalue(), suggested)

    def test_reconnect_no_longer_prompts_and_still_accepts_no_app_host(self):
        guest = self.saved_guest()
        stdin = io.StringIO("y\n")
        stdin.isatty = lambda: True
        for argv in (["reconnect", guest.name], ["reconnect", guest.name, "--no-app-host"]):
            with self.subTest(argv=argv), patch.object(rox.sys, "stdin", stdin), \
                    patch.object(rox, "resume"), patch.object(rox, "input", create=True) as asked, \
                    contextlib.redirect_stdout(io.StringIO()):
                rox.main(argv)
            asked.assert_not_called()

    def test_remote_launch_forwards_no_app_host(self):
        real_run = rox.run
        requests = []

        def command(argv, **kwargs):
            if argv[0] == "git":
                return real_run(argv, **kwargs)
            requests.append(argv)
            if argv[4] == "new":
                return subprocess.CompletedProcess(argv, 0, json.dumps({
                    "session_id": "session:launch",
                    "initial_windows": [{"block_ids": ["block:provision"]}],
                }), "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.object(rox, "run", side_effect=command), contextlib.redirect_stdout(io.StringIO()):
            rox.main(["up", "container", "--host", "buildbox", "--project", "demo",
                      "--repo", "https://git.example.test/demo.git", "--branch", "main",
                      "--no-app-host"])
        self.assertIn("--no-app-host", requests[0])

    def save_guest(self, name, project):
        guest = rox.Guest(name, "container", "image", "workspace", engine="docker")
        guest.add_project(project, "https://git.example.test/repo.git", "main")
        guest.directory.mkdir(parents=True)
        guest.save()
        return guest

    def down_without_name(self, answer=None):
        nested = self.make_repo()
        stdin = io.StringIO(answer or "")
        stdin.isatty = lambda: answer is not None
        captured = io.StringIO()
        with (
            patch.object(rox.sys, "stdin", stdin),
            patch.object(rox, "down") as down,
            patch.object(rox, "rex_host_labels", return_value=[]),
            contextlib.redirect_stdout(captured),
        ):
            rox.main(["down", "--cwd", str(nested)])
        return down.call_args.args[0], captured.getvalue()

    def test_down_without_a_name_finds_the_projects_guest_on_another_host(self):
        nested = self.make_repo()
        listing = {"this machine": [], "mini": [{
            "name": "rv-sample-999988887777", "backend": "container", "phase": "ready",
            "projects": "sample-project:main", "checkouts": [{"id": "sample-project", "name": "sample project"}]}]}
        with patch.object(rox, "gather_records", return_value=(listing, {"offline": "timed out"})), \
                patch.object(rox, "down_remote") as removed, contextlib.redirect_stderr(io.StringIO()) as warned:
            rox.main(["down", "--cwd", str(nested)])
        self.assertEqual((removed.call_args.args[0].name, removed.call_args.args[0].host),
                         ("rv-sample-999988887777", "mini"))
        self.assertIn("Could not check offline for workspaces", warned.getvalue())
        self.save_guest("rv-sample-000011112222", "sample project")
        with patch.object(rox, "gather_records", return_value=(listing, {})), \
                patch.object(rox.sys, "stdin", io.StringIO()), \
                self.assertRaisesRegex(rox.Error, r"(?s)rv-sample-000011112222  container.*rv-sample-999988887777  on mini"):
            rox.main(["down", "--cwd", str(nested)])

    def test_rex_host_labels_leave_out_guests_named_by_tailnet_name(self):
        hosts = {"hosts": [{"host": {"label": label}} for label in (
            "mini", "rv-web-0123456789ab", "rv-web-0123456789ab.tail1234.ts.net", "studio.tail1234.ts.net")]}
        with patch.object(rox, "output", return_value=json.dumps(hosts)):
            self.assertEqual(rox.rex_host_labels(), ["mini", "studio.tail1234.ts.net"])

    def test_down_without_a_name_removes_the_projects_only_workspace(self):
        self.save_guest("rv-other-000011112222", "other project")
        self.save_guest("rv-sample-000011112222", "sample project")
        name, printed = self.down_without_name()
        self.assertEqual(name, "rv-sample-000011112222")
        self.assertEqual(printed, "")

    def test_down_without_a_name_asks_which_of_several_workspaces(self):
        self.save_guest("rv-other-000011112222", "other project")
        self.save_guest("rv-sample-000011112222", "sample project")
        self.save_guest("rv-sample-333344445555", "sample project")
        name, printed = self.down_without_name(answer="2\n")
        self.assertEqual(name, "rv-sample-333344445555")
        self.assertIn("sample project has 2 workspaces", printed)
        self.assertIn("1) rv-sample-000011112222", printed)
        self.assertNotIn("rv-other", printed)

    def test_down_without_a_name_lists_choices_when_it_cannot_ask(self):
        self.save_guest("rv-sample-000011112222", "sample project")
        self.save_guest("rv-sample-333344445555", "sample project")
        with self.assertRaisesRegex(rox.Error, r"(?s)rox down NAME.*rv-sample-333344445555"):
            self.down_without_name()
        with self.assertRaisesRegex(rox.Error, "No workspace selected"):
            self.down_without_name(answer="9\n")

    def test_down_without_a_name_reports_a_project_with_no_workspaces(self):
        self.save_guest("rv-other-000011112222", "other project")
        with self.assertRaisesRegex(rox.Error, "No workspaces for sample project"):
            self.down_without_name()

    @contextlib.contextmanager
    def fake_guests(self):
        """Stand in for the guest runtime, recording guest stages and host commands."""
        record = {"stages": [], "commands": [], "sessions": iter(f"session:{n}" for n in range(1, 99))}

        def stage(guest, name, *args, agent=None, capture=False):
            record["stages"].append((name, *args))
            if name == "prepare":
                return f"/home/guest/.rox/{guest.name}\n/home/guest/.rox/{guest.name}/r.sock"
            if name == "session":
                return json.dumps({"session_id": next(record["sessions"])})
            return None

        real_run = rox.run
        real_output = rox.output

        def command(argv, capture=False, check=True, **kwargs):
            if argv[0] == "git":
                return real_run(argv, capture=capture, check=check, **kwargs)
            record["commands"].append(argv)
            stdout = base_image() if argv[1:3] == ["image", "inspect"] else ""
            return subprocess.CompletedProcess(argv, 0, stdout, "")

        def probe(argv, **kwargs):
            return real_output(argv, **kwargs) if argv[0] == "git" else "{}"

        with (
            patch.object(rox.Guest, "runtime", property(lambda guest: self.root / "runtime" / guest.name)),
            patch.object(rox, "run", side_effect=command),
            patch.object(rox, "prepare_project_image", return_value="") as prepare,
            patch.object(rox, "guest_stage", side_effect=stage),
            patch.object(rox, "check_guest_os"),
            patch.object(rox, "launch_guest", return_value=("127.0.0.1", 22)),
            patch.object(rox, "container_address", return_value=("127.0.0.1", 22)),
            patch.object(rox, "open_tunnel"),
            patch.object(rox, "output", side_effect=probe),
            patch.object(rox, "git_host_entries", return_value=""),
            patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "no-config")}),
            patch.object(rox.shutil, "which", return_value="/fixture"),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            record["prepare"] = prepare
            yield record

    def guests(self):
        return [rox.Guest.load(path.parent.name) for path in sorted(rox.state_root().glob("*/guest.json"))]

    def test_up_outside_a_checkout_starts_a_guest_without_a_project(self):
        outside = self.root / "not a checkout"
        outside.mkdir()
        printed = io.StringIO()
        with self.fake_guests() as record, contextlib.redirect_stdout(printed):
            rox.main(["up", "container", "--cwd", str(outside), "--", "agent"])
        [guest] = self.guests()
        self.assertRegex(guest.name, r"^rv-host-[a-f0-9]{12}$")
        self.assertEqual((guest.phase, guest.projects, guest.session_id), ("ready", [], "session:1"))
        record["prepare"].assert_not_called()
        self.assertEqual([stage[0] for stage in record["stages"]], ["prepare", "server", "session"])
        session = record["stages"][-1]
        self.assertEqual(session[session.index("-c") + 1], "/home/guest")
        self.assertEqual(session[-3:], ("--keep-open", "--", "agent"))
        self.assertIn("without a project", printed.getvalue())

    def test_no_project_starts_an_empty_guest_from_inside_a_checkout(self):
        with self.fake_guests(), contextlib.redirect_stdout(io.StringIO()):
            rox.main(["up", "container", "--no-project", "--cwd", str(self.make_repo())])
        [guest] = self.guests()
        self.assertEqual(guest.projects, [])
        with self.assertRaisesRegex(rox.Error, "--no-project can't be combined"):
            rox.main(["up", "container", "--no-project", "--branch", "main"])
        with self.assertRaisesRegex(rox.Error, "--rebuild needs a project image"):
            rox.main(["up", "container", "--no-project", "--rebuild"])

    def test_outside_a_checkout_a_repo_and_branch_name_the_project(self):
        args = rox.argparse.Namespace(no_project=False, project=None, remote=None, cwd=self.root / "missing",
                                      repo="git@git.example.test:team/web-app.git", branch="main")
        self.assertEqual(rox.resolve_project(args), ("web-app", args.repo, "main"))
        args.branch = None
        with self.assertRaisesRegex(rox.Error, "not inside a Git checkout"):
            rox.resolve_project(args)

    def test_add_clones_projects_into_a_running_guest(self):
        repo = self.make_repo()
        with self.fake_guests() as record, contextlib.redirect_stdout(io.StringIO()):
            rox.main(["up", "container", "--no-project", "--cwd", str(self.root)])
            [guest] = self.guests()
            record["stages"].clear()
            rox.main(["add", guest.name, "--cwd", str(repo), "--no-hook", "--", "agent"])
            rox.main(["add", guest.name, "--project", "other", "--repo", "https://git.example.test/other.git",
                      "--branch", "main"])
            rox.main(["add", guest.name, "--cwd", str(repo)])
        guest = rox.Guest.load(guest.name)
        base = f"/home/guest/.rox/{guest.name}"
        self.assertEqual([(p.id, p.name, p.branch, p.workspace, p.session_id, p.phase) for p in guest.projects], [
            ("sample-project", "sample project", "published-feature", f"{base}/projects/sample-project",
             "session:2", "ready"),
            ("other", "other", "main", f"{base}/projects/other", "session:3", "ready"),
            ("sample-project-2", "sample project", "published-feature", f"{base}/projects/sample-project-2",
             "session:4", "ready"),
        ])
        clones = [stage for stage in record["stages"] if stage[0] == "clone"]
        self.assertEqual([clone[1:4] for clone in clones], [
            (f"{base}/projects/sample-project", "git@git.example.test:fork/repo.git", "published-feature"),
            (f"{base}/projects/other", "https://git.example.test/other.git", "main"),
            (f"{base}/projects/sample-project-2", "git@git.example.test:fork/repo.git", "published-feature"),
        ])
        setups = [stage[-1] for stage in record["stages"] if stage[0] == "setup"]
        self.assertEqual(setups, ["true", "false", "false"])
        # Adding reuses the guest's Rex server and keeps its single host registration.
        self.assertEqual(sum(argv[:3] == ["rex", "hosts", "add"] for argv in record["commands"]), 1)
        self.assertEqual(guest.session_id, "session:1")

        listed = io.StringIO()
        with contextlib.redirect_stdout(listed):
            rox.main(["ls"])
        self.assertIn("sample-project:published-feature, other:main, sample-project-2:published-feature",
                      listed.getvalue())
        self.assertEqual(self.down_without_name()[0], guest.name)

    def test_add_marks_a_failed_project_and_keeps_the_guest_ready(self):
        with self.fake_guests() as record, contextlib.redirect_stdout(io.StringIO()):
            rox.main(["up", "container", "--no-project", "--cwd", str(self.root)])
            [guest] = self.guests()
            with patch.object(rox, "git_agent", side_effect=rox.Error("clone fixture")):
                with self.assertRaisesRegex(rox.Error, "clone fixture"):
                    rox.main(["add", guest.name, "--cwd", str(self.make_repo())])
        guest = rox.Guest.load(guest.name)
        self.assertEqual(guest.phase, "ready")
        self.assertEqual([(p.id, p.phase) for p in guest.projects], [("sample-project", "failed")])

    def test_add_needs_a_ready_guest_and_a_project(self):
        guest = self.save_guest("rv-host-000011112222", "unused")
        guest.projects = []
        guest.save()
        with self.assertRaisesRegex(rox.Error, "not inside a Git checkout"):
            rox.main(["add", guest.name, "--cwd", str(self.root)])
        with patch.object(rox.shutil, "which", return_value="/fixture"):
            with self.assertRaisesRegex(rox.Error, "is starting. Run rox reconnect"):
                rox.main(["add", guest.name, "--cwd", str(self.make_repo())])
        with self.assertRaisesRegex(rox.Error, "Invalid workspace name"):
            rox.main(["add", "../escape", "--cwd", str(self.make_repo())])

    def test_remote_add_dispatches_with_the_resolved_project(self):
        real_run = rox.run
        requests = []

        def command(argv, **kwargs):
            if argv[0] == "git":
                return real_run(argv, **kwargs)
            requests.append(argv)
            if argv[4] == "new":
                return subprocess.CompletedProcess(argv, 0, json.dumps({
                    "session_id": "session:launch",
                    "initial_windows": [{"block_ids": ["block:provision"]}],
                }), "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.object(rox, "run", side_effect=command), contextlib.redirect_stdout(io.StringIO()):
            rox.main(["add", "rv-host-000011112222", "-S", "buildbox", "--cwd", str(self.make_repo()),
                      "--git-key", "~/.ssh/deploy", "--", "agent"])
            rox.main(["up", "linux", "-S", "buildbox", "--cwd", str(self.root)])
        launch = requests[0]
        self.assertEqual(launch[launch.index("rox sample project"):], [
            "rox sample project", "--", *rox.REMOTE_ROX, "add", "rv-host-000011112222", "--project=sample project",
            "--repo=git@git.example.test:fork/repo.git", "--branch=published-feature",
            "--git-key=~/.ssh/deploy", "--", "agent",
        ])
        # add's new, wait, capture and close come first.
        self.assertEqual(requests[3][-2:], ["kill", "session:launch"])
        self.assertEqual(requests[4][requests[4].index("--") + 1:][:8],
                         [*rox.REMOTE_ROX, "up", "linux", "--no-project", "--boot-timeout=120"])

    def test_shell_opens_the_only_project_a_named_one_or_the_guest_home(self):
        guest = rox.Guest("rv-host-000011112222", "container", "image", "workspace", phase="ready",
                          address="127.0.0.1", guest_socket="/home/workspace/.rox/rv-host-000011112222/r.sock")
        first = guest.add_project("web", "https://git.example.test/web.git", "main")
        first.workspace = "/checkouts/web"

        def directory(*argv):
            with patch.object(rox.Guest, "load", return_value=guest), \
                    patch.object(rox.os, "execvpe") as exec_run, contextlib.redirect_stderr(io.StringIO()):
                rox.main(["shell", guest.name, *argv])
            return shlex.split(exec_run.call_args.args[1][-1])[-1]

        self.assertEqual(directory(), "/checkouts/web")
        guest.add_project("api", "https://git.example.test/api.git", "main").workspace = "/checkouts/api"
        self.assertEqual(directory(), "/home/workspace")
        self.assertEqual(directory("api"), "/checkouts/api")
        with self.assertRaisesRegex(rox.Error, "'cli' is not a project.*web, api"):
            directory("cli")

    def test_ls_lists_the_ip_of_guests_that_have_their_own_address(self):
        rox.private_directory(rox.state_root())
        for name, backend, engine, address in (
            ("rv-apple-000000000001", "container", "container", "192.168.64.7"),
            ("rv-docker-000000000002", "container", "docker", "127.0.0.1"),
            ("rv-tart-000000000003", "linux", "", "192.168.65.4"),
            ("rv-booting-000000000004", "linux", "", ""),
        ):
            guest = rox.Guest(name, backend, "image", "workspace", engine, address=address, phase="ready")
            guest.directory.mkdir(mode=0o700)
            guest.save()
        listed = io.StringIO()
        with contextlib.redirect_stdout(listed):
            rox.main(["ls"])
        rows = {line.split("\t")[0]: line.split("\t")[3] for line in listed.getvalue().splitlines()}
        self.assertEqual(rows, {
            "rv-apple-000000000001": "192.168.64.7", "rv-docker-000000000002": "-",
            "rv-tart-000000000003": "192.168.65.4", "rv-booting-000000000004": "-",
        })

    def test_skill_install_copies_the_bundled_skill_for_each_agent_set_up(self):
        claude, codex = self.root / "claude", self.root / "codex"
        claude.mkdir()
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude), "CODEX_HOME": str(codex)}), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            rox.main(["skill", "install"])
            self.assertEqual((claude / "skills/rox/SKILL.md").read_text(),
                             (ROOT / "skills/rox/SKILL.md").read_text())
            self.assertTrue((claude / "skills/rox/references/remote-launcher.md").is_file())
            self.assertFalse(codex.exists())
            self.assertIn("for Claude Code", printed.getvalue())
            # Reinstalling replaces an older copy, including files the new one dropped.
            (claude / "skills/rox/stale.md").write_text("old\n")
            rox.main(["skill", "install", "--agent", "codex", "--agent", "claude"])
            self.assertFalse((claude / "skills/rox/stale.md").exists())
            self.assertTrue((codex / "skills/rox/SKILL.md").is_file())
            self.assertEqual(sorted(path.name for path in (claude / "skills").iterdir()), ["rox"])

    def test_skill_install_protects_folders_that_are_not_the_rox_skill(self):
        target = self.root / "project-skills"
        (target / "rox").mkdir(parents=True)
        (target / "rox" / "notes.md").write_text("mine\n")
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(rox.Error, "isn't a rox skill"):
                rox.main(["skill", "install", "--dir", str(target)])
            self.assertTrue((target / "rox" / "notes.md").is_file())
            rox.main(["skill", "install", "--dir", str(target), "--force"])
        self.assertTrue((target / "rox" / "SKILL.md").is_file())
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "none-a"),
                                     "CODEX_HOME": str(self.root / "none-b")}):
            with self.assertRaisesRegex(rox.Error, "Found no Claude Code or Codex folder"):
                rox.main(["skill", "install"])

    def tailnet_guest(self):
        guest = rox.Guest("rv-fixture-000011112222", "container", "rox:linux", "workspace", engine="container",
                          identity=str(self.root / "login"), address="192.168.64.9",
                          guest_socket="/home/workspace/.rox/rv-fixture-000011112222/r.sock")
        guest.directory.mkdir(parents=True, mode=0o700)
        return guest

    def auth_key(self, mode=0o600):
        key = self.root / "ts-authkey"
        key.write_text("tskey-auth-secret\n")
        key.chmod(mode)
        return key

    def test_tailscale_settings_are_validated(self):
        config_path = self.root / "config.json"
        self.assertIsNone(rox.tailscale_settings({}, config_path, False))
        key = self.auth_key()
        settings = {"tailscale": {"auth_key_file": str(key), "tags": ["tag:rox-guest"]}}
        self.assertIsNone(rox.tailscale_settings(settings, config_path, True))
        self.assertEqual(rox.tailscale_settings(settings, config_path, False),
                         {"key": key, "tags": ["tag:rox-guest"], "control_url": ""})
        for tailscale, message in (
            ({}, "auth_key_file"),
            ({"auth_key_file": str(key), "tags": ["rox-guest"]}, "list of tags"),
            ({"auth_key_file": str(key), "control_url": "http://hs.test"}, "https:// URL"),
            ({"auth_key_file": str(key), "authkey": "x"}, "Unknown tailscale setting authkey"),
        ):
            with self.subTest(tailscale=tailscale), self.assertRaisesRegex(rox.Error, message):
                rox.tailscale_settings({"tailscale": tailscale}, config_path, False)
        key.chmod(0o644)
        with self.assertRaisesRegex(rox.Error, "chmod 600"):
            rox.tailscale_settings(settings, config_path, False)

    def test_tailnet_enrollment_sends_the_key_on_stdin_and_registers_both_endpoints(self):
        guest = self.tailnet_guest()
        settings = {"key": self.auth_key(), "tags": ["tag:rox-guest"], "control_url": ""}
        # Tailscale reports needs_login briefly while it processes the key, then issues the certificate.
        calls = []
        states = iter([("starting", "pending"), ("needs_login", "pending"), ("running", "pending"),
                       ("running", "pending"), ("running", "available")])

        def fake_output(argv, **kwargs):
            calls.append((argv, kwargs.get("input")))
            if "status" in argv:
                state, https = next(states)
                return json.dumps({"tailscale": {"state": state, "https": {"state": https},
                                                 "dns_name": "rv-fixture-000011112222.tail.ts.net."}})
            return "{}"

        commands = []
        with (
            patch.object(rox, "output", side_effect=fake_output),
            patch.object(rox, "run", side_effect=lambda argv, **kwargs: commands.append(argv)),
            patch.object(rox.time, "sleep"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            rox.enable_tailnet(guest, settings, 10)
            rox.register_guest(guest)
        on_argv, key_input = calls[0]
        remote = shlex.split(on_argv[-1])
        self.assertEqual(key_input, "tskey-auth-secret")
        self.assertFalse(any("tskey" in arg for arg in on_argv))
        self.assertEqual(remote[remote.index("--hostname") + 1], guest.name)
        self.assertEqual(remote[remote.index("--advertise-tag") + 1], "tag:rox-guest")
        self.assertIn("--allow-tailnet-members", remote)
        self.assertEqual(remote[-2:], ["--auth-key-file", "-"])
        self.assertEqual(guest.tailnet_name, "rv-fixture-000011112222.tail.ts.net")
        self.assertEqual(sum("status" in argv for argv, _ in calls), 5)
        self.assertEqual(commands, [["rex", "hosts", "add", guest.name, guest.endpoint, guest.tailnet_name]])

    def test_untagged_nodes_stay_owner_only_and_a_rejected_key_fails_clearly(self):
        guest = self.tailnet_guest()
        settings = {"key": self.auth_key(), "tags": [], "control_url": ""}
        calls = []

        def fake_output(argv, **kwargs):
            calls.append(argv)
            if "status" in argv:
                return json.dumps({"tailscale": {"state": "needs_login", "auth_url": "https://login.test/a"}})
            return "{}"

        clock = iter(range(0, 1000, 10))
        with (
            patch.object(rox, "output", side_effect=fake_output),
            patch.object(rox.time, "monotonic", side_effect=lambda: next(clock)),
            patch.object(rox.time, "sleep"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaisesRegex(rox.Error, "auth key was rejected"):
                rox.enable_tailnet(guest, settings, 30)
        self.assertNotIn("--allow-tailnet-members", shlex.split(calls[0][-1]))

    def test_remote_launch_registers_the_guest_here_by_its_tailnet_name(self):
        provisioned = ("Starting rv-host-000011112222\nWorkspace  rv-host-000011112222\n"
                       "Tailnet    rv-host-000011112222.tail1234.ts.net\nSession    session:1\n")
        commands = []
        with (
            patch.object(rox, "dispatch", return_value=provisioned),
            patch.object(rox, "run", side_effect=lambda argv, **kwargs: commands.append(argv) or
                         subprocess.CompletedProcess(argv, 0, "", "")),
            contextlib.redirect_stdout(io.StringIO()) as printed,
        ):
            rox.main(["up", "container", "--no-project", "--host", "mini"])
        self.assertEqual(commands, [["rex", "hosts", "add", "rv-host-000011112222",
                                     "rv-host-000011112222.tail1234.ts.net"]])
        self.assertIn("on this machine at rv-host-000011112222.tail1234.ts.net", printed.getvalue())

    def test_remote_launch_without_a_tailnet_says_the_guest_stays_on_the_launcher(self):
        with (
            patch.object(rox, "dispatch", return_value="Workspace  rv-host-000011112222\n"),
            patch.object(rox, "run") as command,
            contextlib.redirect_stdout(io.StringIO()) as printed,
        ):
            rox.main(["up", "container", "--no-project", "--host", "mini"])
        command.assert_not_called()
        self.assertIn("reachable only on mini", printed.getvalue())

    def test_down_on_a_remote_host_removes_the_guest_there_and_its_host_here(self):
        commands = []
        with (
            patch.object(rox, "dispatch", return_value="Removed rv-host-000011112222\n") as dispatch,
            patch.object(rox, "run", side_effect=lambda argv, **kwargs: commands.append(argv) or
                         subprocess.CompletedProcess(argv, 3, "", "")),
        ):
            rox.main(["down", "rv-host-000011112222", "--host", "mini"])
        self.assertEqual(dispatch.call_args.args[2], ["down", "rv-host-000011112222"])
        self.assertEqual(commands, [["rex", "hosts", "remove", "rv-host-000011112222"]])
        with self.assertRaisesRegex(rox.Error, "Name the workspace"):
            rox.main(["down", "--host", "mini"])

    def test_down_on_a_remote_host_dispatches_only_the_down_command(self):
        requests = []

        def fake_output(argv, **kwargs):
            requests.append(argv)
            return json.dumps({"session_id": "session:down", "initial_windows": [{"block_ids": ["block:down"]}]})

        with (
            patch.object(rox, "output", side_effect=fake_output),
            patch.object(rox, "run", side_effect=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", "")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            rox.main(["down", "rv-host-000011112222", "-S", "mini"])
        self.assertEqual(requests[0][requests[0].index("--") + 1:], [*rox.REMOTE_ROX, "down", "rv-host-000011112222"])

    def test_down_closes_guest_tabs_and_a_session_left_empty(self):
        guest = self.saved_guest(shell_windows=[{"session": "session:provision", "window": "window:guest"}])
        commands = []

        def fake_run(argv, **kwargs):
            commands.append(argv)
            stdout = json.dumps({"windows": []}) if "ls" in argv else ""
            return subprocess.CompletedProcess(argv, 0, stdout, "")

        with patch.object(rox, "run", side_effect=fake_run):
            rox.close_shell_windows(guest)
        rex = ["rex", "--autostart=false", "--timeout", "5s"]
        self.assertEqual(commands, [[*rex, "window", "close", "-s", "session:provision", "window:guest"],
                                    [*rex, "window", "ls", "-s", "session:provision", "--json"],
                                    [*rex, "kill", "session:provision"]])

    def test_removing_a_rex_host_that_is_already_gone_succeeds(self):
        for code, stderr, removed in ((0, "", True), (3, "", True),
                                      (1, 'Error: no host has that label: "rv-x"', True), (1, "permission denied", False)):
            with self.subTest(code=code, stderr=stderr), patch.object(
                    rox, "run", return_value=subprocess.CompletedProcess([], code, "", stderr)):
                self.assertEqual(rox.remove_rex_host("rv-x"), removed)

    def test_remote_provisioning_closes_its_session_on_success_and_keeps_it_on_failure(self):
        args = type("Args", (), {"host": "mini"})()
        for status in (0, 1):
            commands = []

            def fake_run(argv, **kwargs):
                commands.append(argv)
                return subprocess.CompletedProcess(argv, status if "wait" in argv else 0, "done\n", "")

            created = json.dumps({"session_id": "session:p", "initial_windows": [{"block_ids": ["block:p"]}]})
            with self.subTest(status=status), patch.object(rox, "output", return_value=created), \
                    patch.object(rox, "run", side_effect=fake_run), contextlib.redirect_stdout(io.StringIO()):
                if status == 0:
                    rox.dispatch(args, "label", ["down", "x"], [])
                    self.assertEqual(commands[-1][-2:], ["kill", "session:p"])
                else:
                    with self.assertRaisesRegex(rox.Error, "See the Provision tab in session:p on mini"):
                        rox.dispatch(args, "label", ["up"], [])
                    self.assertFalse(any("kill" in argv for argv in commands))


    def test_down_signs_the_guest_out_of_the_tailnet(self):
        guest = self.tailnet_guest()
        guest.tailnet_name = "rv-fixture-000011112222.tail.ts.net"
        guest.phase = "ready"
        guest.save()
        commands = []

        def command(argv, **kwargs):
            commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.object(rox, "run", side_effect=command), contextlib.redirect_stdout(io.StringIO()):
            rox.main(["down", guest.name])
        logout = [argv for argv in commands if argv[0] == "ssh"]
        self.assertEqual(shlex.split(logout[0][-1])[-3:], ["server", "tailscale", "logout"])
        self.assertIn(["container", "delete", "--force", guest.name], commands)

    def test_project_config_cannot_put_guests_on_the_tailnet(self):
        self.assertIn("tailscale", rox.LAUNCHER_KEYS)

    def fake_rex_hosts(self, servers, listings, killed):
        """Fake rex for rox ls --all: server IDs per host, and what rox ls prints there."""
        hosts = {"hosts": [{"host": {"label": label}} for label in [*servers, "rv-guest-000011112222"]]}
        sessions = {}

        def fake_output(argv, **kwargs):
            if argv[:3] == ["rex", "hosts", "list"]:
                return json.dumps(hosts)
            target = argv[argv.index("-S") + 1] if "-S" in argv else None
            if argv[-3:] == ["server", "status", "--json"]:
                if servers.get(target, "here") is None:
                    raise rox.Error("connection refused")
                return json.dumps({"host_id": servers.get(target, "here") if target else "here"})
            if "new" in argv:
                self.assertIn("--keep-open", argv)
                sessions[target] = f"session:{target}"
                return json.dumps({"session_id": f"session:{target}", "initial_windows": [{"block_ids": ["block:1"]}]})
            if "capture" in argv:
                return listings[target]
            raise AssertionError(argv)

        def fake_run(argv, **kwargs):
            if "kill" in argv:
                killed.append(argv[-1])
            return subprocess.CompletedProcess(argv, 0, "", "")

        return fake_output, fake_run

    def test_ls_all_lists_guests_on_every_reachable_launcher_once(self):
        rox.private_directory(rox.state_root())
        local = rox.Guest("rv-local-000000000001", "container", "rox:linux", "workspace", "container",
                          address="192.168.64.5", phase="ready")
        local.directory.mkdir(mode=0o700)
        local.save()
        mini = [{"name": "rv-mini-000000000002", "backend": "container", "engine": "container", "phase": "ready",
                 "ip": "192.168.64.8", "tailnet": "rv-mini-000000000002.tail.ts.net", "projects": "app:main"}]
        listings = {
            "mini": json.dumps(mini, indent=2),
            "mini-alias": json.dumps(mini, indent=2),
            # An older rox without --json falls back to its plain listing.
            "studio": "#rox-plain\nrv-old-000000000003    linux    failed    -    api:dev (failed)\n",
        }
        servers = {"air": "here", "mini": "mini-id", "mini-alias": "mini-id-2", "studio": "studio-id", "gone": None}
        killed = []
        fake_output, fake_run = self.fake_rex_hosts(servers, listings, killed)
        with (
            patch.object(rox, "output", side_effect=fake_output),
            patch.object(rox, "run", side_effect=fake_run),
            contextlib.redirect_stdout(io.StringIO()) as printed,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            rox.main(["ls", "--all"])
        rows = [line.split("\t") for line in printed.getvalue().splitlines()]
        self.assertEqual(rows[0], ["HOST", "NAME", "BACKEND", "PHASE", "IP", "PROJECTS"])
        self.assertEqual([(row[0], row[1]) for row in rows[1:]], [
            ("this machine", "rv-local-000000000001"),
            ("mini", "rv-mini-000000000002"),
            ("studio", "rv-old-000000000003"),
        ])
        self.assertEqual(rows[3][3:], ["failed", "-", "api:dev (failed)"])
        self.assertIn("Skipped gone: connection refused", errors.getvalue())
        # Guests aren't launchers, and this machine's own server isn't asked twice.
        self.assertEqual(sorted(killed), ["session:mini", "session:mini-alias", "session:studio"])

    def test_ls_on_one_host_reports_a_missing_rox_and_prints_json(self):
        listings = {"mini": json.dumps([{"name": "rv-mini-000000000002", "backend": "container",
                                         "phase": "ready", "ip": "-", "projects": "-"}])}
        killed = []
        fake_output, fake_run = self.fake_rex_hosts({"mini": "mini-id"}, listings, killed)
        with (
            patch.object(rox, "output", side_effect=fake_output),
            patch.object(rox, "run", side_effect=fake_run),
            contextlib.redirect_stdout(io.StringIO()) as printed,
        ):
            rox.main(["ls", "--host", "mini", "--json"])
        self.assertEqual([(entry["host"], entry["name"]) for entry in json.loads(printed.getvalue())],
                         [("mini", "rv-mini-000000000002")])
        listings["mini"] = "sh: rox: not found\n"
        with (
            patch.object(rox, "output", side_effect=fake_output),
            patch.object(rox, "run", side_effect=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 127, "", "")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaisesRegex(rox.Error, "isn't installed there"):
                rox.main(["ls", "--host", "mini"])

    def test_records_from_single_project_launches_still_load(self):
        name = "rv-legacy-000011112222"
        directory = rox.state_root() / name
        directory.mkdir(parents=True)
        (directory / "guest.json").write_text(json.dumps({
            "name": name, "backend": "linux", "image": "rox-ubuntu", "project": "My App", "branch": "main",
            "user": "admin", "workspace": "/Users/admin/.rox/rv-legacy-000011112222/project",
            "guest_socket": "/Users/admin/.rox/rv-legacy-000011112222/r.sock",
            "session_id": "session:old", "phase": "ready", "registered": True,
        }))
        guest = rox.Guest.load(name)
        self.assertEqual(guest.session_id, "")
        self.assertEqual(guest.projects, [rox.Project(
            "my-app", "My App", "", "main", "/Users/admin/.rox/rv-legacy-000011112222/project",
            "session:old", "ready")])
        self.assertEqual(str(guest.home), "/Users/admin")
        guest.save()
        self.assertEqual(rox.Guest.load(name), guest)

    def agent_config(self, agents):
        path = self.root / "agents-config.json"
        path.write_text(json.dumps({"agents": agents}))
        return path

    def secret(self, name, value, mode=0o600):
        path = self.root / name
        path.write_text(value + "\n")
        path.chmod(mode)
        return path

    def test_agent_settings_are_validated(self):
        token = self.secret("token", "sk-ant-oat01-x")
        self.assertEqual(rox.agent_settings({}, "c"), {})
        self.assertEqual(rox.agent_settings({"agents": {"claude": {"token_file": str(token)}, "codex": {}}}, "c"),
                         {"claude": {"version": "latest", "token_file": token}, "codex": {"version": "latest"}})
        self.assertEqual(rox.agent_settings({"agents": {"codex": {"version": "0.171.0"}}}, "c"),
                         {"codex": {"version": "0.171.0"}})
        loose = self.secret("loose", "x", 0o644)
        for agents, message in (
            ([], "Expected an object for agents"),
            ({"gemini": {}}, "Unknown agent gemini"),
            ({"claude": "latest"}, "Expected an object for agents.claude"),
            ({"claude": {"auth_file": str(token)}}, "Unknown setting agents.claude.auth_file"),
            ({"claude": {"version": "v2"}}, "Expected latest or a version"),
            ({"claude": {"token_file": str(loose)}}, "chmod 600"),
            ({"claude": {"token_file": str(self.root / "missing")}}, "File does not exist"),
            ({"claude": {"token_file": str(token), "api_key_file": str(token)}}, "Choose one of"),
        ):
            with self.subTest(agents=agents), self.assertRaisesRegex(rox.Error, message):
                rox.agent_settings({"agents": agents}, "c")

    def test_agent_versions_check_latest_every_few_hours_and_never_block(self):
        rox.private_directory(rox.state_root())
        agents = {"claude": {"version": "latest"}, "codex": {"version": "0.1.0"}}
        with patch.object(rox, "latest_agent_version", return_value="2.0.0") as latest:
            self.assertEqual(rox.agent_versions(agents, now=1000), {"claude": "2.0.0", "codex": "0.1.0"})
            latest.return_value = "2.0.1"
            self.assertEqual(rox.agent_versions(agents, now=2000)["claude"], "2.0.0")
            self.assertEqual(rox.agent_versions(agents, now=1000 + rox.AGENT_VERSION_TTL)["claude"], "2.0.1")
            latest.return_value = "2.0.2"
            self.assertEqual(rox.agent_versions(agents, refresh=True, now=1001 + rox.AGENT_VERSION_TTL)["claude"],
                             "2.0.2")
            self.assertEqual([call.args for call in latest.call_args_list], [("claude",)] * 3)
        self.assertEqual((rox.state_root() / "agent-versions.json").stat().st_mode & 0o777, 0o600)
        with patch.object(rox, "latest_agent_version", side_effect=OSError("offline")), \
                contextlib.redirect_stderr(io.StringIO()) as warned:
            self.assertEqual(rox.agent_versions(agents, refresh=True)["claude"], "2.0.2")
            (rox.state_root() / "agent-versions.json").unlink()
            self.assertEqual(rox.agent_versions(agents)["claude"], "latest")
        self.assertIn("Could not check for a newer Claude Code (offline)", warned.getvalue())

    def test_latest_agent_versions_come_from_each_release_feed(self):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        bodies = {rox.AGENT_LATEST["claude"]: b"2.1.330\n",
                  rox.AGENT_LATEST["codex"]: json.dumps({"name": "@openai/codex", "version": "0.171.0"}).encode()}
        with patch.object(rox.urllib.request, "urlopen", side_effect=lambda request, timeout: Response(bodies[request.full_url])):
            self.assertEqual(rox.latest_agent_version("claude"), "2.1.330")
            self.assertEqual(rox.latest_agent_version("codex"), "0.171.0")
        bodies[rox.AGENT_LATEST["claude"]] = b"<html>error</html>"
        with patch.object(rox.urllib.request, "urlopen", side_effect=lambda request, timeout: Response(bodies[request.full_url])), \
                self.assertRaisesRegex(ValueError, "unexpected version"):
            rox.latest_agent_version("claude")

    def test_agent_layer_rebuilds_only_for_new_versions_or_a_new_base(self):
        rox.private_directory(rox.state_root())
        images = {"rox:web": ("sha256:project", {})}
        builds = []

        def command(argv, **kwargs):
            if argv[1:3] == ["image", "inspect"]:
                if argv[3] not in images:
                    return subprocess.CompletedProcess(argv, 1, "", "missing")
                image_id, labels = images[argv[3]]
                return subprocess.CompletedProcess(argv, 0, json.dumps([{"Id": image_id, "Config": {"Labels": labels}}]), "")
            if argv[1] == "build":
                containerfile = (Path(argv[-1]) / "Containerfile").read_text()
                self.assertIn("FROM rox:web\n", containerfile)
                self.assertIn('"agents", "claude=2.0.0", "codex=0.1.0"]', containerfile)
                self.assertTrue((Path(argv[-1]) / "bootstrap.sh").is_file())
                labels = dict(arg.removeprefix("--label=").split("=", 1) for arg in argv if arg.startswith("--label="))
                builds.append(labels)
                images[argv[argv.index("--tag") + 1]] = (f"sha256:agents{len(builds)}", labels)
            return subprocess.CompletedProcess(argv, 0, "", "")

        versions = {"codex": "0.1.0", "claude": "2.0.0"}
        with patch.object(rox, "run", side_effect=command), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(rox.prepare_agent_image("podman", "rox:web", versions), "rox:web-agents")
            self.assertEqual(builds, [{"io.rox.agents": "claude=2.0.0 codex=0.1.0", "io.rox.agents-base": "sha256:project"}])
            rox.prepare_agent_image("podman", "rox:web", versions)
            self.assertEqual(len(builds), 1)
            images["rox:web"] = ("sha256:project2", {})
            rox.prepare_agent_image("podman", "rox:web", versions)
            self.assertEqual(len(builds), 2)
        self.assertEqual(rox.agent_image_name("ghcr.io/acme/dev:1"), "rox:ghcr.io-acme-dev-1-agents")

        def failing(argv, **kwargs):
            if argv[1] == "build":
                raise subprocess.CalledProcessError(1, argv)
            return command(argv, **kwargs)

        with patch.object(rox, "run", side_effect=failing), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as warned:
            self.assertEqual(rox.prepare_agent_image("podman", "rox:web", {"claude": "3.0.0"}), "rox:web")
        self.assertIn("installs them when it boots instead", warned.getvalue())

    def test_agent_credentials_go_to_the_guest_on_stdin(self):
        token = self.secret("token", "sk-ant-oat01-it's")
        auth = self.secret("auth.json", '{"tokens": {}}')
        key = self.secret("openai", "sk-openai")
        guest = self.saved_guest()
        sent = []

        def fake_output(argv, input=None, **kwargs):
            sent.append((argv[-1], input))
            return ""

        with patch.object(rox, "output", side_effect=fake_output):
            rox.push_agent_credentials(guest, {"claude": {"token_file": token}, "codex": {"auth_file": auth}})
            rox.push_agent_credentials(guest, {"codex": {"api_key_file": key}})
        env = "# Written by rox from the launcher's config. Rox replaces this file.\n"
        self.assertEqual(sent, [
            (shlex.join(["bash", "-c", rox.AGENT_ENV_SCRIPT]),
             env + "export CLAUDE_CODE_OAUTH_TOKEN='sk-ant-oat01-it'\"'\"'s'\n"),
            (shlex.join(["bash", "-c", rox.CODEX_AUTH_SCRIPT]), '{"tokens": {}}\n'),
            (shlex.join(["bash", "-c", rox.AGENT_ENV_SCRIPT]), env),
            (shlex.join(["bash", "-c", rox.CODEX_LOGIN_SCRIPT]), "sk-openai\n"),
        ])
        for argv, _ in sent:
            self.assertNotIn("sk-", argv)

    def test_bootstrap_agents_stage_skips_installed_versions_and_completes_onboarding(self):
        home = self.root / "home"
        bin_dir = home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        for name, text in (("claude", "2.1.0 (Claude Code)"), ("codex", "codex-cli 0.9.0"), ("curl", "")):
            tool = bin_dir / name
            tool.write_text(f"#!/bin/sh\necho '{text}'\n" if name != "curl" else "#!/bin/sh\nexit 22\n")
            tool.chmod(0o755)
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}

        def stage(*specs):
            return subprocess.run(["bash", str(ROOT / "guest/bootstrap.sh"), "agents", *specs], env=env,
                                  capture_output=True, text=True)

        done = stage("claude=2.1.0", "codex=latest")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("claude 2.1.0 is installed", done.stderr)
        self.assertEqual(json.loads((home / ".claude.json").read_text()), {"hasCompletedOnboarding": True})
        (home / ".claude.json").write_text('{\n  "installMethod": "native"\n}\n')
        self.assertEqual(stage("claude=2.1.0").returncode, 0)
        self.assertEqual(json.loads((home / ".claude.json").read_text()),
                         {"hasCompletedOnboarding": True, "installMethod": "native"})
        line = '[ -f "$HOME/.rox/agents.env" ] && . "$HOME/.rox/agents.env"'
        for profile in (".profile", ".bashrc", ".zshenv"):
            self.assertEqual((home / profile).read_text().splitlines().count(line), 1)
        # A different version runs the installer, whose download fails here.
        failed = stage("codex=1.0.0")
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("Installing codex 1.0.0", failed.stderr)

    def test_bootstrap_server_reads_the_agents_credentials(self):
        script = (ROOT / "guest/bootstrap.sh").read_text()
        server = script[script.index("    server)"):script.index("    session)")]
        self.assertLess(server.index('. "$HOME/.rox/agents.env"'), server.index("nohup rex server run"))

    def test_up_installs_agents_before_the_rex_server_starts(self):
        token = self.secret("token", "sk-ant-oat01-x")
        config = self.agent_config({"claude": {"token_file": str(token)}})
        stages = []
        pushed = []

        def stage(guest, name, *args, agent=None, capture=False):
            stages.append((name, *args) if name == "agents" else name)
            if name == "prepare":
                return f"/home/workspace/.rox/{guest.name}\n/home/workspace/.rox/{guest.name}/r.sock"
            if name == "session":
                return '{"session_id":"session:fixture"}'
            return None

        launched = []
        with (
            patch.object(rox, "run", return_value=subprocess.CompletedProcess([], 0, "", "")),
            patch.object(rox, "prepare_container_image"),
            patch.object(rox, "agent_versions", return_value={"claude": "2.0.0"}),
            patch.object(rox, "prepare_agent_image", return_value="rox:linux-agents") as layer,
            patch.object(rox, "push_agent_credentials", side_effect=lambda guest, agents: pushed.append(agents)),
            patch.object(rox, "guest_stage", side_effect=stage),
            patch.object(rox, "check_guest_os"),
            patch.object(rox, "launch_guest", side_effect=lambda guest, timeout: launched.append(guest.image) or ("127.0.0.1", 22)),
            patch.object(rox, "attach_guest"),
            patch.object(rox.shutil, "which", return_value="/fixture"),
            patch.object(rox.Guest, "runtime", property(lambda guest: self.root / "runtime" / guest.name)),
            contextlib.redirect_stdout(io.StringIO()) as printed,
        ):
            rox.main(["up", "container", "--no-project", "--config", str(config)])
            rox.main(["up", "container", "--no-project", "--no-agents", "--config", str(config)])
        layer.assert_called_once_with("docker", "rox:linux", {"claude": "2.0.0"})
        self.assertEqual(launched, ["rox:linux-agents", "rox:linux"])
        self.assertEqual(stages, ["prepare", ("agents", "claude=2.0.0"), "server", "session",
                                  "prepare", "server", "session"])
        self.assertEqual(pushed, [{"claude": {"version": "latest", "token_file": token}}])
        self.assertIn("Agents     Claude Code 2.0.0", printed.getvalue())
        agents = sorted(rox.local_guests(), key=lambda guest: bool(guest.agents))
        self.assertEqual([guest.agents for guest in agents], [{}, {"claude": "2.0.0"}])

    def test_update_agents_refreshes_running_guests(self):
        config = self.agent_config({"codex": {}})
        self.saved_guest("rv-fixture-000011112222", agents={"codex": "0.1.0"})
        self.saved_guest("rv-stopped-000011113333", stopped=True)
        installed = []
        with (
            patch.object(rox, "agent_versions", return_value={"codex": "0.2.0"}) as versions,
            patch.object(rox, "guest_stage", side_effect=lambda guest, *args: installed.append((guest.name, *args))),
            patch.object(rox, "push_agent_credentials"),
            contextlib.redirect_stdout(io.StringIO()) as printed,
        ):
            rox.main(["update-agents", "--all", "--config", str(config)])
            rox.main(["update-agents", "rv-fixture-000011112222", "--config", str(config)])
        versions.assert_called_with({"codex": {"version": "latest"}}, refresh=True)
        self.assertEqual(installed, [("rv-fixture-000011112222", "agents", "codex=0.2.0")] * 2)
        self.assertEqual(rox.Guest.load("rv-fixture-000011112222").agents, {"codex": "0.2.0"})
        self.assertIn("has Codex 0.2.0 already", printed.getvalue())
        with self.assertRaisesRegex(rox.Error, "No agents are configured"):
            rox.main(["update-agents", "rv-fixture-000011112222", "--config", str(self.root / "none.json")])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            rox.main(["update-agents"])

    def test_ssh_clones_fall_back_to_the_default_ssh_keys(self):
        args = rox.argparse.Namespace(git_key=None, git_known_hosts="known")
        url = "git@github.com:acme/app.git"
        with patch.object(rox, "git_host_entries", return_value=""), patch.object(rox, "require"), \
                contextlib.redirect_stderr(io.StringIO()) as warned:
            self.assertEqual(rox.git_access(args, {}, "c", url)[0], [])
        self.assertIn("the guest probably can't clone git@github.com:acme/app.git", warned.getvalue())
        ssh = self.root / "home" / ".ssh"
        ssh.mkdir(parents=True)
        (ssh / "id_ed25519").write_text("key")
        (ssh / "id_rsa").write_text("key")
        with patch.object(rox, "git_host_entries", return_value=""), patch.object(rox, "require"), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(rox.git_access(args, {}, "c", url)[0], [ssh / "id_rsa", ssh / "id_ed25519"])
            self.assertEqual(rox.git_access(args, {"git_keys": []}, "c", url)[0], [])
            self.assertEqual(rox.git_access(args, {}, "c", "https://github.com/acme/app.git"), ([], ""))
        self.assertIn("Using your default SSH keys ~/.ssh/id_rsa, ~/.ssh/id_ed25519 for Git", printed.getvalue())

    def test_config_agents_saves_private_credentials(self):
        config = self.root / "config" / "config.json"
        codex_home = self.root / "codex-home"
        codex_home.mkdir()
        (codex_home / "auth.json").write_text("{}")
        (codex_home / "auth.json").chmod(0o600)

        def configure(*argv, stdin=""):
            with patch.object(rox.sys, "stdin", io.StringIO(stdin)), \
                    patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}), \
                    contextlib.redirect_stdout(io.StringIO()) as printed:
                rox.main(["config", "agents", *argv, "--config", str(config)])
            return printed.getvalue(), json.loads(config.read_text())

        _, saved = configure("claude", stdin="sk-ant-oat01-token\n")
        token = config.with_name("claude-token")
        self.assertEqual(saved, {"agents": {"claude": {"token_file": str(token)}}})
        self.assertEqual((token.read_text(), token.stat().st_mode & 0o777), ("sk-ant-oat01-token\n", 0o600))
        _, saved = configure("claude", "--version", "2.1.0", stdin="sk-ant-api03-key\n")
        self.assertEqual(saved["agents"]["claude"], {"version": "2.1.0",
                                                     "api_key_file": str(config.with_name("anthropic-api-key"))})
        self.assertFalse(token.exists())
        printed, saved = configure("codex")
        self.assertEqual(saved["agents"]["codex"], {"auth_file": str((codex_home / "auth.json").resolve())})
        self.assertIn("If guests sign each other out", printed)
        _, saved = configure("codex", "--api-key", stdin="sk-proj-key\n")
        self.assertEqual(saved["agents"]["codex"], {"api_key_file": str(config.with_name("openai-api-key"))})
        rox.agent_settings(saved, config)
        _, saved = configure("claude", "--off")
        self.assertEqual(list(saved["agents"]), ["codex"])
        self.assertFalse(config.with_name("anthropic-api-key").exists())
        for argv, message in ((["claude", "--version", "new"], "Expected latest or a version"),
                              (["claude", "--auth-file", "x"], "--auth-file is for codex"),
                              ([], "Name the agent to set up")):
            with self.subTest(argv=argv), self.assertRaisesRegex(rox.Error, message):
                configure(*argv)

    def test_guest_git_keys_default_to_the_ssh_keys_and_can_be_turned_off(self):
        ssh = self.root / "home" / ".ssh"
        ssh.mkdir(parents=True)
        (ssh / "id_ed25519").write_text("key")
        deploy = self.root / "deploy"
        deploy.write_text("key")
        args = rox.argparse.Namespace(git_key=None, no_git_agent=False)
        self.assertEqual(rox.guest_git_keys(args, {}, "c"), [str(ssh / "id_ed25519")])
        self.assertEqual(rox.guest_git_keys(args, {"git_keys": [str(deploy)]}, "c"), [str(deploy)])
        self.assertEqual(rox.guest_git_keys(args, {"git_agent": False}, "c"), [])
        self.assertEqual(rox.guest_git_keys(rox.argparse.Namespace(git_key=None, no_git_agent=True), {}, "c"), [])
        with self.assertRaisesRegex(rox.Error, "true or false for git_agent"):
            rox.guest_git_keys(args, {"git_agent": "yes"}, "c")

    def test_git_agent_holds_the_keys_on_the_launcher_and_forwards_them(self):
        key = self.root / "id_ed25519"
        key.write_text("key")
        guest = self.saved_guest(git_keys=[str(key)])
        runtime = self.root / "runtime"
        statuses = [2]
        commands, started = [], []

        class Process:
            pid = 4242

            def poll(self):
                return None

        def fake_run(argv, **kwargs):
            commands.append((argv, kwargs.get("env", {}).get("SSH_AUTH_SOCK")))
            return subprocess.CompletedProcess(argv, statuses.pop(0) if argv[1:] == ["-l"] else 0, "", "")

        def fake_popen(argv, **kwargs):
            started.append(argv)
            if argv[0] == "ssh-agent":
                (runtime / "git-agent.sock").touch()
            return Process()

        with (
            patch.object(rox.Guest, "runtime", property(lambda guest: runtime)),
            patch.object(rox, "run", side_effect=fake_run),
            patch.object(rox.subprocess, "Popen", side_effect=fake_popen),
            patch.object(rox.Path, "is_socket", lambda path: path.exists()),
            patch.object(rox, "pid_alive", return_value=False),
        ):
            rox.start_git_agent(guest)
        sock = str(runtime / "git-agent.sock")
        self.assertEqual(commands, [(["ssh-add", "-l"], sock), (["ssh-add", str(key)], sock)])
        self.assertEqual(started[0], ["ssh-agent", "-D", "-a", sock])
        forward = started[1]
        self.assertIn(f"ForwardAgent={sock}", forward)
        self.assertEqual(forward[-1], shlex.join(["sh", "-c", rox.GIT_AGENT_SCRIPT, "rox",
                                                  "/home/workspace/.rox/x/git-agent.sock"]))
        self.assertEqual((runtime / "git-forward.pid").read_text(), "4242\n")
        self.assertNotIn(str(key), forward)

    def test_git_agent_comes_back_with_the_guest_and_stops_with_it(self):
        guest = self.saved_guest(git_keys=["/keys/id"])
        calls = []
        with (
            patch.object(rox, "restore_git_agent", side_effect=lambda guest: calls.append("restore") or True),
            patch.object(rox, "close_git_forward", side_effect=lambda guest: calls.append("close")),
            patch.object(rox, "restore_funnels", return_value=False),
            patch.object(rox, "ensure_runtime"),
            patch.object(rox, "connect"),
            patch.object(rox, "register_guest"),
            patch.object(rox, "close_tunnel"),
        ):
            self.assertTrue(rox.resume(guest))
            self.assertEqual(calls, ["restore"])
            calls.clear()
            rox.resume(guest, force=True)
            self.assertEqual(calls, ["close", "restore"])
        with patch.object(rox, "pid_alive", return_value=True), patch.object(rox, "start_git_agent") as start:
            self.assertFalse(REAL_RESTORE_GIT_AGENT(guest))
            start.assert_not_called()
        with patch.object(rox, "pid_alive", return_value=False), \
                patch.object(rox, "start_git_agent", side_effect=rox.Error("passphrase")), \
                contextlib.redirect_stderr(io.StringIO()) as warned:
            self.assertFalse(REAL_RESTORE_GIT_AGENT(guest))
        self.assertIn("can't use your SSH keys until this works: passphrase", warned.getvalue())
        with patch.object(rox, "stop_git_agent") as stopped, patch.object(rox, "close_tunnel"), \
                patch.object(rox, "stop_guest"), contextlib.redirect_stdout(io.StringIO()):
            rox.main(["stop", guest.name])
        stopped.assert_called_once()

    def test_guest_shells_and_rex_server_find_the_forwarded_git_agent(self):
        argv = rox.shell_command("/home/workspace/.rox/rv-x/r.sock", "/home/workspace")
        self.assertIn("SSH_AUTH_SOCK=/home/workspace/.rox/rv-x/git-agent.sock", argv)
        script = (ROOT / "guest/bootstrap.sh").read_text()
        server = script[script.index("    server)"):script.index("    session)")]
        self.assertIn('export SSH_AUTH_SOCK="$base/git-agent.sock"', server)

    def test_clone_keeps_the_pinned_git_host_keys_for_later_fetches(self):
        home = self.root / "home"
        workspace = self.root / "guest" / "projects" / "app"
        workspace.parent.mkdir(parents=True)
        pins = workspace.parent / "git_known_hosts"
        pins.write_text("github.com ssh-ed25519 AAAA\n")
        (home / ".ssh").mkdir(parents=True)
        (home / ".ssh" / "known_hosts").write_text("other ssh-ed25519 BBBB\n")
        env = {"HOME": str(home), "PATH": os.environ["PATH"]}
        for _ in range(2):
            subprocess.run(["bash", str(ROOT / "guest/bootstrap.sh"), "clone", str(workspace),
                            "git@127.0.0.1:none.git", "main", "no"], env=env, capture_output=True)
        self.assertEqual((home / ".ssh" / "known_hosts").read_text(),
                         "other ssh-ed25519 BBBB\ngithub.com ssh-ed25519 AAAA\n")


if __name__ == "__main__":
    unittest.main()
