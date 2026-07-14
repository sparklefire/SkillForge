"""Central model routing backed by config/models.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]


class ModelRouter:
    def __init__(self, config_path: Path | None = None) -> None:
        path = config_path or ROOT / "config" / "models.json"
        self.config: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))

    def reasoning(self, route: str) -> dict[str, str]:
        try:
            selected = self.config["reasoning"][route]
        except KeyError as exc:
            raise KeyError(f"未知推理路由: {route}") from exc
        return {
            "model": selected["model"],
            "reasoning_effort": selected.get("reasoning_effort", "medium"),
        }

    def capability(self, family: str, name: str) -> str:
        try:
            return str(self.config[family][name])
        except KeyError as exc:
            raise KeyError(f"未知能力路由: {family}.{name}") from exc
