"""Переспросы батчей доезжают до журнала прогона (21.08.2026).

ЦЕНА. Замер Северстали Q1-2025 (стенд, 20.08) переспросил батч 008 — и узнать
об этом по журналу было нельзя. Движок переспросы считает
(`stage2_retry_count`, `segment_stage2_retry_count`), но полная диагностика в
журнал не едет: `log_extract_run` кладёт в запись ВЫБОРКУ — ячейку, роль,
единицу, разрез стадий и потери шардирования. Счётчик оставался внутри одного
хода и умирал вместе с ним, а `extract_runs.jsonl` — единственное место, где
прогоны сравнивают между собой.

Отличие от `batches_failed`: там батч сорвался НАСОВСЕМ и оставил дыру в
таблице; здесь он ответил со второй-третьей попытки — таблица цела, но
заплачено временем и токенами. Попыток четыре, то есть незамеченными могут
пройти три лишних вызова на каждый батч.

⚠️ ЗДЕСЬ ПРОВЕРЯЕТСЯ ТОЛЬКО АРИФМЕТИКА ВЫДАЧИ — словарь собран руками, и
переименуй кто-нибудь ключ в движке, тесты ЭТОГО файла останутся зелёными
(проверено мутацией 21.08: `diag["stage2_retry_count"]` → любое другое имя).
То, что имена ключей и сам факт переспроса держатся ПРОГОНОМ ДВИЖКА, стережёт
`test_a_retried_batch_says_it_was_retried.py` — на той же мутации краснеет он.
"""
from __future__ import annotations

from finengine.skills import extract_result


def test_the_journal_learns_how_many_retries_the_run_paid_for():
    """Счётчик переспросов доезжает до записи прогона, а не только до `_diag`."""
    losses = extract_result.shard_losses(
        {"stage2_batch_count": 8, "stage2_retry_count": 1, "stage2_invalid_batches": []}
    )
    assert losses["batch_retries"] == 1, "переспросы снова не доехали до журнала"
    assert losses["batches_failed"] == 0, (
        "переспрос засчитан за сорвавшийся батч — это разные потери"
    )


def test_segment_retries_are_counted_apart_from_the_main_ones():
    """Сегментная стадия считается тоже — но СВОИМ числом при СВОЁМ знаменателе.

    Правило, заведённое в одной из двух стадий, — дефект: они правятся порознь
    и расходятся молча. Поэтому сегментные переспросы обязаны доезжать до
    журнала. Но складывать их с основными нельзя: `batches` — это только
    основная стадия, и сумма делится на чужой знаменатель. Числа примера дают
    ровно ту ошибку, ради которой правка: при НУЛЕ переспросов основной стадии
    доля выходила бы 3/8 = 37 %.
    """
    losses = extract_result.shard_losses(
        {
            "stage2_batch_count": 8,
            "stage2_retry_count": 0,
            "segment_stage2_batch_count": 3,
            "segment_stage2_retry_count": 3,
        }
    )
    assert losses["batch_retries"] == 0, (
        "сегментные переспросы снова считаются основной стадии — "
        f"доля выйдет {losses['batch_retries']}/{losses['batches']}"
    )
    assert losses["segment_batch_retries"] == 3, "переспросы сегментной стадии потерялись"
    assert losses["segment_batches"] == 3, (
        "сегментные переспросы отданы без своего знаменателя — делить не на что"
    )


def test_a_run_without_a_segment_stage_says_nothing_about_segment_batches():
    """Сегментной стадии не было — сегментных ключей нет вовсе, а не нули.

    «segment_batch_retries: 0» в журнале читается как «сегментные батчи были и
    не переспрашивали». Тот же довод, по которому схема без батчей отдаёт
    пустой словарь.
    """
    losses = extract_result.shard_losses({"stage2_batch_count": 8, "stage2_retry_count": 1})
    assert "segment_batches" not in losses, losses
    assert "segment_batch_retries" not in losses, losses


def test_a_schema_without_batches_reports_no_losses_at_all():
    """У полноконтекстной схемы батчей нет — пустой словарь, а не нулевой счётчик.

    «batch_retries: 0» в журнале читалось бы как «батчи были, переспросов не
    случилось», а батчей не было вовсе.
    """
    assert extract_result.shard_losses({"stage2_retry_count": 0}) == {}
