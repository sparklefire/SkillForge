from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dgx_service_is_loopback_only_and_offline() -> None:
    unit = (ROOT / "deploy/systemd/skillforge-demo.service").read_text(encoding="utf-8")
    starter = (ROOT / "scripts/start_native.sh").read_text(encoding="utf-8")

    assert "SKILLFORGE_HOST=127.0.0.1" in unit
    assert "SKILLFORGE_PORT=7860" in unit
    assert "SKILLFORGE_SKIP_DOTENV=1" in unit
    assert "run_demo_mode.sh offline" in unit
    assert "Restart=on-failure" in unit
    assert "NoNewPrivileges=true" in unit
    assert "docker" not in unit.lower()
    assert 'SKILLFORGE_SKIP_DOTENV:-0' in starter


def test_dgx_deployment_shell_syntax() -> None:
    scripts = [
        ROOT / "scripts/manage_dgx_demo_service.sh",
        ROOT / "scripts/dgx_demo_tunnel.sh",
    ]
    for script in scripts:
        subprocess.run(["bash", "-n", str(script)], check=True)

    tunnel = scripts[1].read_text(encoding="utf-8")
    assert "unset STEP_API_KEY" in tunnel
    assert "set -a" not in tunnel
