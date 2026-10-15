# tests/golden/extraction/ — СХЕМА И MACHINE-VERIFIED CANDIDATE CORPUS

C0.1 (`docs/build-plan/03-metodologiya-cikla-sreza.md`) требует не менее 30
отчётов и не менее 300 подтверждённых фактов. В `cases/` находятся 30
отчётов и 300 фактов, детерминированно собранных из восстановленного vendor
snapshot: существующая GT-строка сопоставлена с буквальной числовой строкой
той же OCR-таблицы, а `document_version` равен SHA-256 исходного PDF.

Генератор не вызывает LLM. Полный source/path/hash mapping и статус проверки
зафиксированы в `../c0_manifest.json`. Corpus является machine-verified
candidate по явному решению владельца от 22.09.2026; требование исходной
формулировки о независимой ручной проверке каждого факта остаётся открытым
до owner/analyst review и не должно объявляться выполненным молча.

## Формат (когда будет заполняться)

`cases/<report_id>.json`:

```json
{
  "report_id": "string",
  "company": "string",
  "source_url": "string (реальный URL, не выдуманный)",
  "document_version": "string",
  "language": "ru|en",
  "facts": [
    {
      "fact_id": "string",
      "concept": "string (код показателя из реестра)",
      "value": 0.0,
      "unit": "string",
      "period_ref": "string",
      "basis": "string|null",
      "page": 0,
      "quote": "string — буквальная цитата страницы, подтверждающая value"
    }
  ]
}
```

Каждый факт обязан быть проверен человеком против реальной страницы
документа — `quote` существует именно для того, чтобы это можно было
перепроверить позже, а не поверить на слово записи.
