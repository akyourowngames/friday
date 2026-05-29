"""KING cognition substrate.

Local-first, config-driven cognitive modules that ride on top of the existing
memory graph and maintenance engine. Nothing here hardcodes phrases, routes by
keyword, or uses regex. Every threshold and weight is read from the markdown
control surface (`cognition/COGNITION_CONFIG.md`).

Modules:
- `situation`  : ambient signal fusion -> cognitive load / availability gate
- `cadence`    : per-node 24x7 activity rhythm + deviation scoring
- `episodes`   : narrative clustering of memories into episodes
- `proactive`  : candidate scoring with adaptive threshold, budget, novelty
"""

from .config import load_cognition_config, section_values
from .situation import SituationModel
from .cadence import CadenceModel
from .episodes import stitch_episodes
from .proactive import Candidate, ProactiveEngine

__all__ = [
    "load_cognition_config",
    "section_values",
    "SituationModel",
    "CadenceModel",
    "stitch_episodes",
    "Candidate",
    "ProactiveEngine",
]
