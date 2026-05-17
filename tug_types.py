"""
tug_types.py -- Gedeelde type-aliassen voor publieke API's
"""

from typing import Any, Callable

# Een GeoJSON Feature zoals geretourneerd door WFS/PDOK/DUO en intern doorgegeven.
# Heeft minimaal {"type": "Feature", "geometry": {...}, "properties": {...}}.
Feature = dict[str, Any]
FeatureList = list[Feature]

# Log-callback die door alle bronnen-functies wordt geaccepteerd.
# Tekst-in, niets-uit; impl. is doorgaans een nested closure die in
# een lokale lijst log_regels accumuleert.
LogFn = Callable[[str], None]

# Resultaat-types voor signaleringsfuncties.
SignaalResultaat = dict[str, list[dict[str, Any]]]

# Een classificatie-context dict, zoals geretourneerd door _bouw_classificatie_context.
ContextDict = dict[str, Any]
