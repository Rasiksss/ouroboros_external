"""Per-component launch scripts for the multi-user deployment.

One process per component is the shape this package is for. What it hands each component
is still the in-process objects the single-process server composes, so nothing here is a
deployment yet: replacing them is a change to one module per component —
``<component>/dependencies.py`` — and to nothing else.

``server.py`` stays the composition root of the single-process profile and is untouched by
anything here.
"""
