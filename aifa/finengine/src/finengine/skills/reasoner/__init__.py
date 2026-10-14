"""Reasoner — metrics-table helper for the analysis prompt.

Historically this package hosted sequential CoT chains over extracted
metrics (byte-identical prompts with upstream `financier-reasoner`). Those
chains were removed together with the legacy chain-analysis endpoints; what
remains is `build_metrics_table_str` in `runner.py`, used by `fe_analyze`
(imported directly from the submodule).
"""
