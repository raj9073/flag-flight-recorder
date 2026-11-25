"""Manage the loopback-only demo Flagsmith instance from a checkout."""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from flag_flight_recorder import FlightError
from flag_flight_recorder.diagnostics import compose_engine
from flag_flight_recorder.store import read_file, write_private


def settings(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--port", type=int, default=13810, help="loopback Flagsmith port")
    parser.add_argument(
        "--state", type=Path, default=Path(".local/lab"), help="private lab directory"
    )


@dataclass(frozen=True)
class Stack:
    port: int
    state: Path

    def __post_init__(self) -> None:
        if not 1024 <= self.port <= 65535:
            raise FlightError("Choose a Flagsmith port between 1024 and 65535.")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def env_file(self) -> Path:
        return self.state / "compose.env"

    def compose(self, *args: str) -> None:
        engine = compose_engine()
        if not engine:
            raise FlightError("Install Podman or Docker with a Compose provider.")
        if not Path("compose.yaml").is_file():
            raise FlightError("Run the demo stack command from the project checkout.")
        variables = dict(os.environ)
        for key in ("FFR_COMPOSE_PORT", "FFR_COMPOSE_DB_PASSWORD", "FFR_COMPOSE_DJANGO_SECRET"):
            variables.pop(key, None)
        try:
            subprocess.run(
                [
                    engine,
                    "compose",
                    "--project-name",
                    f"flag-flight-recorder-{self.port}",
                    "--env-file",
                    str(self.env_file.resolve()),
                    *args,
                ],
                env=variables,
                check=True,
            )
        except subprocess.CalledProcessError as error:
            raise FlightError(f"Compose exited with status {error.returncode}.") from error

    def verify_state(self) -> None:
        state = read_file(self.env_file, "demo Compose settings").decode("utf-8")
        if f"FFR_COMPOSE_PORT={self.port}" not in state.splitlines():
            raise FlightError("This lab directory belongs to a different port.")

    def start(self) -> None:
        if not self.env_file.exists():
            payload = (
                f"FFR_COMPOSE_PORT={self.port}\n"
                f"FFR_COMPOSE_DB_PASSWORD={secrets.token_hex(24)}\n"
                f"FFR_COMPOSE_DJANGO_SECRET={secrets.token_hex(32)}\n"
            )
            write_private(self.env_file, payload.encode())
        self.verify_state()
        self.compose("up", "-d")
        self.wait()

    def wait(self, timeout: float = 90.0) -> None:
        deadline = time.monotonic() + timeout
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while time.monotonic() < deadline:
            try:
                with opener.open(self.url + "/health/liveness/", timeout=3) as response:
                    if response.status == 200:
                        return
            except OSError:
                pass
            time.sleep(1)
        raise FlightError(f"Flagsmith did not become healthy at {self.url}.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "status", "stop"))
    settings(parser)
    args = parser.parse_args()
    try:
        stack = Stack(args.port, args.state)
        if args.action == "start":
            stack.start()
            print(f"Flagsmith ready at {stack.url}")
        else:
            stack.verify_state()
            stack.compose(*(("ps", "-a") if args.action == "status" else ("stop",)))
    except FlightError as error:
        print(f"Flight Recorder demo: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
