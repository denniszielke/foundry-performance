"""Shared timing utilities for the weather-agent benchmark clients.

Each protocol client returns :class:`Turn` records; :func:`summarize` aggregates
them into per-(protocol, phase) statistics. Phases model the questions the
benchmark answers:

* ``cold``     — the very first request against a freshly started agent.
* ``warm``     — steady-state requests on a new session each time.
* ``followup`` — a second request that reuses the same session (conversation).
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass
class Turn:
    protocol: str
    phase: str
    ok: bool
    total_s: float
    ttfb_s: float | None = None
    text: str = ""
    error: str | None = None


@dataclass
class Timer:
    """Monotonic stopwatch that records total time and (optionally) TTFB."""

    _start: float = field(default_factory=time.perf_counter)
    ttfb_s: float | None = None

    def first_byte(self) -> None:
        if self.ttfb_s is None:
            self.ttfb_s = time.perf_counter() - self._start

    @property
    def elapsed_s(self) -> float:
        return time.perf_counter() - self._start


def _pct(values: Sequence[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


@dataclass
class Stat:
    protocol: str
    phase: str
    count: int
    errors: int
    mean_s: float
    p50_s: float
    p95_s: float
    mean_ttfb_s: float | None


def summarize(turns: Sequence[Turn]) -> list[Stat]:
    """Collapse raw turns into one :class:`Stat` per (protocol, phase)."""
    buckets: dict[tuple[str, str], list[Turn]] = {}
    for t in turns:
        buckets.setdefault((t.protocol, t.phase), []).append(t)

    stats: list[Stat] = []
    for (protocol, phase), items in buckets.items():
        ok = [t for t in items if t.ok]
        totals = [t.total_s for t in ok]
        ttfbs = [t.ttfb_s for t in ok if t.ttfb_s is not None]
        stats.append(
            Stat(
                protocol=protocol,
                phase=phase,
                count=len(items),
                errors=sum(1 for t in items if not t.ok),
                mean_s=statistics.fmean(totals) if totals else float("nan"),
                p50_s=_pct(totals, 0.50),
                p95_s=_pct(totals, 0.95),
                mean_ttfb_s=statistics.fmean(ttfbs) if ttfbs else None,
            )
        )
    stats.sort(key=lambda s: (s.protocol, s.phase))
    return stats


_PHASE_ORDER = {"cold": 0, "warm": 1, "followup": 2}


def format_table(stats: Sequence[Stat]) -> str:
    """Render a fixed-width results table (seconds → milliseconds)."""
    header = f"{'protocol':<16}{'phase':<10}{'n':>4}{'err':>5}{'mean ms':>10}{'p50 ms':>10}{'p95 ms':>10}{'ttfb ms':>10}"
    lines = [header, "-" * len(header)]
    for s in sorted(stats, key=lambda s: (s.protocol, _PHASE_ORDER.get(s.phase, 9))):
        ttfb = f"{s.mean_ttfb_s * 1000:.0f}" if s.mean_ttfb_s is not None else "-"
        lines.append(
            f"{s.protocol:<16}{s.phase:<10}{s.count:>4}{s.errors:>5}"
            f"{s.mean_s * 1000:>10.0f}{s.p50_s * 1000:>10.0f}{s.p95_s * 1000:>10.0f}{ttfb:>10}"
        )
    return "\n".join(lines)
