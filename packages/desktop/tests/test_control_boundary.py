"""Optional desktop control primitives, outside the socket-free core test suite."""

from __future__ import annotations

import fcntl
import os
import socket
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory


def test_private_unix_control_channel_and_single_instance_ownership() -> None:
    # Keep the pathname below macOS's Unix-domain socket limit on CI and locally.
    temporary_root = Path("/private/tmp" if sys.platform == "darwin" else "/tmp")
    with TemporaryDirectory(prefix="ob-control-", dir=temporary_root) as temporary:
        directory = Path(temporary).resolve(strict=True)
        directory.chmod(0o700)
        endpoint = directory / "control.sock"
        lock_path = directory / "instance.lock"
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            contender = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import fcntl,sys\n"
                    "with open(sys.argv[1], 'r+b') as handle:\n"
                    " try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                    " except BlockingIOError: sys.exit(0)\n"
                    " sys.exit(1)\n",
                    str(lock_path),
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            assert contender.returncode == 0, contender.stderr

            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.settimeout(2)
                server.bind(str(endpoint))
                endpoint.chmod(0o600)
                server.listen(1)
                assert stat.S_IMODE(directory.stat().st_mode) == 0o700
                assert stat.S_IMODE(endpoint.stat().st_mode) == 0o600
                assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600

                def answer_status() -> None:
                    connection, _address = server.accept()
                    with connection:
                        connection.settimeout(2)
                        assert connection.recv(64) == b"status\n"
                        connection.sendall(b"paused\n")

                with ThreadPoolExecutor(max_workers=1) as pool:
                    response = pool.submit(answer_status)
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                        client.settimeout(2)
                        client.connect(str(endpoint))
                        client.sendall(b"status\n")
                        assert client.recv(64) == b"paused\n"
                    response.result(timeout=3)
            endpoint.unlink()
            assert not endpoint.exists()
        finally:
            os.close(lock_fd)
