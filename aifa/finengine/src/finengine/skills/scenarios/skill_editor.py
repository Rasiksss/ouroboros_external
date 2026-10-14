"""#93 (S3 v1): редактор навыков — «нейронка переписывает скрипт», с гейтом.

Просьба пользователя за пределами ручек («добавь колонку», «меняй формат»,
«новая метрика в статистику») → glm-5.2 правит КОПИЮ файла навыка → ГЕЙТ
(компиляция + для .sh парс) → готовый unified-diff как артефакт и .patch-файл.

Активация v1 — КОНТРОЛИРУЕМАЯ: патч применяется через git-коммит + ship.sh
(это же даёт версии и откат `git revert`). Горячей подмены кода в рантайме нет
намеренно: прод с пользователями, правки проходят ревью+гейт. v2 (после
per-user полок #39/#52): правка в запечатанной комнате станции + авто-активация
копии пользователя.

Границы безопасности:
  - редактируемые файлы — ТОЛЬКО из явного whitelist-а (SKILL_FILES);
  - модель возвращает ПОЛНЫЙ новый файл (не diff) — применение атомарно
    и не зависит от фантазий модели о номерах строк;
  - гейт обязателен: не скомпилировалось → патч не сохраняется.
"""
from __future__ import annotations

import difflib
import json
import logging
import time
from pathlib import Path
from finengine.config import settings

log = logging.getLogger(__name__)


# Whitelist редактируемых навыков: ключ → (путь в образе бэка, краткое
# назначение для модели и для списка в туле).
SKILL_FILES: dict[str, tuple[str, str]] = {
    "daily_runner": (
        "src/finengine/skills/scenarios/runner.py",
        "оркестрация прогонов автоматизаций (дайджест/календарь/агент): прогресс, артефакты",
    ),
    "daily_render": (
        "src/finengine/skills/news/render.py",
        "рендер дайджеста: секции, long list, форматирование",
    ),
    "daily_collect": (
        "src/finengine/skills/news/collect.py",
        "сбор новостей из источников: группы, статистика",
    ),
    "daily_macro": (
        "src/finengine/skills/news/macro_indicators.py",
        "макропоказатели: реестр индикаторов, дашборд, недельная секция",
    ),
    "calendar": (
        "src/finengine/skills/ir_calendar.py",
        "IR-календарь: источники событий, извлечение, группы, окно",
    ),
    "station_extract": (
        "station-skills/aifa-extract/scripts/extract.py",
        "станционный навык извлечения показателей (движок, Excel, события)",
    ),
    "station_analyze": (
        "station-skills/aifa-analyze/scripts/analyze.py",
        "станционный навык финанализа (заземление, капитал, отчёт)",
    ),
}

_MAX_FILE_CHARS = 120_000

_SYSTEM = (
    "Ты — редактор кода навыка финансовой платформы. Тебе дают ОДИН файл и "
    "просьбу пользователя. Внеси МИНИМАЛЬНУЮ правку, реализующую просьбу.\n\n"
    "Правила:\n"
    "- Верни СТРОГО JSON: {\"file\": \"<ПОЛНЫЙ новый текст файла>\", "
    "\"summary\": \"что изменил, по-русски, 1-3 предложения\", "
    "\"feasible\": true|false, \"reason\": \"если false — почему\"}.\n"
    "- Правка хирургическая: не переформатируй чужой код, не меняй несвязанное, "
    "сохраняй стиль и комментарии файла.\n"
    "- Если просьба не реализуема правкой ЭТОГО файла (нужен другой файл, "
    "внешние ключи, инфраструктура) — feasible=false с объяснением, file пустой.\n"
    "- Никаких новых внешних зависимостей и сетевых вызовов, кроме уже "
    "используемых в файле."
)


def _repo_root() -> Path:
    # В контейнере бэка код лежит в /app; локально — корень репо (два уровня
    # выше src/finengine/skills/scenarios/).
    for cand in (Path("/app"), Path(__file__).resolve().parents[3]):
        if (cand / "src" / "finengine").is_dir():
            return cand
    return Path.cwd()


