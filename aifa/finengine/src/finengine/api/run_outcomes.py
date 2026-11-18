"""Чтение исхода прогона: по нонсу и по документу (#276, шаг 1).

ЗАЧЕМ РУЧКА. Запись исхода в базу (`store/runs.py`) без чтения — это склад без
двери: данные есть, а показать их пользователю нечем. Сегодня единственный
способ увидеть результат прогона — держать вкладку открытой на всём его
протяжении; 18.08 два прогона пропали ровно потому, что слушателя не стало.

ЧЕГО ЗДЕСЬ НЕТ СОЗНАТЕЛЬНО:
  * СБОРКИ ЭКРАНА. Ручка отдаёт СТРОКУ ПРОГОНА как она записана, а не готовую
    таблицу. Раскладка роли и markdown — производные: они пересобираются из
    строк и плана роли (`extract_result`), и сохранённая копия разошлась бы с
    данными при первой правке `roles/layouts.json` — молча, ровно как
    расходились два рисовальщика таблицы;
  * ЧУЖИХ ПРОГОНОВ. `user_id` уходит в WHERE обоих запросов, а не проверяется
    после выборки: «нет такого исхода» и «исход не твой» обязаны быть
    неразличимы, иначе по коду ответа перебирается чужой список нонсов.

ВЫКЛЮЧЕННОЕ ХРАНИЛИЩЕ отвечает 404, как и отсутствующий прогон: это дефолт, а
не поломка — фронт остаётся на своём localStorage, как и до этой правки.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from finengine.auth.db import User
from finengine.auth.dependencies import get_current_user
from finengine.store import runs

router = APIRouter()


def _as_json(outcome: runs.RunOutcome) -> dict:
    """Строка прогона наружу. Владельца НЕ отдаём: спрашивающий и есть он."""
    return {
        "nonce": outcome.nonce,
        "kind": outcome.kind,
        "status": outcome.status,
        "project_id": outcome.project_id,
        "session_id": outcome.session_id,
        "run_id": outcome.run_id,
        "doc_id": outcome.doc_id or "",
        "payload": outcome.payload,
        "screen": outcome.screen,
        "error": outcome.error,
        "started_at": outcome.started_at,
        "finished_at": outcome.finished_at,
        "rev": outcome.rev,
    }


@router.get("/runs/by-doc/{doc_id}")
def latest_run_of_document(
    doc_id: str,
    kind: str = Query("", description="extract | analyze; пусто — любой вид"),
    user: User = Depends(get_current_user),
) -> dict:
    """Последний ЗАВЕРШЁННЫЙ прогон по документу. Идущий не отдаём: он не исход."""
    outcome = runs.get_run_store().latest_for_doc(doc_id, user_id=user.id, kind=kind)
    if outcome is None:
        raise HTTPException(status_code=404, detail="завершённых прогонов по документу нет")
    return _as_json(outcome)


@router.get("/runs/{nonce}")
def run_outcome(nonce: str, user: User = Depends(get_current_user)) -> dict:
    outcome = runs.get_run_store().load(nonce, user_id=user.id)
    if outcome is None:
        raise HTTPException(status_code=404, detail="исход прогона не найден")
    return _as_json(outcome)
