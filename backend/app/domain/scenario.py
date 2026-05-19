from __future__ import annotations

from enum import Enum


class ScenarioObject(str, Enum):
    O1 = "O1"  # Vendor
    O2 = "O2"  # Regulation
    O3 = "O3"  # RAI (Request for Additional Information)
    O4 = "O4"  # Relation / cross-reference


class ScenarioDepth(str, Enum):
    D1 = "D1"  # Overview
    D2 = "D2"  # Technical
    D3 = "D3"  # Rationale
    D4 = "D4"  # Formal / clause-level
