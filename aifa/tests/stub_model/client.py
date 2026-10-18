"""Сторона теста: загрузка сценария в подставной сервер и чтение его журнала (шаг 07)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class StubModelAdmin:
    """Служебные пути сервера (`PUT /_scenario`, `/_journal`). Только стандартная библиотека."""

    def __init__(self, base_url: str, timeout_s: float = 10.0) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        request = urllib.request.Request(
            f"{self._base}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise RuntimeError(
                f"подставная модель: {method} {path} → {error.code}: {detail}"
            ) from error

    def use_scenario(self, scenario: Mapping[str, Any] | Path) -> None:
        """Заменяет сценарий; неверный сценарий сервер отклоняет, прежний остаётся."""
        body = (
            json.loads(scenario.read_text(encoding="utf-8"))
            if isinstance(scenario, Path)
            else scenario
        )
        self._call("PUT", "/_scenario", body)

    def journal(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = self._call("GET", "/_journal")
        return entries

    def clear(self) -> None:
        self._call("DELETE", "/_journal")

    def saw(self, text: str) -> bool:
        """Модель увидела `text` хотя бы в одном сообщении любого запроса."""
        return any(
            text in json.dumps(entry["messages"], ensure_ascii=False) for entry in self.journal()
        )

    def unmatched(self) -> list[dict[str, Any]]:
        """Запросы без правила: в сценарии контрактного теста их быть не должно."""
        return [entry for entry in self.journal() if entry["rule"] is None]
