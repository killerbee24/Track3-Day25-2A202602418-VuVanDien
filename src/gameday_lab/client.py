"""Part C — ResilientClient: compose classification, retry, breaker and fallback.

The simulator calls ``start`` for every logical LLM call, then ``on_success``
or ``on_error`` for every attempt. You answer with ``Send`` (try a provider,
maybe after a delay) or ``GiveUp`` (user gets a static degraded answer).

Tests: tests/test_client.py, then `make compare` (6 incident tapes).

Per-call scratch space lives in ``call.state`` (a dict). Suggested keys:
    "ladder": list[str]  providers this call may use, in order
    "idx":    int        position of the provider we are currently on
    "tries":  int        attempts already made on that provider

Decision table for ``on_error`` (implement it!):

| ErrorKind        | breaker              | then                                                    |
|------------------|----------------------|---------------------------------------------------------|
| CLIENT_ERROR     | release (not health) | GiveUp("client_error") — never retry, never fail over   |
| QUOTA_EXHAUSTED  | release              | cooldown provider for quota_cooldown_s; fail over now   |
| RATE_LIMITED     | release              | cooldown provider until now + retry_after; fail over now|
| OVERLOADED /     | on_failure           | retry SAME provider after backoff.delay(tries) if       |
| SERVER_ERROR /   |                      |   tries < max_attempts_per_provider AND budget allows   |
| TIMEOUT          |                      |   AND it fits the deadline; otherwise fail over         |

Cooldowns are per provider and shared by all calls (other calls must avoid a
rate-limited provider too). When every provider in the ladder is unusable,
you may wait for a *short* (<= 5 s) rate-limit cooldown if it fits the deadline.
"""

from __future__ import annotations

import random
from typing import Any

from gameday_lab.breaker import BreakerState, Permit, SlidingWindowBreaker
from gameday_lab.errors import ErrorKind, classify, counts_toward_breaker
from gameday_lab.retry import FullJitterBackoff, RetryBudget
from gameday_lab.sim.contracts import (
    Action,
    Attempt,
    BaseClient,
    CallContext,
    GiveUp,
    ProviderError,
    ProviderInfo,
    Send,
)

RATE_LIMIT_WAIT_MAX_S = 5.0  # only wait for short (rate-limit) cooldowns, never for quota


