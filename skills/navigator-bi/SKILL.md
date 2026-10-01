---
name: navigator-bi
description: Query financial and management-reporting data from Navigator BI via the `navigator_bi_search` tool. Use this skill whenever the user asks for any numeric financial fact — PL, income, balance, credits, deposits, OPEX, KPIs, plan or forecast values — for any division, block, product, or period, even if Navigator is not mentioned by name. Never answer such questions from memory; always follow the catalog → reference → schema → query workflow.
version: 1.1.0
type: extension
runtime: python3
entry: plugin.py
permissions: [tool, net, read_settings]
env_from_settings:
  - NAVIGATOR_BI_BASE_URL
  - NAVIGATOR_BI_MODEL_IDS
  - NAVIGATOR_BI_SBER_PDI
  - NAVIGATOR_BI_EMAIL
  - NAVIGATOR_BI_TIMEOUT_SEC
  - NAVIGATOR_BI_TOTAL_TIMEOUT_SEC
when_to_use: User asks about financial results or operational finance metrics (PL, net income, balance sheet, credits, deposits, OPEX, KPIs, plans, forecasts) by period, division, block, or product — anything that may be covered by a connected Navigator BI model.
---

# Navigator BI

Use `navigator_bi_search`, not memory, for any numeric financial fact.

This skill is universal: it does not assume which models (витрины) are connected. The catalog of models is discovered at runtime, and model-specific knowledge lives in per-model reference files. Concrete model, division, and indicator names in this document are illustrative examples only.

## Workflow (mandatory order)

For every new data task:

1. **List the catalog.** Call `mode="schema"` without naming a model. This is a listing call: it returns metadata only — for each configured model, its exact `sName`, its `nID`, and `metadata_status`. There are no business descriptions and no data rows at this step. Only models whose `nID` is listed in `NAVIGATOR_BI_MODEL_IDS` appear; this catalog defines the full scope of what you can answer.
2. **Select a model by name, confirm by reference.** Build a short candidate list by matching the user's question against `sName` values from the catalog. If exactly one model fits, proceed. If several plausibly fit — including model families whose names differ only by a suffix indicating value type or comparison basis (illustrative: «… с учетом корректировок к бизнес-плану» vs «… YoY») — read `references/<nID>.md` for every candidate and choose by the model's purpose (fact/plan questions vs year-over-year dynamics); a name suffix is a hint, not a guarantee. Ask the user only when the ambiguity survives reading the references.
3. **Read the model reference, if it exists.** Check the model reference via `skill_read(skill="navigator-bi", path="references/<nID>.md")`, where `<nID>` is the model ID from the catalog. It holds model-specific knowledge: metric synonyms, units and aggregation rules, canonical division names and codes, block aliases and hierarchy warnings, fact-vs-plan model choice. A missing file is not an error — proceed with schema alone, and prefer asking the user over guessing on any ambiguity.
4. **Get the model columns.** Call `mode="schema"` for the chosen model. Treat the response as the allowed catalog of columns and `allowed_operators`; do not invent or assume column names.
5. **Query.** Call `mode="query"` with a bounded `plan` (template below), using the exact `sName` from the catalog.
6. **Answer** in business Russian from `rows`.

Catalog and column metadata (steps 1 and 4) may be reused within one conversation; re-fetch if an expected model is missing or `needs_clarification` suggests schema drift. Data values may never be reused — see Data freshness.

## Scope

The supported scope is whatever the runtime catalog returns — nothing more. If the user's question is not covered by any configured model, say so politely, explain that this data is not connected, and do not query or substitute numbers from general knowledge. Never enumerate "what Navigator can do" from memory — describe the scope from a fresh catalog call.

## Role: extraction, not analysis

You extract and present Navigator BI data; you are not an analyst.

Forbidden:
- inventing numbers or filling gaps from general knowledge;
- computing aggregates without the `calculator` tool;
- drawing conclusions about causes, trends, or risks unless the user explicitly asks to compare specific periods you have already fetched;
- building your own forecast scenarios (base/optimistic/pessimistic) or assigning probabilities;
- citing macro factors, expert opinions, or "forecast methodologies" not present in Navigator responses;
- modifying source data in any way;
- saving forecast methodologies or unverified numbers to memory.

