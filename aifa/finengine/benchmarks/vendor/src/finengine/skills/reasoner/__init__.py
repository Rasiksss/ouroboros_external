"""Reasoner — sequential CoT chains over extracted metrics.

Chain prompts are kept byte-identical with upstream `financier-reasoner`
(`Репа в репе/financier-reasoner/cot/prompts.py`); they pull `StepDescription`
from `mmar_carl`, AIRI's collaborative reasoning lib (installed via
git+https in pyproject.toml). The runner is our own minimal sequential
executor — it calls OpenRouter once per step and streams the synthesis.
"""
from mmar_carl import StepDescription  # noqa: F401

from finengine.skills.reasoner.runner import run_chain, run_full_synthesis  # noqa: F401
