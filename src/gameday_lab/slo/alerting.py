"""Part E — Multi-window, multi-burn-rate alerting (Google SRE Workbook, ch. 5).

Input: per-minute ``total`` and ``bad`` request counts for a 30-day window.

burn_rate(window) = error_ratio(window) / (1 - SLO)

A rule fires at minute i when BOTH its long and its short window (ending at i,
inclusive) burn at least ``burn_rate``. The long window says "this is
significant"; the short window says "it is still happening" (so the alert
resets quickly after recovery).

Tests: tests/test_alerting.py, then `make slo`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AlertRule:
    name: str
    severity: str  # "page" or "ticket"
    long_min: int
    short_min: int
    burn_rate: float


# SRE Workbook defaults for a 30-day SLO window
DEFAULT_RULES: tuple[AlertRule, ...] = (
    AlertRule("fast_burn_2pct_1h", "page", 60, 5, 14.4),
    AlertRule("slow_burn_5pct_6h", "page", 360, 30, 6.0),
    AlertRule("budget_leak_10pct_3d", "ticket", 4320, 360, 1.0),
)


@dataclass(frozen=True)
class AlertEvent:
    rule: str
    severity: str
    start_min: int  # first minute the rule fired
    end_min: int  # first minute it stopped firing (exclusive)


class WindowSums:
    """O(1) sum of any window, via prefix sums. Build once, query many times.

    43,200 minutes x 6 windows: recomputing every window with sum() is far too slow.
    """

    def __init__(self, values: list[int]) -> None:
        """TODO(student): build the prefix-sum list (prefix[0] = 0)."""
        prefix = [0]
        running = 0
        for value in values:
            running += value
            prefix.append(running)
        self.prefix = prefix

    def sum(self, end_inclusive: int, length: int) -> int:
        """TODO(student): sum of values[end_inclusive - length + 1 .. end_inclusive],
        clipped at index 0 when the window starts before the data."""
        end = end_inclusive + 1
        start = max(0, end_inclusive - length + 1)
        return self.prefix[end] - self.prefix[start]


class BurnRateAlerter:
    def __init__(self, slo: float = 0.995, rules: tuple[AlertRule, ...] = DEFAULT_RULES) -> None:
        if not 0 < slo < 1:
            raise ValueError("slo must be in (0, 1)")
        self.slo = slo
        self.rules = rules

    def burn_rate(self, total: WindowSums, bad: WindowSums, end: int, length: int) -> float:
        """TODO(student): (bad / total over the window) / (1 - slo); 0.0 if total is 0."""
        window_total = total.sum(end, length)
        if window_total == 0:
            return 0.0
        return (bad.sum(end, length) / window_total) / (1 - self.slo)

    def evaluate(self, total: list[int], bad: list[int]) -> list[AlertEvent]:
        """TODO(student): one AlertEvent per contiguous firing episode of each rule.

        For each rule, scan the minutes; the rule fires at minute i when
        burn(long) >= rule.burn_rate AND burn(short) >= rule.burn_rate.
        An episode starts at the first firing minute and ends (exclusive) at the
        first minute it stops firing (or len(total)). Sort by start_min.
        """
        total_sums = WindowSums(total)
        bad_sums = WindowSums(bad)
        minutes = len(total)
        events: list[AlertEvent] = []
        for rule in self.rules:
            started: int | None = None
            for minute in range(minutes):
                firing = (
                    self.burn_rate(total_sums, bad_sums, minute, rule.long_min) >= rule.burn_rate
                    and self.burn_rate(total_sums, bad_sums, minute, rule.short_min) >= rule.burn_rate
                )
                if firing:
                    if started is None:
                        started = minute
                elif started is not None:
                    events.append(AlertEvent(rule.name, rule.severity, started, minute))
                    started = None
            if started is not None:
                events.append(AlertEvent(rule.name, rule.severity, started, minutes))
        events.sort(key=lambda e: e.start_min)
        return events


def error_budget_report(total: list[int], bad: list[int], slo: float = 0.995) -> dict[str, float | bool]:
    """TODO(student): how much of the 30-day error budget is spent?

    Return a dict with:
      requests          sum(total)
      allowed_bad       (1 - slo) * requests            (round to 1 decimal)
      bad               sum(bad)
      availability      1 - bad / requests              (round to 6 decimals)
      budget_consumed   bad / allowed_bad               (round to 4 decimals)
      budget_remaining  1 - budget_consumed             (round to 4 decimals)
      freeze_releases   True when nothing is left (budget_remaining <= 0)
    """
    requests = sum(total)
    allowed_bad = (1 - slo) * requests
    bad_total = sum(bad)
    availability = 1 - bad_total / requests if requests else 1.0
    consumed = bad_total / allowed_bad if allowed_bad else 0.0
    remaining = 1 - consumed
    return {
        "requests": requests,
        "allowed_bad": round(allowed_bad, 1),
        "bad": bad_total,
        "availability": round(availability, 6),
        "budget_consumed": round(consumed, 4),
        "budget_remaining": round(remaining, 4),
        "freeze_releases": remaining <= 0,
    }