def _gate(rel_path: str, new_text: str) -> tuple[bool, str]:
    """Гейт: код обязан парситься. .py — ast.parse; .sh — bash -n."""
    if rel_path.endswith(".py"):
        import ast
        try:
            ast.parse(new_text)
            return True, "py-parse OK"
        except SyntaxError as e:
            return False, f"SyntaxError: {e}"
    if rel_path.endswith(".sh"):
        import subprocess
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
            f.write(new_text)
            tmp = f.name
        try:
            r = subprocess.run(["bash", "-n", tmp], capture_output=True, text=True,
                               timeout=20)
            return (r.returncode == 0,
                    "bash -n OK" if r.returncode == 0 else r.stderr.strip()[:300])
        finally:
            Path(tmp).unlink(missing_ok=True)
    return True, "no-op gate"


async def propose_patch(*, user_id: int, target: str, request: str) -> dict:
    """Просьба → правка whitelist-файла редактором (glm-5.2) → гейт → патч.

    Возвращает {"ok", "summary"/"error", "diff", "gate", "patch_path",
    "artifact_run_id"}. Патч НЕ активируется здесь — активация через
    git+ship (ревью). Никогда не бросает."""
    spec = SKILL_FILES.get(target)
    if spec is None:
        return {"ok": False, "error": (
            f"неизвестный навык {target!r}; доступны: "
            + ", ".join(f"{k} ({v[1]})" for k, v in SKILL_FILES.items()))}
    rel_path, purpose = spec
    src_path = _repo_root() / rel_path
    if not src_path.is_file():
        return {"ok": False, "error": f"файл навыка не найден: {rel_path}"}
    original = src_path.read_text(encoding="utf-8")
    if len(original) > _MAX_FILE_CHARS:
        return {"ok": False, "error": f"файл слишком велик для v1-редактора ({len(original)} симв.)"}
    req = (request or "").strip()
    if not req:
        return {"ok": False, "error": "пустая просьба"}

    try:
        from finengine.llm import openrouter as llm  # noqa: PLC0415
        prompt = (
            f"## Файл навыка: {rel_path}\n"
            f"Назначение: {purpose}\n\n"
            f"## Текущий код:\n```\n{original}\n```\n\n"
            f"## Просьба пользователя:\n{req}"
        )
        raw, _usage = await llm.complete_with_usage(
            prompt=prompt, system=_SYSTEM, model=settings.openrouter_model,
            temperature=0.0, max_output=60_000,
            response_format={"type": "json_object"},
        )
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        log.warning("skill editor LLM failed: %s", e)
        return {"ok": False, "error": f"редактор недоступен: {e}"}

    if not data.get("feasible", False):
        return {"ok": False, "error": (
            "не реализуемо правкой этого навыка: "
            + str(data.get("reason") or "без объяснения"))}
    new_text = str(data.get("file") or "")
    if not new_text.strip() or new_text.strip() == original.strip():
        return {"ok": False, "error": "редактор не внёс изменений"}

    gate_ok, gate_msg = _gate(rel_path, new_text)
    diff = "".join(difflib.unified_diff(
        original.splitlines(keepends=True), new_text.splitlines(keepends=True),
        fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}",
    ))
    if not gate_ok:
        return {"ok": False, "error": f"гейт не пройден ({gate_msg}) — патч отклонён",
                "diff": diff[:4000]}

    # Патч на диск (для применения git apply) + артефакт в историю пользователя.
    pdir = Path(settings.data_dir) / "skill_patches"
    pdir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    patch_path = pdir / f"{ts}-{target}.patch"
    patch_path.write_text(diff, encoding="utf-8")

    artifact_run_id = ""
    try:
        from finengine.store import artifacts as _art  # noqa: PLC0415
        meta = _art.save_artifact(
            user_id=user_id, project_id="skill-editor", kind="skill_patch",
            payload={"target": target, "path": rel_path, "request": req,
                     "summary": str(data.get("summary") or ""),
                     "gate": gate_msg, "diff": diff[:60_000]},
            title=f"Правка навыка: {target}",
            summary=str(data.get("summary") or "")[:120],
            params={"patch_path": str(patch_path)},
        )
        artifact_run_id = meta.run_id
    except Exception:  # noqa: BLE001 — артефакт best-effort, патч уже на диске
        log.warning("skill patch artifact save failed", exc_info=True)

    return {"ok": True, "summary": str(data.get("summary") or ""),
            "gate": gate_msg, "diff": diff[:12_000],
            "patch_path": str(patch_path), "artifact_run_id": artifact_run_id,
            "note": ("Патч прошёл гейт и сохранён. Активация — применением к "
                     "коду (git) и деплоем; откат — git revert.")}