## Data freshness

- Every numeric value you present must originate from a `mode="query"` response in the current conversation. One query that returns all needed rows is fine — the rule is provenance, not one call per number.
- Every new question involving metric values requires a fresh `mode="query"` call, even when similar numbers appeared earlier in the dialogue. **Only exception:** the user explicitly says to use the same data as before and the period/filters fully match — then you may repeat the last answer, confirming that parameters did not change.
- Take forecast and plan values only from Navigator `rows` for the requested period. Do not extrapolate, blend sources, or produce year-end projections that Navigator did not return.
- If a value did not come from Navigator, label it **`[мое допущение]`** and never present it as a verified fact.
- If the tool returns a `⚠️ TOOL_ERROR:` line, report the access, configuration, or service issue briefly; do not substitute plausible-looking numbers.

## Finance rules

- Ambiguous metric names must be resolved before querying: e.g. «доход» may mean ЧПД, ЧКД, операционный доход, or another metric. Resolve via the model reference file, or ask the user.
- Use ready Navigator values for month/QTD/YTD when available; do not recompute them from lower periods.
- If Navigator has no ready value for the requested grain (e.g. YTD is absent), say so and offer the grains that do exist. Do not derive the missing aggregate on your own initiative. Only if the user explicitly asks to sum components: this is allowed solely for additive flow metrics (confirmed as such by the model reference), via `calculator`, and the result must be presented as computed from lower periods — not as a Navigator figure.
- Do not sum ratios or stock balances. Do not add a total together with its components.
- Present numbers exactly as returned in `rows`, with units from the model or its reference. Do not round, rescale (млн ↔ млрд), or convert currencies unless the user asks; if asked, convert via `calculator`.
- For calculations, use only available tools such as `calculator`; do not invent unavailable tools.

## Calculations (mandatory calculator)

Any calculation on Navigator data — sum, average, difference, min, max, share, period comparison — must use the `calculator` tool (enable it with `enable_tools` when needed).

Forbidden: eyeball estimates, "visual" comparison, approximate values (`~151,6`), or mental arithmetic.

- **Min/max:** `min(v1, v2, ...)` / `max(...)` with numeric literals taken from `rows`.
- **Average:** `(a + b + ... + n) / count` via one or two `calculator` calls.
- **Variance/share/rank:** fetch the needed Navigator rows first, then calculate with `calculator`; do not compute by sight.
- You may name the month of a minimum/maximum from `rows`, but the numeric min/max must come from `calculator` or directly from a Navigator row value.

## Analysis requests

When the user asks for "analysis", "dynamics", or "compare":
1. Clarify the exact metrics, model, division, and periods.
2. If periods are open-ended («за все периоды»), ask for concrete date boundaries.
3. If an abbreviation or metric is ambiguous, ask before `query`.
4. Fetch each period with its own `navigator_bi_search` call and present the results side by side — without subjective commentary on causes or risks.

Safe response example: «Могу выгрузить выручку за оба периода и показать их рядом. Уточните, пожалуйста, какие периоды сравнить — например, декабрь 2025 и декабрь 2024.»

## Query plan template

```json
{
  "mode": "query",
  "plan": {
    "model": "<exact sName from the catalog>",
    "conditions": [
      {"column": "Наименование подразделения", "operator": "=", "value": "Сбербанк"},
      {"column": "Дата", "operator": ">=", "value": "2025-12-01"}
    ],
    "limit": 20
  }
}
```

The model, column names, and values above are illustrative — take real ones from the catalog, the model schema, and the model reference file.

## Filter discipline

- Use only operators from schema `allowed_operators`.
- At least one filter condition is required; empty `conditions` are rejected.
- Combine conditions with AND; no OR trees.
- For `in` / `not in`, pass array values in `plan.conditions[].value`.
- Respect limits: default 20 rows, max 100.
- If a model is missing from the catalog, it is not configured in `NAVIGATOR_BI_MODEL_IDS` — do not attempt to query it.

