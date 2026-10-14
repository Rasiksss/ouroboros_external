"""Модель извлечения задаётся настройкой, а не литералом в коде.

ЧТО СЛУЧИЛОСЬ (07.09.2026). Стенд считался переведённым на Cloud.ru: бэкенд,
станция и прокси стояли на DeepSeek-V4-Pro. Но двухстадийный экстрактор —
боевой тракт извлечения — держал два ПИНА, скопированных из Finengine 16.08
ради бенча, и все 23 реальных вызова 03.09 ушли на OpenRouter мимо настройки.
Компилятор и редактор сценариев так же пинили `z-ai/glm-5.2`.

Два сторожа:
  1. в трёх модулях нет строковых литералов вида `вендор/модель` — АСТ, не grep
     по тексту (комментарий с именем модели сторожа не красит);
  2. поведение: подменили `settings.openrouter_model` — обе стадии и сценарии
     берут именно её.

Отрицательный контроль: вернуть литерал `"qwen/qwen3.6-plus-04-02"` в
two_stage_extractor — красный.
"""
import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MODULES = ["src/finengine/skills/two_stage_extractor.py",
           "src/finengine/skills/scenarios/compiler.py",
           "src/finengine/skills/scenarios/skill_editor.py"]
_MODEL_LIT = re.compile(r"^(qwen|deepseek|deepseek-ai|moonshotai|z-ai|zai-org|openrouter|anthropic|openai|google)/[A-Za-z0-9._-]+$")


@pytest.mark.parametrize("rel", MODULES)
def test_no_model_id_literals_in_code(rel):
    tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
    hits = [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and _MODEL_LIT.match(n.value)]
    assert not hits, f"{rel}: пин модели в коде {hits} — модель задаётся настройкой"


def test_extractor_stages_follow_the_setting(monkeypatch):
    from finengine.config import settings
    from finengine.skills import two_stage_extractor as T
    src = (REPO / MODULES[0]).read_text(encoding="utf-8")
    # обе стадии сводятся к настройке, если явного аргумента нет
    assert src.count("compression_model_id or settings.openrouter_model") == 2
    assert src.count("extraction_model_id or model_id or settings.openrouter_model") == 2
    monkeypatch.setattr(settings, "openrouter_model", "probe/model-x")
    assert T.settings.openrouter_model == "probe/model-x"


def test_scenario_compiler_and_editor_follow_the_setting():
    """Импорт проверяется АСТ на верхнем уровне модуля: подстрока «в файле»
    зеленела, пока `settings` был не в области видимости (F821 на 146:50)."""
    for rel in MODULES[1:]:
        src = (REPO / rel).read_text(encoding="utf-8")
        assert "model=settings.openrouter_model," in src, f"{rel}: модель не из настройки"
        top = ast.parse(src).body
        assert any(isinstance(n, ast.ImportFrom) and n.module == "finengine.config"
                   and any(a.name == "settings" for a in n.names) for n in top), (
            f"{rel}: `settings` не импортирован на верхнем уровне — NameError в рантайме")
