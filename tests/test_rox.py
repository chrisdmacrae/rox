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
                                        "XDG_CACHE_HOME": str(self.root / "cache-home")})
        state.start()
        self.addCleanup(state.stop)
        # Keep the app host prompt out of tests run from a terminal.
        stdin = patch.object(rox.sys, "stdin", io.StringIO())
        stdin.start()
        self.addCleanup(stdin.stop)
        # The default container engine depends on the platform, so pin it for repeatable tests.
        engine = patch.object(rox, "default_engine", return_value="docker")
        engine.start()
        self.addCleanup(engine.stop)
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
             "--branch=published-feature", "--boot-timeout=60", "--open-shell",
             "--config=~/Rex Config/rox.json", "--image=custom:latest", "--engine=podman",
             "--git-known-hosts=~/.ssh/git_hosts", "--git-key=~/.ssh/remote key", "--no-hook",
             "--", "agent", "'an argument with spaces' $HOME"],
            [*rex, "wait", "-s", "session:launch", "block:provision"],
            [*rex, "capture", "-s", "session:launch", "-b", "block:provision", "--trim", "--unwrap"],
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
            patch.object(rox, "apple_container_address", return_value=("192.168.64.10", 22)),
            patch.object(rox.os, "execvp") as execvp,
            contextlib.redirect_stdout(io.StringIO()) as printed,
        ):
            rox.main(["forward", guest.name, "5173", "8080:3000"])
        argv = execvp.call_args.args[1]
        self.assertEqual(argv[-2:], ["--", "192.168.64.10"])
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
                                   "--keep-open", "Guest", "--", str(ROOT / "rox"), "shell", name,
                                   "sample-project"], commands)
            rox.main(["down", name])
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

    def app_host_fixture(self, transport="unix", answer="\n"):
        guest = rox.Guest("rv-app-000011112222", "container", "image", "workspace",
                          guest_socket="/home/workspace/r.sock", registered=True)
        clients = {"clients": [
            {"client_id": "client:cli", "info": {"kind": "cli"}, "connection_state": "connected",
             "principal": {"transport": "unix"}},
            {"client_id": "client:lost", "info": {"kind": "app"}, "connection_state": "lost",
             "principal": {"transport": "unix"}},
            {"client_id": "client:app", "info": {"kind": "app"}, "connection_state": "connected",
             "principal": {"transport": transport}},
        ]}
        calls = []

        def command(argv, **kwargs):
            calls.append((argv, kwargs.get("input")))
            stdout = json.dumps(clients) if argv[-3:] == ["client", "ls", "--json"] else ""
            return subprocess.CompletedProcess(argv, 0, stdout, "")

        stdin = io.StringIO(answer)
        stdin.isatty = lambda: True
        captured = io.StringIO()
        with (
            patch.object(rox.sys, "stdin", stdin),
            patch.object(rox.shutil, "which", return_value="/usr/bin/pbcopy"),
            patch.object(rox, "run", side_effect=command),
            contextlib.redirect_stdout(captured),
        ):
            rox.offer_app_host(guest)
        return guest, [argv for argv, _ in calls], calls, captured.getvalue()

    def test_app_host_copies_the_endpoint_and_opens_the_local_app_dialog(self):
        guest, argvs, calls, printed = self.app_host_fixture()
        self.assertEqual(argvs, [
            ["rex", "--autostart=false", "client", "ls", "--json"],
            ["pbcopy"],
            ["rex", "--autostart=false", "-C", "client:app", "do", "client.host.add"],
        ])
        self.assertEqual(calls[1][1], guest.endpoint)
        self.assertIn(f"Add {guest.name} to the Rex app's host list?", printed)
        self.assertIn(f"Copied {guest.endpoint}", printed)

    def test_app_host_is_skipped_when_declined_or_the_app_is_remote(self):
        _, argvs, _, _ = self.app_host_fixture(answer="n\n")
        self.assertEqual(argvs, [["rex", "--autostart=false", "client", "ls", "--json"]])
        _, argvs, _, printed = self.app_host_fixture(transport="tcp")
        self.assertEqual(argvs, [["rex", "--autostart=false", "client", "ls", "--json"]])
        self.assertNotIn("host list?", printed)

    def test_app_host_is_skipped_without_a_terminal(self):
        guest = rox.Guest("rv-app-000011112222", "container", "image", "workspace",
                          guest_socket="/home/workspace/r.sock", registered=True)
        with patch.object(rox, "run") as command:
            rox.offer_app_host(guest)
        command.assert_not_called()

    def test_reconnect_offers_the_app_host_unless_disabled(self):
        name = "rv-retained-000011112222"
        guest = rox.Guest(name, "container", "image", "workspace",
                          engine="docker", guest_socket="/home/workspace/r.sock")
        guest.directory.mkdir(parents=True)
        guest.save()
        for argv, offered in ((["reconnect", name], True), (["reconnect", name, "--no-app-host"], False)):
            with self.subTest(argv=argv):
                with (
                    patch.object(rox, "container_address", return_value=("127.0.0.1", 33000)),
                    patch.object(rox, "guest_stage"),
                    patch.object(rox, "attach_guest"),
                    patch.object(rox, "close_tunnel"),
                    patch.object(rox, "run", return_value=subprocess.CompletedProcess([], 0, "", "")),
                    patch.object(rox, "offer_app_host") as offer,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    rox.main(argv)
                self.assertEqual(offer.called, offered)
                if offered:
                    self.assertEqual(offer.call_args.args[0].name, name)

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
            contextlib.redirect_stdout(captured),
        ):
            rox.main(["down", "--cwd", str(nested)])
        return down.call_args.args[0], captured.getvalue()

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
            "--repo=git@git.example.test:fork/repo.git", "--branch=published-feature", "--open-shell",
            "--git-key=~/.ssh/deploy", "--", "agent",
        ])
        self.assertEqual(requests[3][requests[3].index("--") + 1:][:8],
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


if __name__ == "__main__":
    unittest.main()
