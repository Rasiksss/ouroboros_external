"""AIFA baseline — maximum-fidelity port of the upstream extractor.

Differences from `aifa/` (the DEV version):
- Uses the IDP-OCR cloud service (instead of pdfplumber) to convert PDF
  → markdown tables. Address comes from settings.idp_ocr_url.
- Uses AIFA's exact extraction prompt templates (extract_from_table.py)
  verbatim — same wording, same JSON-or-CSV format constraints.
- The only deliberate deviation is the LLM backend: AIFA's GigaChat is
  swapped for whatever model is configured for our project (Qwen by
  default). The prompt is model-agnostic, so this is safe.

When the upstream CatBoost head from `Репа в репе` is available (Git LFS
binary), the heuristic ranker in `aifa.scorer` should be replaced with
the model. Until then this baseline shares the same heuristic — what is
"baseline" here is the OCR + prompt fidelity, not the ranker.
"""
# Движок aifa_baseline снят 21.08.2026 (легаси, пятая копия реестра, числа
# в сто раз меньше). В пакете остаётся то, чем пользуется БОЕВОЙ путь.

__all__: list[str] = []
