"""Expose a private Unix socket through SSH's standard streams."""
import select
import socket
import subprocess
import sys
from pathlib import Path


def relay(runtime, command):
    paths = [runtime / "rex.sock", runtime / "relay.sock"]
    bound_paths = []
    children = set()
    shutdown = None
    with socket.socket(socket.AF_UNIX) as listener, socket.socket(socket.AF_UNIX) as control:
        try:
            for sock, path in zip((listener, control), paths):
                path.unlink(missing_ok=True)
                sock.bind(str(path))
                bound_paths.append(path)
                sock.listen()
            while True:
                readable, _, _ = select.select([listener, control], [], [], 1)
                if control in readable:
                    shutdown, _ = control.accept()
                    break
                children = {child for child in children if child.poll() is None}
                if listener in readable:
                    client, _ = listener.accept()
                    with client:
                        children.add(subprocess.Popen(command, stdin=client, stdout=client))
        finally:
            for path in bound_paths:
                path.unlink(missing_ok=True)
            for child in children:
                child.terminate()
            for child in children:
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            if shutdown is not None:
                shutdown.close()


if __name__ == "__main__":
    relay(Path(sys.argv[1]), sys.argv[2:])
