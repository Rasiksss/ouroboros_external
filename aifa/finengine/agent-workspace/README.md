# agent-workspace — нативные OpenCLAW-навыки агента AIFA CLAW

Source-of-truth для **SKILL.md-навыков** агента `fa-claw`. Это нативный слой
OpenCLAW: тонкие плейбуки, которые учат модель, КОГДА какой инструмент звать и
как подать результат. Тяжёлая логика живёт не здесь, а в **MCP-тулах**
(`finengine.mcp_server` — отдельный сервис; см. `src/finengine/mcp_server/`).

```
skills/
  aifa-finance/SKILL.md          — базовая опер-процедура: намерение → инструмент, политика уточнений, дефолты
  aifa-extract-metrics/SKILL.md  — fe_extract_metrics: сырые показатели (НЕ анализ)
  aifa-analyze/SKILL.md          — fe_analyze: выводы (general=быстро / chains=глубоко)
  aifa-factor-analysis/SKILL.md  — factor_analysis: план-факт по факторам
  aifa-knowledge-base/SKILL.md   — kb_add_url / kb_search: база знаний чата
```

(Уже на гейтвее живут `ir-calendar` и `ir-daily-digest` — переиспользуются как есть.)

## Деплой (Фаза 3 — switchover)
Синкаются в воркспейс агента на гейтвее и включаются в его skills-allowlist:
```
rsync -a agent-workspace/skills/  root@<gw>:/opt/fa-claw-agent/workspace/skills/
# + в openclaw.json: agents.list[fa-claw].skills += [aifa-finance, aifa-extract-metrics, …]
# + mcp.servers.finengine = { url, transport: "streamable-http" }
```
⚠️ Гейтвей общий (на нём и банковский агент) — менять конфиг осторожно/обратимо.

## Принцип
- **Тул** = функция (в MCP-сервере) — делает работу.
- **Навык** = инструкция (здесь) — учит, когда тул звать и как показать результат.
- Если тул вернул поле `*_markdown` — выводить как есть (verbatim), не переписывать.
