"""Why each candidate transition was accepted or rejected.

Real-world tuning is impossible without this. When a lecture comes back with a
missed slide or a duplicate, the useful question is not "what are the
thresholds" but "which gate rejected the transition at 12:34, and by how much" -
so every candidate records each gate it was measured against, the value, the
limit, and whether it passed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.timecode import format_hms

__all__ = ["Gate", "Candidate", "Outcome", "BusyRegionReport"]


class Outcome:
    COMMITTED = "committed"
    REJECTED = "rejected"
    ABSORBED = "absorbed"


@dataclass(frozen=True)
class Gate:
    name: str
    value: float
    limit: float
    passed: bool
    #: ``">="`` when the value must reach the limit, ``"<"`` when it must stay
    #: below it. Only used for display.
    comparison: str = ">="

    def describe(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return (
            f"{self.name:<22} {self.value:9.4f}  {status}"
            f"  {self.comparison} {self.limit:.4f}"
        )


@dataclass
class Candidate:
    """One moment the detector considered making a slide boundary."""

    time: float
    gates: list[Gate] = field(default_factory=list)
    outcome: str = Outcome.REJECTED
    #: Name of the first gate that failed, or why it was absorbed.
    reason: str | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    #: Which decision path examined it: "strong global" or "normal".
    path: str = "normal"
    #: 0-1 belief in the boundary, recorded on the appearance when committed.
    confidence: float | None = None

    @property
    def strength(self) -> float:
        """How close this came to being a slide, for ranking near misses.

        Combines the two independent kinds of evidence so the ranking is not
        dominated by whichever one happened to be measured: the global
        structural change, and the local content replacement.
        """
        low_frequency = self.metrics.get("low_frequency_change", 0.0)
        replaced = self.metrics.get("content_replaced", 0.0)
        ink = self.metrics.get("ink_change", 0.0)
        return float(low_frequency * 2.0 + replaced + ink * 5.0)

    def add(
        self,
        name: str,
        value: float,
        limit: float,
        passed: bool,
        comparison: str = ">=",
    ) -> bool:
        self.gates.append(Gate(name, float(value), float(limit), bool(passed), comparison))
        return passed

    def reject(self, reason: str) -> Candidate:
        self.outcome = Outcome.REJECTED
        self.reason = reason
        return self

    def absorb(self, reason: str) -> Candidate:
        self.outcome = Outcome.ABSORBED
        self.reason = reason
        return self

    def commit(self) -> Candidate:
        self.outcome = Outcome.COMMITTED
        self.reason = None
        return self

    @property
    def failed_gate(self) -> Gate | None:
        return next((g for g in self.gates if not g.passed), None)

    def describe(self) -> str:
        lines = [f"Candidate at {format_hms(self.time)}   [{self.path} path]"]
        lines += [f"  {gate.describe()}" for gate in self.gates]
        if self.outcome == Outcome.COMMITTED:
            confidence = (
                f" (confidence {self.confidence:.2f})"
                if self.confidence is not None
                else ""
            )
            lines.append(f"  -> new slide{confidence}")
        elif self.outcome == Outcome.ABSORBED:
            lines.append(f"  -> same slide ({self.reason})")
        else:
            lines.append(f"  -> rejected: {self.reason}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "time": round(self.time, 3),
            "outcome": self.outcome,
            "reason": self.reason,
            "path": self.path,
            "confidence": (
                round(self.confidence, 4) if self.confidence is not None else None
            ),
            "metrics": {k: round(v, 5) for k, v in self.metrics.items()},
            "gates": [
                {
                    "name": g.name,
                    "value": round(g.value, 5),
                    "limit": round(g.limit, 5),
                    "passed": g.passed,
                }
                for g in self.gates
            ],
        }


@dataclass
class BusyRegionReport:
    """What the busy-region mask covered, so it can be audited.

    The mask is useful - webcam overlays, clocks, embedded video - but it can
    also hide real slide content, and doing so silently would look exactly like
    a detector that misses slides. So it is always reported, with the per-cell
    activity that produced it.
    """

    mask: list[list[bool]] = field(default_factory=list)
    activity: list[list[float]] = field(default_factory=list)
    masked_cells: int = 0
    total_cells: int = 0
    #: True when the mask covers enough of the frame to be suspicious.
    warn: bool = False

    @property
    def fraction(self) -> float:
        return self.masked_cells / self.total_cells if self.total_cells else 0.0

    def describe(self) -> str:
        lines = [
            f"Busy regions: {self.masked_cells} of {self.total_cells} cells "
            f"ignored ({self.fraction * 100:.0f} %)"
        ]
        if self.warn:
            lines.append(
                "  WARNING: a large share of the frame is being ignored. If the "
                "slide itself sits in one of these cells, real changes there "
                "cannot be seen. Raise busy_region_mask.noisy_cell_activity."
            )
        if self.activity:
            lines.append("  activity per cell (share of samples in which it changed):")
            for row_index, row in enumerate(self.activity):
                marks = "".join(
                    "#" if self.mask[row_index][col] else
                    ("+" if value > 0.05 else ".")
                    for col, value in enumerate(row)
                )
                lines.append(f"    {marks}   " + " ".join(f"{v:.2f}" for v in row))
            lines.append("    # = ignored, + = some activity, . = static")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "masked_cells": self.masked_cells,
            "total_cells": self.total_cells,
            "fraction": round(self.fraction, 4),
            "warn": self.warn,
            "activity": self.activity,
            "mask": self.mask,
        }
