"""Прогон извлекателя AIFA CLAW по golden-документам Finengine.

Локальный запуск полноконтекстного движка (extract_stream_finengine_dev) →
JSON-предсказания по контракту скорера. Скоринг — отдельно, score.py.

⚠️ ПАЙПЛАЙН КАК У АГЕНТА: движок кормится текстом **IDP-OCR** (сильный на
таблицах, прод-дефолт), а НЕ pymupdf. Берём готовый IDP-OCR из golden
`json/<doc>.json` (`ocr_result.text` — уже пагинированный `===PAGE` markdown)
и кладём его в sidecar; pymupdf-чанки НЕ добавляем, иначе движок предпочёл бы
их (`pdf_text_for_prompt = paginated_text-из-чанков or sidecar`). Это тот же
вход, что benchmark_v2 (input_mode=json) и что видит агент после IDP-загрузки.

Нужен OPENROUTER_API_KEY в .env (движок зовёт модель; с Мака OpenRouter
доступен). `git lfs pull` — иначе golden json будут указателями.

Примеры:
  .venv/bin/python benchmarks/run_extract.py --cell ifrs_nf_ru --docs arendadata-2024.pdf
  .venv/bin/python benchmarks/run_extract.py --cell ifrs_bank_ru --limit 3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
os.chdir(REPO)  # settings грузит .env из cwd

from bench_cells import CELLS, GOLDEN_DIR  # noqa: E402

from finengine.models.document import (  # noqa: E402
    Chunk, CompanyType, DocType, DocumentMeta, ExtractedItem,
)

# Self-contained: golden PDF вендорены под LFS (benchmarks/vendor). `git lfs
# pull` нужен, чтобы pdf были реальными, а не указателями. Обновляется
# sync_vendor.sh.
DEFAULT_FINENGINE_REPO = Path(__file__).resolve().parents[1] / "benchmarks" / "vendor"
_EMPTY_RETRIES = 3  # как в fe_extract_metrics: движок изредка отдаёт пусто


class BenchIndex:
    """Минимальный duck-type DocumentStore: только то, что читает движок."""

    def __init__(self) -> None:
        self._docs: dict[str, DocumentMeta] = {}
        self._chunks: list[Chunk] = []

    def add_document(self, meta: DocumentMeta, chunks: list[Chunk]) -> None:
        self._docs[meta.doc_id] = meta
        self._chunks.extend(chunks)

    def get_document(self, doc_id: str) -> DocumentMeta | None:
        return self._docs.get(doc_id)

    def get_chunks_by_type(
        self, chunk_type: str, doc_id: str | None = None,
    ) -> list[ExtractedItem]:
        out: list[ExtractedItem] = []
        for chunk in self._chunks:
            if chunk.chunk_type != chunk_type:
                continue
            if doc_id and chunk.doc_id != doc_id:
                continue
            out.append(ExtractedItem(
                content=chunk.content,
                score=1.0,
                chunk_type=chunk.chunk_type,
                metadata={
                    **chunk.metadata,
                    "chunk_id": chunk.chunk_id,
                    "doc_id": chunk.doc_id,
                    "page": chunk.page,
                    "section": chunk.section,
                    "speaker": chunk.speaker,
                },
            ))
        return out

    def search(self, *args, **kwargs) -> list[ExtractedItem]:  # noqa: D102
        return []  # полноконтекстный путь поиском не пользуется


def _cell_registry(cell: str) -> dict:
    """Реестр ячейки ТЕМ ЖЕ загрузчиком, что и живой путь.

    Раньше здесь читался json напрямую и брались только имена показателей, а
    строки (синонимы, подсказки) и формулы движок подставлял свои — из
    сгенерированного `data/metrics.json`. То есть бенчмарк мерил ДРУГОЙ промпт,
    чем видит пользователь: с 01.08 живой путь собирает промпт из реестра ячейки
    (#147), и без этой прокидки любой замер «после» ничего не доказывал бы про
    прод. Имя ячейки в бенчмарке совпадает с profile_id — это и есть ключ.
    """
    from finengine.skills.metric_schema import registry_for_profile

    return registry_for_profile(cell)


def _cell_enums(cell: str) -> set[str]:
    return {m["enum"] for m in _cell_registry(cell)["extractable"]}


def _idp_sidecar_text(pdf_path: Path) -> tuple[str, int]:
    """IDP-OCR текст документа из golden json/<stem>.json (тот же вход, что у
    агента после IDP-загрузки). Возвращает (пагинированный текст, число страниц).
    """
    json_path = pdf_path.parent.parent / "json" / f"{pdf_path.stem}.json"
    if not json_path.is_file():
        raise FileNotFoundError(
            f"нет IDP-OCR json: {json_path} (сделай `git lfs pull`)")
    idp = json.loads(json_path.read_text(encoding="utf-8"))
    ocr = idp.get("ocr_result") or {}
    text = ocr.get("text") or ""
    if "===PAGE" not in text:
        raise ValueError(
            f"{json_path.name}: ocr_result.text не пагинирован (===PAGE нет)")
    return text, len(ocr.get("pages") or [])


async def _extract_one(
    pdf_path: Path, cell: str, model_id: str | None, *, with_segments: bool = False,
) -> dict:
    from finengine.config import settings

    cfg = CELLS[cell]
    sidecar_text, n_pages = _idp_sidecar_text(pdf_path)
    meta = DocumentMeta(
        filename=pdf_path.name,
        doc_type=DocType.IFRS_REPORT,
        company_name=pdf_path.stem,
        company_type=(CompanyType.BANK if cfg["company_type"] == "bank"
                      else CompanyType.NON_FINANCIAL),
        language=cfg["language"],
        file_path=str(pdf_path),
    )
    # Пустой индекс (без pymupdf-чанков) → движок берёт IDP sidecar, а не
    # чанки (pdf_text_for_prompt = paginated_text-из-чанков or sidecar).
    index = BenchIndex()
    index.add_document(meta, [])

    docs_dir = Path(settings.docs_dir)
    docs_dir.mkdir(parents=True, exist_ok=True)
    sidecar = docs_dir / f"{meta.doc_id}.txt"
    sidecar.write_text(sidecar_text, encoding="utf-8")

    try:
        return await _run_engine(meta, cfg, cell, index, model_id,
                                 n_chunks=n_pages, pdf_name=pdf_path.name,
                                 with_segments=with_segments)
    finally:
        sidecar.unlink(missing_ok=True)


async def _run_engine(meta, cfg, cell, index, model_id, *, n_chunks, pdf_name,
                      with_segments: bool = False):
    from finengine.skills.bulk_extractor import extract_stream_finengine_dev

    metrics: list[dict] = []
    done_info: dict = {}
    for attempt in range(_EMPTY_RETRIES):
        metrics, done_info = [], {}
        async for ev in extract_stream_finengine_dev(
            meta.doc_id, cfg["company_type"], index=index,
            model_id=model_id,
            metric_enums=_cell_enums(cell),
            # Реестр ячейки — тот же вход, что у живого пути (#147). Без него
            # бенчмарк мерил бы промпт, которого в продукте больше нет.
            registry_override=_cell_registry(cell),
            # Сегменты по умолчанию ВЫКЛЮЧЕНЫ: они удваивают вызовы и в
            # обычном замере не нужны. Но разметка сегментов в golden ЕСТЬ
            # (колонки segment_name/segment_type, ~690 строк в 15 документах),
            # и скорер их считает отдельным скоупом — так что замерить их можно,
            # это вопрос флага, а не отсутствия GT.
            include_segments=with_segments,
            include_consolidated_group=False,
        ):
            et = ev.get("type")
            if et == "metric" and ev.get("found"):
                # Сегментные строки едут со своим именем сегмента: скорер
                # сопоставляет их с GT по ключу (документ, показатель, период,
                # СЕГМЕНТ), и пустое имя склеило бы разные сегменты в один.
                pvs = [
                    {"period": pv.get("period", ""), "value": pv.get("value")}
                    for pv in (ev.get("period_values") or [])
                    if pv.get("value") is not None
                ]
                if pvs:
                    metrics.append({
                        "type": "metric",
                        "enum": ev.get("enum", ""),
                        "segment": ev.get("segment") or "",
                        "dimension": ev.get("dimension") or "",
                        "unit": ev.get("unit", ""),
                        "period_values": pvs,
                    })
            elif et == "done":
                done_info = {k: ev.get(k) for k in
                             ("period_current", "period_prev", "company_name")}
        if metrics:
            break
        print(f"    пустой результат, ретрай {attempt + 1}/{_EMPTY_RETRIES}")

    return {
        "dataset": cfg["dataset"],
        "pdf_name": pdf_name,
        "metrics": metrics,
        "_meta": {**done_info, "cell": cell, "n_chunks": n_chunks},
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cell", required=True, choices=sorted(CELLS))
    ap.add_argument("--docs", default="",
                    help="имена pdf через запятую (по умолчанию — все в ячейке)")
    ap.add_argument("--limit", type=int, default=0, help="первые N документов")
    ap.add_argument("--model", default="", help="model_id движка (дефолт движка)")
    ap.add_argument("--finengine-repo", type=Path, default=DEFAULT_FINENGINE_REPO,
                    help="корень с golden-pdf (дефолт: benchmarks/vendor)")
    ap.add_argument("--out", type=Path, default=REPO / "benchmarks" / "preds")
    ap.add_argument("--segments", action="store_true",
                    help="извлекать и сегментные разрезы (дороже: отдельные вызовы "
                         "движка); скорить их — score.py --segments")
    args = ap.parse_args()

    golden = args.finengine_repo / "tests" / "test_benchmark" / "data" / "golden" \
        / GOLDEN_DIR[args.cell] / "pdf"
    if not golden.is_dir():
        sys.exit(f"нет папки {golden} — проверь --finengine-repo / git lfs pull")

    pdfs = sorted(golden.glob("*.pdf"))
    if args.docs:
        wanted = {d.strip() for d in args.docs.split(",")}
        pdfs = [p for p in pdfs if p.name in wanted]
        missing = wanted - {p.name for p in pdfs}
        if missing:
            sys.exit(f"не нашёл в ячейке: {sorted(missing)}")
    if args.limit:
        pdfs = pdfs[: args.limit]
    if not pdfs:
        sys.exit("нечего прогонять")

    out_dir = args.out / args.cell
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"ячейка {args.cell}: {len(pdfs)} док(ов) → {out_dir}")

    for i, pdf in enumerate(pdfs, 1):
        t0 = time.time()
        print(f"[{i}/{len(pdfs)}] {pdf.name} …", flush=True)
        try:
            preds = await _extract_one(pdf, args.cell, args.model or None,
                                       with_segments=args.segments)
        except Exception as e:  # noqa: BLE001 — один док не валит батч
            print(f"    ОШИБКА: {e}")
            continue
        out = out_dir / f"{pdf.stem}.json"
        out.write_text(json.dumps(preds, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        print(f"    метрик: {len(preds['metrics'])}, "
              f"{time.time() - t0:.0f}с → {out.name}")


if __name__ == "__main__":
    asyncio.run(main())
