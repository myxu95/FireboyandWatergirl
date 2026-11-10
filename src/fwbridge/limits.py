"""Limits that stop a worker before it burns unbounded tokens or time."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .usage import Totals

WARN_FRACTION = 0.8


@dataclass
class Limits:
    """None disables a limit. Token limits are token counts, timeouts are seconds."""

    max_cost_usd: float | None = None
    max_total_tokens: int | None = 10_000_000
    max_context_tokens: int | None = 200_000
    max_api_calls: int | None = 200
    stall_timeout_s: float | None = 1200
    wall_timeout_s: float | None = 4 * 3600

    def to_dict(self) -> dict:
        return asdict(self)

    def check(self, totals: Totals, elapsed_s: float, idle_s: float) -> tuple[str | None, list[str]]:
        """Return (breach, warnings). A breach means the worker must be stopped now."""
        metrics = [
            ("cost_usd", totals.cost_usd, self.max_cost_usd),
            ("total_tokens", totals.total_tokens, self.max_total_tokens),
            ("context_tokens", totals.max_context, self.max_context_tokens),
            ("api_calls", totals.api_calls, self.max_api_calls),
            ("wall_time_s", elapsed_s, self.wall_timeout_s),
            ("stall_s", idle_s, self.stall_timeout_s),
        ]
        warnings = []
        for name, value, limit in metrics:
            if limit is None or value is None:
                continue
            if value >= limit:
                return f"{name} limit reached: {_fmt(value)} >= {_fmt(limit)}", warnings
            # A stall has no meaningful "approaching" state.
            if name != "stall_s" and value >= WARN_FRACTION * limit:
                warnings.append(f"{name} at {value / limit:.0%} of limit ({_fmt(value)} / {_fmt(limit)})")
        return None, warnings


def _fmt(value) -> str:
    return f"{value:.2f}" if isinstance(value, float) and not value.is_integer() else f"{int(value)}"