class ResilientClient(BaseClient):
    name = "resilient"

    def __init__(
        self,
        providers: dict[str, ProviderInfo],
        fallback_order: list[str] | tuple[str, ...],
        settings: dict[str, Any],
        seed: int = 0,
    ) -> None:
        """PROVIDED: reads the `client:` block of configs/gameday.yaml."""
        self.providers = providers
        self.order = list(fallback_order)
        self.timeout_s = float(settings.get("attempt_timeout_s", 8.0))
        self.max_attempts_per_provider = int(settings.get("max_attempts_per_provider", 2))
        self.min_time_left_s = float(settings.get("min_time_left_s", 1.0))
        self.quota_cooldown_s = float(settings.get("quota_cooldown_s", 300.0))
        self.backoff = FullJitterBackoff(
            float(settings.get("backoff_base_s", 0.25)),
            float(settings.get("backoff_cap_s", 4.0)),
            random.Random(seed),
        )
        self.budget = RetryBudget(
            float(settings.get("retry_budget_ratio", 0.1)),
            float(settings.get("retry_budget_min_per_s", 0.5)),
            float(settings.get("retry_budget_max_tokens", 20.0)),
        )
        breaker_cfg = dict(settings.get("breaker", {}))
        self.breakers = {name: SlidingWindowBreaker(name, **breaker_cfg) for name in providers}
        self.cooldown_until: dict[str, float] = {name: 0.0 for name in providers}
        # Only rate-limit cooldowns may be waited out when every route is busy.
        # Quota cooldowns are deliberately never waitable.
        self.rate_limit_until: dict[str, float] = {name: 0.0 for name in providers}

    # ------------------------------------------------------------------ provided helper
    def _send(self, call: CallContext, provider: str, now: float, delay: float) -> Action | None:
        """PROVIDED. Build a Send for ``provider`` (after ``delay``), or None if its breaker refuses.

        Takes a breaker permit (carried in ``tag``), and caps the attempt timeout
        so it never runs past the call's deadline. Returns GiveUp("deadline") if
        there is not enough time left for a meaningful attempt.
        """
        permit = self.breakers[provider].allow(now)
        if permit is None:
            return None
        timeout = min(self.timeout_s, call.deadline - (now + delay))
        if timeout < self.min_time_left_s:
            self.breakers[provider].release(permit, now)
            return GiveUp("deadline")
        return Send(provider, timeout_s=timeout, delay_s=delay, tag=permit)

    # ------------------------------------------------------------------ TODO
    def ladder(self, task: str) -> list[str]:
        """TODO(student): ``self.order`` filtered by capability —
        a "tool" call may only use providers with ``supports_tools``."""
        if task == "tool":
            return [name for name in self.order if self.providers[name].supports_tools]
        return list(self.order)

    def _route(self, call: CallContext, now: float, delay: float = 0.0) -> Action:
        """TODO(student): pick the provider for the next attempt.

        Walk the ladder from call.state["idx"]:
          * skip a provider still in cooldown at send time (now + delay)
          * otherwise try self._send(...); a non-None result is your answer
          * when skipping, move idx forward, reset tries, and set delay = 0
            (a failover goes out immediately)
        If the ladder is exhausted: optionally wait for the earliest *short*
        rate-limit cooldown (<= RATE_LIMIT_WAIT_MAX_S, breaker not OPEN, fits the
        deadline), else GiveUp("no_provider_available").
        """
        ladder = call.state["ladder"]
        while call.state["idx"] < len(ladder):
            provider = ladder[call.state["idx"]]
            if now + delay < self.cooldown_until[provider]:
                call.state["idx"] += 1
                call.state["tries"] = 0
                delay = 0.0
                continue

            action = self._send(call, provider, now, delay)
            if action is not None:
                if isinstance(action, Send):
                    call.state["tries"] += 1
                return action

            # An OPEN breaker is equivalent to an unavailable route.
            call.state["idx"] += 1
            call.state["tries"] = 0
            delay = 0.0

        # If all routes were skipped, a short rate-limit cooldown can be worth
        # waiting for. Never wait for quota cooldowns or OPEN breakers.
        candidates: list[tuple[float, int, str]] = []
        for idx, provider in enumerate(ladder):
            until = self.cooldown_until[provider]
            remaining = until - now
            is_rate_limit = abs(self.rate_limit_until[provider] - until) <= 1e-9
            if (
                is_rate_limit
                and 0.0 < remaining <= RATE_LIMIT_WAIT_MAX_S
                and self.breakers[provider].state is not BreakerState.OPEN
                and now + remaining + self.min_time_left_s <= call.deadline
            ):
                candidates.append((remaining, idx, provider))

        for remaining, idx, provider in sorted(candidates):
            call.state["idx"] = idx
            call.state["tries"] = 0
            action = self._send(call, provider, now, remaining)
            if action is not None:
                if isinstance(action, Send):
                    call.state["tries"] = 1
                return action

        if call.deadline - now < self.min_time_left_s:
            return GiveUp("deadline")
        return GiveUp("no_provider_available")

    def start(self, call: CallContext, now: float) -> Action:
        """TODO(student): record the request in the retry budget, initialise
        call.state (ladder, idx=0, tries=0) and route."""
        self.budget.record_request(now)
        call.state["ladder"] = self.ladder(call.task)
        call.state["idx"] = 0
        call.state["tries"] = 0
        return self._route(call, now)

    def on_success(self, attempt: Attempt, now: float, latency_s: float) -> None:
        """TODO(student): report success + latency to the provider's breaker (attempt.tag is the Permit)."""
        if isinstance(attempt.tag, Permit):
            self.breakers[attempt.provider].on_success(attempt.tag, now, latency_s)

    def on_error(self, attempt: Attempt, err: ProviderError, now: float) -> Action:
        """TODO(student): implement the decision table in the module docstring."""
        kind = classify(err)
        breaker = self.breakers[attempt.provider]
        if isinstance(attempt.tag, Permit):
            if counts_toward_breaker(kind):
                breaker.on_failure(attempt.tag, now)
            else:
                breaker.release(attempt.tag, now)

        if kind is ErrorKind.CLIENT_ERROR:
            return GiveUp("client_error")

        if kind is ErrorKind.QUOTA_EXHAUSTED:
            self.cooldown_until[attempt.provider] = max(
                self.cooldown_until[attempt.provider], now + self.quota_cooldown_s
            )
            self.rate_limit_until[attempt.provider] = 0.0
            attempt.call.state["idx"] += 1
            attempt.call.state["tries"] = 0
            return self._route(attempt.call, now)

        if kind is ErrorKind.RATE_LIMITED:
            retry_after = max(0.0, float(err.retry_after or 0.0))
            until = now + retry_after
            if until >= self.cooldown_until[attempt.provider]:
                self.cooldown_until[attempt.provider] = until
                self.rate_limit_until[attempt.provider] = until
            attempt.call.state["idx"] += 1
            attempt.call.state["tries"] = 0
            return self._route(attempt.call, now)

        tries = int(attempt.call.state["tries"])
        if tries < self.max_attempts_per_provider:
            delay = self.backoff.delay(max(1, tries))
            fits_deadline = now + delay + self.min_time_left_s <= attempt.call.deadline
            if fits_deadline and self.budget.try_acquire_retry(now):
                return self._route(attempt.call, now, delay)

        attempt.call.state["idx"] += 1
        attempt.call.state["tries"] = 0
        return self._route(attempt.call, now)

    def on_cancel(self, call: CallContext, tag: Any, now: float) -> None:
        """TODO(student): a delayed attempt was dropped (deadline) — release its permit."""
        if isinstance(tag, Permit) and tag.breaker in self.breakers:
            self.breakers[tag.breaker].release(tag, now)
