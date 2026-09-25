import os
import shutil
import socket
import subprocess
import time

import pytest


@pytest.fixture(scope="session")
def redis_url():
    if url := os.getenv("AGENTKIT_TEST_REDIS_URL"):
        yield url
        return
    binary = shutil.which("redis-server")
    if not binary:
        pytest.skip("no Redis available (install redis-server or set AGENTKIT_TEST_REDIS_URL)")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([binary, "--port", str(port), "--save", "", "--appendonly", "no"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    yield f"redis://127.0.0.1:{port}/0"
    proc.terminate()
