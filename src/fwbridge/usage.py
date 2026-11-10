"""Token accounting shared by both workers. All quantities are token counts unless noted."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class CallUsage:
    """Usage of one model API call."""

    input_uncached: int
    cache_read: int
    cache_write: int
    output: int

    @property
    def context(self) -> int:
        """Prompt size of this call: what the model had to read, cached or not."""
        return self.input_uncached + self.cache_read + self.cache_write


@dataclass
class Totals:
    api_calls: int = 0
    input_uncached: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    # None means "not observed" (e.g. Codex without live usage), never zero.
    last_context: int | None = None
    max_context: int | None = None
    cost_usd: float | None = None

    def add(self, call: CallUsage) -> None:
        self.api_calls += 1
        self.input_uncached += call.input_uncached
        self.cache_read += call.cache_read
        self.cache_write += call.cache_write
        self.output += call.output
        self.last_context = call.context
        self.max_context = max(self.max_context or 0, call.context)

    @property
    def total_tokens(self) -> int:
        return self.input_uncached + self.cache_read + self.cache_write + self.output

    def to_dict(self) -> dict:
        d = asdict(self)
        d["total_tokens"] = self.total_tokens
        return d
