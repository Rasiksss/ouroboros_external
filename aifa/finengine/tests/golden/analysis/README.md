# tests/golden/analysis/ — СХЕМА И CANDIDATE CORPUS

C0.1 требует не менее 30 сквозных кейсов с утверждённой доказательной базой
и рубрикой качества вывода. В `cases/` находятся 30 детерминированных cases,
которые ссылаются на evidence IDs из extraction candidate corpus и требуют
точности, evidence alignment, логической связности и отсутствия рекомендаций.
Они не генерировались проверяемой моделью. Owner/analyst review всё ещё нужен,
чтобы слово «утверждённой» считать закрытым, а не подменять машинной сборкой.

## Формат (когда будет заполняться)

`cases/<case_id>.json`:

```json
{
  "case_id": "string",
  "company": "string",
  "goal": "строка запроса пользователя",
  "evidence_ids": ["ссылки на факты из tests/golden/extraction/"],
  "expected_judgment": {"concept": "string", "verdict": "string"},
  "expected_explanation_covers": ["пункты, которые объяснение обязано упомянуть"],
  "rubric": {"criterion": "вес/пороговое условие приёмки"}
}
```

Требует, чтобы `tests/golden/extraction/` уже была заполнена — доказательная
база здесь ссылается на факты оттуда, а не дублирует числа заново.
