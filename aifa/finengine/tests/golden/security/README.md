# tests/golden/security/

Эталонный набор враждебных входов для C0.1/C8 (`docs/build-plan/03-metodologiya-cikla-sreza.md`):
внедрение инструкций через документ, SSRF/обход пути через URL, подмена
чисел и обход доказательной базы, плюс маркеры для проверок, которых пока
нет в коде (циклы в графе, отравление памяти) — они существуют как критерий
на будущее, а не как проходящий сегодня тест.

Внешний контент — это данные, а не инструкции (POLICY.md §1). Ничего из
этого набора не исполняется этой сессией — атомов и `GraphExecutor`,
которые должны безопасно обработать эти случаи, ещё нет (Срез 1 и далее).

## `cases.json`

Список объектов `{id, kind, payload, expected_safe_behavior, detail}`. Виды
(`kind`): `prompt_injection_in_document_text`, `ssrf_url`, `path_traversal`,
`number_substitution`, `evidence_bypass`, `secret_leak_attempt`,
`infinite_chain_marker`, `memory_poisoning_marker`.

`payload` — либо строка (текст/URL), либо объект, когда сценарию нужно
несколько полей (например, `number_substitution` хранит и цитату, и
заявленное число, чтобы будущий тест сверял их напрямую).

## `files/`

Два реальных бинарных фикстура, сгенерированных `make_files.py` (перегенерировать:
`.venv/Scripts/python.exe tests/golden/security/make_files.py`):

* `corrupted.pdf` — валидный PDF-заголовок, битое тело, без xref/trailer.
  Не шелл-код — просто структурно повреждённый файл для проверки, что парсер
  сообщает ошибку, а не падает процессом или зависает.
* `ratio_bomb.zip` — 10 КБ сжатого, 10 МБ распакованного (нули, коэффициент
  ~1000×). Достаточно, чтобы проверить срабатывание лимита на коэффициент
  сжатия, недостаточно, чтобы реально что-либо исчерпать.

## Статус

20 текстовых/URL-кейсов + 2 бинарных фикстура. `tests/test_golden_security_set.py`
проверяет форму набора (не поведение будущих атомов).

**Срез 7 (A5, получение отчётности из интернета)** — первое реальное
поведенческое покрытие поверх формы набора:

- `sec-004`/`sec-005`/`sec-006` (`ssrf_url`, сетевой уровень) —
  `tests/document_acquisition/test_url_safety.py::test_golden_ssrf_cases_are_rejected_at_network_level`;
- `sec-007`/`sec-008` (`ssrf_url`, поддомен-подделка регулятора) —
  `tests/document_acquisition/test_policy.py::test_golden_domain_spoof_cases_are_not_trusted_as_official`;
- `sec-009`/`sec-010`/`sec-011` (`path_traversal`) —
  `tests/document_acquisition/test_registration.py::test_golden_path_traversal_cases_do_not_escape_the_storage_key`.

Остальные 12 кейсов (`prompt_injection_in_document_text`, `number_substitution`,
`evidence_bypass`, `secret_leak_attempt`, `infinite_chain_marker`,
`memory_poisoning_marker`) вне области Среза 7 — их поведенческая проверка
остаётся заготовкой на будущее, как и было.
