"""Check the actual local prerequisites for an offline replay or the live examples."""

import contextlib
import os
import shutil
import socket
import subprocess
import sys

from .model import JSON, FlightError, Object


def compose_engine() -> str | None:
    requested = os.getenv("FFR_CONTAINER_ENGINE")
    if requested and requested not in ("docker", "podman"):
        raise FlightError("Set FFR_CONTAINER_ENGINE to docker or podman.")
    for name in (requested,) if requested else ("podman", "docker"):
        binary = shutil.which(name)
        if binary:
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                if (
                    subprocess.run(
                        [binary, "compose", "version"], capture_output=True, timeout=12
                    ).returncode
                    == 0
                ):
                    return binary
    return None


def preflight(live: bool, port: int, viewer_port: int) -> Object:
    checks: list[JSON] = []

    def check(label: str, ok: bool, detail: str) -> bool:
        checks.append({"check": label, "ok": ok, "detail": detail})
        return ok

    ok = check("Python", (3, 11) <= sys.version_info[:2] < (3, 15), sys.version.split()[0])
    binary = shutil.which("unshare")
    isolated = False
    if binary:
        try:
            result = subprocess.run(
                [
                    binary,
                    "--user",
                    "--map-root-user",
                    "--net",
                    "--",
                    sys.executable,
                    "-c",
                    "import socket; assert [x[1] for x in socket.if_nameindex()]==['lo']",
                ],
                capture_output=True,
                timeout=10,
            )
            isolated = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    ok = (
        check(
            "Offline worker",
            isolated,
            "User and network namespaces available."
            if isolated
            else "Use a Linux host with util-linux and permission to create user and network namespaces.",
        )
        and ok
    )
    if live:
        engine = compose_engine()
        compose = False
        if engine:
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                compose = (
                    subprocess.run([engine, "info"], capture_output=True, timeout=12).returncode
                    == 0
                )
        ok = (
            check(
                "Compose",
                compose,
                "Container engine and Compose ready."
                if compose
                else "Start Docker or Podman with its Compose provider.",
            )
            and ok
        )
        for label, number in (("Flagsmith port", port), ("Viewer port", viewer_port)):
            with socket.socket() as sock:
                try:
                    sock.bind(("127.0.0.1", number))
                    note = "available"
                except OSError:
                    note = "already in use; reuse your running service or choose another port"
                checks.append({"check": label, "port": number, "detail": note})
    return {"ready": ok, "checks": checks}
