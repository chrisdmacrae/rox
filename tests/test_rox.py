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


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        state = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        state.start()
        self.addCleanup(state.stop)
        # Keep the app host prompt out of tests run from a terminal.
        stdin = patch.object(rox.sys, "stdin", io.StringIO())
        stdin.start()
        self.addCleanup(stdin.stop)

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
             "rox sample project", "--", "rox", "up", "container",
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
        with patch.object(rox, "up_on_host") as launch:
            rox.main([
                "up", "linux", "-S", "buildbox", "--cwd", str(self.root / "missing"),
                "--project", "fixture", "--repo", "https://git.example.test/repo.git", "--branch", "main",
            ])
        self.assertEqual(launch.call_args.args[2:], ("fixture", "https://git.example.test/repo.git", "main"))

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
        guest = rox.Guest("rv-fixture-000011112222", "container", "image", "fixture", "main", "workspace",
                              address="127.0.0.1", port=44000, phase="ready",
                              workspace="/home/workspace/project with spaces", guest_socket="/home/workspace/rex.sock")
        with patch.object(rox.Guest, "load", return_value=guest), patch.object(rox.os, "execvpe") as exec_run:
            rox.main(["shell", guest.name])
        executable, argv, env = exec_run.call_args.args
        self.assertEqual(executable, "ssh")
        self.assertIn("-t", argv)
        self.assertIn("ForwardAgent=no", argv)
        self.assertEqual(argv[-3:-1], ["--", "127.0.0.1"])
        self.assertEqual(shlex.split(argv[-1])[-1], guest.workspace)
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
            if argv[1:3] == ["image", "inspect"] and not built:
                return subprocess.CompletedProcess(argv, 1, "", "image not found")
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

    def test_default_container_image_run_uses_local_storage(self):
        guest = rox.Guest("rv-fixture-000011112222", "container", "rox:linux",
                              "fixture", "main", "workspace", engine="docker",
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
        guest = rox.Guest(name, "container", "image", "fixture", "main", "workspace",
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
        self.assertEqual(reopened.phase, "failed")

    def test_tunnel_eof_checks_guest_and_falls_back_to_ssh_stdio(self):
        guest = rox.Guest("rv-fixture-000011112222", "container", "image",
                              "fixture", "main", "workspace", guest_socket="/home/workspace/r.sock",
                              session_id="session:fixture")
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
            rox.attach_guest(guest)
        self.assertEqual(calls, [
            "tunnel",
            [*rox.rex_on(guest), "ls", "--json"],
            ("server", guest.guest_socket),
            "close",
            "relay",
            [*rox.rex_on(guest), "session", "inspect", guest.session_id, "--json"],
        ])

    def provision(self, backend, fail_stage=None, open_shell=False, prebuilt=""):
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
            backend: {"image": "fixture", "identity": str(identity), "host_key": str(host_key)},
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
                return "/home/guest/project\n/home/guest/r.sock"
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
                self.assertIn("unix:///home/guest/r.sock", args)
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
            self.assertEqual(state.phase, "failed" if fail_stage else "ready")
            self.assertEqual((state.image, state.prebuilt), (prebuilt or "fixture", bool(prebuilt)))
            self.assertTrue(state.registered)
            self.assertEqual(commands.count(["rex", "hosts", "add", name, state.endpoint]), 1)
            if not fail_stage:
                self.assertEqual(state.session_id, "session:fixture")
                if open_shell:
                    self.assertIn(["rex", "--autostart=false", "window", "new", "-s", "session:launcher",
                                   "--keep-open", "Guest", "--", str(ROOT / "rox"), "shell", name], commands)
            rox.main(["down", name])
            self.assertFalse(state.directory.exists())
            self.assertIn(["rex", "hosts", "remove", name], commands)
            if backend == "container":
                self.assertIn(["docker", "rm", "-f", "-v", "managed-container"], commands)
            else:
                self.assertIn(["tart", "stop", name], commands)
                self.assertIn(["tart", "delete", name], commands)
        return stages

    def test_all_backends_register_a_ready_rex_session(self):
        for backend in ("container", "linux", "macos"):
            with self.subTest(backend=backend):
                self.assertEqual(self.provision(backend), ["prepare", "server", "clone", "setup", "session"])

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
        env = {**os.environ, "HOME": str(home), "REX_SERVER": "unix:///leak"}
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
        self.assertEqual(paths, [workspace, str(home / ".rox/rv-fixture-000011112222/r.sock")])
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
        guest = rox.Guest("rv-app-000011112222", "container", "image", "fixture", "main",
                              "workspace", guest_socket="/home/workspace/r.sock", registered=True)
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
        guest = rox.Guest("rv-app-000011112222", "container", "image", "fixture", "main",
                              "workspace", guest_socket="/home/workspace/r.sock", registered=True)
        with patch.object(rox, "run") as command:
            rox.offer_app_host(guest)
        command.assert_not_called()

    def test_reconnect_offers_the_app_host_unless_disabled(self):
        name = "rv-retained-000011112222"
        guest = rox.Guest(name, "container", "image", "fixture", "main", "workspace",
                              engine="docker", guest_socket="/home/workspace/r.sock",
                              session_id="session:fixture")
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
        guest = rox.Guest(name, "container", "image", project, "main", "workspace", engine="docker")
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


if __name__ == "__main__":
    unittest.main()
