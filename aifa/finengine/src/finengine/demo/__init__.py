"""Demo snapshots — pre-frozen reports for sales demos.

Layout (in repo, copied into the image at /app/demo/):
  deploy/demo/<slug>/
      meta.json        # {slug, title, description, company_name, period_current, period_prev, unit}
      document.pdf     # original PDF
      extraction.json  # {metrics: [...], meta: {...}}  ← from a real run
"""