## Response handling

The tool returns JSON for successes and soft clarification requests; recognized hard failures (settings, network, service) come back as a `⚠️ TOOL_ERROR: <detail>` string, not JSON.

| Tool result | Agent action |
|-------------|--------------|
| JSON `status: "ok"` | Summarize financial facts in business language; name the model when helpful; use row values, not wire/query metadata |
| JSON `status: "not_found"` | Ask the user to refine division, date, or indicator |
| JSON `status: "needs_clarification"` | Fix the plan per the tool's message; re-call schema if needed |
| `⚠️ TOOL_ERROR: <detail>` | Report the access, configuration, or service issue briefly; do not substitute values from memory and do not invent plausible numbers |

If the response indicates truncation (`truncated`, `has_more`), never present the list as complete: either narrow the query (tighter filters, shorter period, `limit` up to 100) or tell the user in business language that the list is partial and offer to refine. Do not mention the wire flags themselves.

## User-facing presentation

Respond in Russian. Translate evidence into business language: division, period, indicator, value, unit.

Do not reveal to the user: internal field IDs (`nID`, opaque field IDs, numeric model IDs); wire operators; `applied_conditions`; `plan`/`conditions` structure; endpoints or base URLs; `backend`/`tool`; `metadata_status`; `plan_notes`; response metadata such as `truncated`, `has_more`, `returned`, or `limit`; empty technical fields in rows; raw wire column names, `sDataType`, `sExample`, or `codeName` from schema. Use human-readable indicator and division names from row values, not the wire catalog; schema column names are for building the tool `plan` only.

You may show Navigator model names (`sName`) when they help the user understand the answer, plus indicator values, divisions, and dates from `rows`.

Do not show formulas in code blocks, self-audit tables, emoji-heavy post-mortems, or lengthy apologies about past mistakes. On errors, state the issue briefly and offer to refine parameters or retry.

Do not offer to save forecast methodologies or query recipes to memory.

For bulk results, do not build a table when key user-facing values are empty; never replace blanks with `скрыто`, `—`, `null`, or guesses.

## Division disambiguation

Never guess the mapping between informal and official division names (illustrative example: «Сбер» vs «Сбербанк» vs «Группа Сбербанка России»).

- The model reference is the source of canonical division names and aliases; the catalog and `mode="schema"` do not return division value lists.
- Normalize the user's wording only via the reference of the chosen model: one matching canonical name — use it in `query`; several matches — show the options and let the user choose; no match in the reference — ask for the exact official name and do not guess.
- When distinct official names exist (e.g. «Сбербанк» vs «Группа Сбербанка России»), filter explicitly on the division column; never assume one stands for the other.
- If the model requires a division and the user did not specify one, ask which division to use before calling `query`.

## Worked example (illustrative)

All names, IDs, columns, and values below are examples; real ones come from your catalog.

User: «Какая чистая прибыль Сбербанка за декабрь 2025?»

1. `mode="schema"` without a model → catalog includes `{sName: "Финансы Группы.PL", nID: 1000000956, metadata_status: "ready"}`.
2. The name matches the question → candidate model «Финансы Группы.PL».
3. `skill_read(skill="navigator-bi", path="references/1000000956.md")` → «чистая прибыль» maps to indicator «Чистая прибыль», units млрд руб.; official divisions include «Сбербанк» and «Группа Сбербанка России».
4. `mode="schema"` for «Финансы Группы.PL» → columns «Наименование подразделения», «Дата», «Показатель», «Значение»; operators `=`, `>=`, `<=`, `in`.
5. The user's wording exactly matches the official name «Сбербанк» → no clarification needed. `mode="query"` with conditions: подразделение = «Сбербанк», Дата = «2025-12-01», Показатель = «Чистая прибыль», limit 20.
6. Answer: «Чистая прибыль Сбербанка за декабрь 2025 — <значение из rows> млрд руб. (модель «Финансы Группы.PL»).»
