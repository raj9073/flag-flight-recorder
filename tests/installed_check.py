"""Exercise the distribution in a fresh project, with its own importable handler."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def check(wheel: Path, archives: Path | None, *, live: bool = False) -> None:
    with tempfile.TemporaryDirectory(prefix="flight-recorder-consumer-") as tmp:
        project = Path(tmp)
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in {"PYTHONPATH", "VIRTUAL_ENV"}
            and not k.startswith(("FLIGHT_RECORDER_", "FLAGSMITH_"))
        }
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        subprocess.run(
            ["uv", "venv", "--python", sys.executable, ".venv"], cwd=project, env=env, check=True
        )
        python = project / ".venv/bin/python"
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), str(wheel.resolve()), "pytest"],
            cwd=project,
            env=env,
            check=True,
        )

        def run(
            *args: str, cwd: Path = project, extra: dict[str, str] | None = None, status: int = 0
        ) -> str:
            result = subprocess.run(
                [str(python), *args],
                cwd=cwd,
                env={**env, **(extra or {})},
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert result.returncode == status, result.stdout + result.stderr
            return result.stdout.strip()

        run(
            "-c",
            "from pathlib import Path; import examples.starter, shutil; "
            'shutil.copytree(Path(examples.starter.__file__).parent,"myapp",ignore=shutil.ignore_patterns("__pycache__"))',
        )
        cli = ["-m", "flag_flight_recorder"]
        for mode in ["fixture", "live"] if live else ["fixture"]:
            arguments = ["-m", "myapp.run"]
            extra = {}
            if mode == "live":
                arguments += ["--live"]
                extra = {k: os.environ[k] for k in ("FLAGSMITH_URL", "FLAGSMITH_SERVER_KEY")}
            cid = run(*arguments, extra=extra)
            listing = json.loads(run(*cli, "list", "--operation", "render-label"))
            assert cid in {row["id"] for row in listing}
            replay = json.loads(
                run(*cli, "replay", cid, "--registry", "myapp.registry:build_registry")
            )
            assert replay["historical"]["outcome"] == {"status": "success", "result": "priority"}
            assert replay["isolation"]["interfaces"] == ["lo"]
            (project / "expected.json").write_text('"priority"\n')
            run(
                *cli,
                "export-pytest",
                cid,
                "--registry",
                "myapp.registry:build_registry",
                "--expect",
                "expected.json",
                "--output",
                mode,
            )
            assert "1 passed" in run("-m", "pytest", "-q", f"{mode}/test_incident.py")
        if archives:
            for name, exception in (("csv-export", "ValueError"), ("shipping-quote", "KeyError")):
                folder = project / name
                with zipfile.ZipFile(archives / f"{name}-synthetic-pytest.zip") as bundle:
                    bundle.extractall(folder)
                product = ["-m", "pytest", "-q", "test_incident.py"]
                red = run(
                    *product, cwd=folder, extra={"FLIGHT_RECORDER_APP_VERSION": "buggy"}, status=1
                )
                assert f"{name} (buggy): {exception}" in red
                assert "1 passed" in run(
                    *product, cwd=folder, extra={"FLIGHT_RECORDER_APP_VERSION": "fixed"}
                )
    print(
        "Clean wheel installation, custom registry, capture, offline replay and exported tests passed."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--archives", type=Path)
    parser.add_argument(
        "--live",
        action="store_true",
        help="also run with a configured, enabled priority_label flag",
    )
    args = parser.parse_args()
    check(args.wheel, args.archives, live=args.live)
