"""Shared helpers re-exported for matrix cells.

This package is a thin facade over the legacy bulk_extractor.py. Cells
should import everything they need from here, not from bulk_extractor
directly — that way Phase D cleanup (when we delete the legacy module)
only touches _shared/, not the cells themselves.

Subpackages:
- io: PDF/text sidecar I/O
- periods: period detection, normalization, ordering
- values: parse "verbatim" numeric strings and units
- company: company name / type / standard / year detection
- llm: thin wrapper over finengine.llm.openrouter

If a function lives at the boundary between two of these (e.g.
`_calculate_metrics` is value-shaped but registry-aware), it stays in
the cell, not here. Keep _shared/ small and stable.
"""
