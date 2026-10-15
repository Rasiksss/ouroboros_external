# tests/golden/structures/ — СХЕМА И MACHINE-VERIFIED CANDIDATE CORPUS

C0.1 требует не менее 20 структурированных таблиц/раскрытий из реальных
отчётов. В `cases/` находятся 20 таблиц из восстановленного vendor snapshot;
строки адресуют буквальные строки OCR-таблиц и исходные PDF через общий
`../c0_manifest.json`. Это machine-verified candidate по решению владельца,
не заявление о завершённом независимом analyst review.

## Формат (когда будет заполняться)

`cases/<structure_id>.json`, десериализуется в `finengine.contracts.StructuredFact`:

```json
{
  "structure_id": "string",
  "company": "string",
  "source_url": "string",
  "table_kind": "segments|maturity|group|...",
  "page": 0,
  "rows": [{"...": "произвольные поля строки, как в реальной таблице документа"}]
}
```
