"""Один \n в пересказе не роняет весь разбор шестидесяти новостей (19.08.2026).

ЖИВОЙ ПРОГОН НА ПРОДЕ, запущенный чтобы проверить, заполняется ли long list
после починки дедупа. Выпуск вышел с «0 компаний (из 100 в покрытии)» при 199
отобранных новостях, в персисте ноль записей — и прогон записался УСПЕШНЫМ.

В журнале прогона одна строка:

    classify: LLM returned non-JSON: Invalid control character at:
    line 325 column 27 (char 11167)

Модель отдала СМЫСЛОВО ВЕРНЫЙ разбор всех шестидесяти кандидатов — в логе видны
нормальные `is_relevant`, `category`, `one_sentence_ru`. Но внутри одного
значения поставила буквальный перевод строки. По букве RFC 8259 это
недопустимо, и `json.loads` отверг ВЕСЬ ответ, а `_parse_response` вернул
пустой список.

ДВЕ ПРАВКИ, И ОНИ РАЗНЫЕ ПО ПРИРОДЕ:

  * `strict=False` — разбор перестаёт ронять шестьдесят классификаций из-за
    одного \n в тексте пересказа. Это не послабление требований к модели, а
    признание того, что цену за её опечатку платит читатель выпуска.
  * неразобранный ответ теперь ИСКЛЮЧЕНИЕ, а не пустой список. Прежде пустота
    выглядела как «сегодня новостей не было», и прогон помечался успешным —
    ровно тот же класс лжи о самом себе, что закрывали 17.08 для отказа
    модели (см. test_empty_digest_is_never_called_success). Дверь была другая:
    там модель молчала, здесь — говорила, но её не расслышали.

🔴 РЕВИЗИЯ 21.08: снят `test_the_reason_stays_next_to_the_code`. Он охранял
КОММЕНТАРИЙ: искал «control character» в 900 знаках перед вызовом. Две мутации
показали, что сторожа в нём нет. Сломать поведение (вернуть `json.loads(text)`)
— краснеет `test_a_stray_newline_does_not_destroy_the_batch`, то есть предмет
уже прикрыт исполняющим соседом. Перевести комментарий на русский, не тронув ни
строки кода, — краснел ТОЛЬКО он. Единственное, что он ловил, дефектом не
является.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from finengine.skills.news import classify
from finengine.skills.news.models import NewsItem

# Форма снята с живого отказа: буквальный перевод строки внутри значения.
_WITH_NEWLINE = '''[{"id": 0, "is_relevant": true, "category": "corporate",
  "region": "US", "importance": 0.5,
  "one_sentence_ru": "Первая строка
вторая строка", "why_important": "почему", "confidence": 0.9}]'''


def _item() -> NewsItem:
    return NewsItem(
        source="РБК", source_id="rbc", source_type="tier1", region="US",
        source_weight=0.7, title="t", snippet="", url="https://x/1",
        published_at=datetime.now(timezone.utc), raw_hash="h1",
    )


def test_the_payload_really_is_invalid_json():
    """Проверка предпосылки: без послабления такой ответ действительно
    отвергается целиком. Иначе сторож ниже охраняет пустоту."""
    with pytest.raises(json.JSONDecodeError):
        json.loads(_WITH_NEWLINE)


def test_a_stray_newline_does_not_destroy_the_batch():
    """Отрицательный контроль: вернул `json.loads(text)` без strict=False —
    краснеет. Именно так 199 отобранных новостей превратились в «0 компаний»."""
    got = classify._parse_response(_WITH_NEWLINE, [_item()])
    assert len(got) == 1, "один перевод строки снова уронил весь разбор"
    assert "Первая строка" in got[0].one_sentence_ru


def test_unparseable_answer_is_an_error_not_an_empty_digest():
    """Отрицательный контроль: вернул `return []` — краснеет.

    Пустой выпуск с пометкой «ошибок нет» никто не перепроверит: он читается
    как «сегодня новостей не было».
    """
    with pytest.raises(ValueError, match="не разобран"):
        classify._parse_response("{это вообще не json", [_item()])

