"""Part G — a semantic response cache that is safe, fresh, and keeps FAQ answers up in an outage.

* ``SemanticCache``: lookup by embedding similarity *within a scope* (prompt
  version; personal questions never), TTL + a stale window, invalidation by
  source document, bounded size (LRU).
* ``CachingClient``: wraps your ResilientClient. A fresh hit answers without
  calling any provider; a miss goes to the providers and the answer is stored;
  when the providers give up, a *stale* answer beats no answer (fallback
  ladder, rung 3: "cached response").

Tests: tests/test_cache.py, then `make cache` (tape C1).

Lifetime of an entry (age = now - stored_at):

    0 ......... ttl_s ................ ttl_s + max_stale_s ......
    |   fresh   |  stale: only if no provider answers  |  expired: drop it

Only FAQ calls carry ``call.request``; every other call has ``request=None``
and must go straight to the inner client.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from gameday_lab.sim.contracts import (
    Action,
    Answer,
    Attempt,
    BaseClient,
    CallContext,
    GiveUp,
    ProviderError,
    Request,
    Serve,
)


def cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """PROVIDED. Cosine similarity: 1.0 = same direction (high = strict threshold)."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@dataclass
class Entry:
    embedding: tuple[float, ...]
    answer: Answer
    stored_at: float
    last_used: float


@dataclass(frozen=True)
class CacheHit:
    answer: Answer
    similarity: float
    age_s: float
    fresh: bool  # age_s <= ttl_s; a stale hit may only be served when no provider can answer


class SemanticCache:
    def __init__(self, similarity_threshold: float = 0.9, ttl_s: float = 60.0, max_stale_s: float = 600.0,
                 max_entries: int = 500) -> None:
        """PROVIDED: reads the `cache:` block of configs/gameday.yaml."""
        self.similarity_threshold = similarity_threshold
        self.ttl_s = ttl_s
        self.max_stale_s = max_stale_s
        self.max_entries = max_entries
        self.entries: list[Entry] = []

    def lookup(self, req: Request, now: float) -> CacheHit | None:
        """TODO(student): the best usable entry for ``req``, or None.

        * personal questions (``req.personal``) are not in the allowlist -> None
        * drop expired entries (age > ttl_s + max_stale_s)
        * scope: only entries whose ``answer.prompt_version == req.prompt_version``
        * best = highest ``cosine(req.embedding, entry.embedding)``; a hit needs
          similarity >= similarity_threshold
        * on a hit: set ``entry.last_used = now`` and return
          CacheHit(answer, similarity, age_s, fresh=age_s <= ttl_s)
        """
        self.entries = [
            entry for entry in self.entries
            if now - entry.stored_at <= self.ttl_s + self.max_stale_s
        ]
        if req.personal:
            return None

        best: Entry | None = None
        best_similarity = -1.0
        for entry in self.entries:
            if entry.answer.prompt_version != req.prompt_version:
                continue
            similarity = cosine(req.embedding, entry.embedding)
            if similarity > best_similarity:
                best = entry
                best_similarity = similarity

        if best is None or best_similarity < self.similarity_threshold:
            return None
        age_s = now - best.stored_at
        best.last_used = now
        return CacheHit(best.answer, best_similarity, age_s, fresh=age_s <= self.ttl_s)

    def store(self, req: Request, answer: Answer, now: float) -> None:
        """TODO(student): remember ``answer`` for ``req``.

        * never store personal answers
        * if an entry in the same scope is already >= similarity_threshold
          similar, *replace* it (same question: refresh, don't pile up duplicates)
        * otherwise append; if there are now more than max_entries, evict the
          least recently used entry (smallest ``last_used``)
        """
        if req.personal:
            return

        replacement: int | None = None
        replacement_similarity = -1.0
        for idx, entry in enumerate(self.entries):
            if entry.answer.prompt_version != req.prompt_version:
                continue
            similarity = cosine(req.embedding, entry.embedding)
            if similarity >= self.similarity_threshold and similarity > replacement_similarity:
                replacement = idx
                replacement_similarity = similarity

        new_entry = Entry(req.embedding, answer, now, now)
        if replacement is not None:
            self.entries[replacement] = new_entry
        else:
            self.entries.append(new_entry)

        while len(self.entries) > self.max_entries:
            lru = min(range(len(self.entries)), key=lambda idx: self.entries[idx].last_used)
            self.entries.pop(lru)

    def invalidate(self, source: str, now: float) -> int:
        """TODO(student): drop every entry whose ``answer.sources`` contains ``source``; return how many."""
        before = len(self.entries)
        self.entries = [entry for entry in self.entries if source not in entry.answer.sources]
        return before - len(self.entries)


class CachingClient(BaseClient):
    name = "cached"

    def __init__(self, inner: BaseClient, cache: SemanticCache) -> None:
        """PROVIDED. ``inner`` is your ResilientClient (see factory.build_client("cached"))."""
        self.inner = inner
        self.cache = cache

    def on_cancel(self, call: CallContext, tag: Any, now: float) -> None:
        """PROVIDED."""
        self.inner.on_cancel(call, tag, now)

    # ------------------------------------------------------------------ TODO
    def start(self, call: CallContext, now: float) -> Action:
        """TODO(student).

        * FAQ call (``call.request`` is not None) with a *fresh* hit -> Serve(hit.answer)
          (no provider is called at all)
        * otherwise ask ``self.inner.start(...)`` and pass its action through ``_or_stale``
        """
        if call.request is not None:
            hit = self.cache.lookup(call.request, now)
            if hit is not None and hit.fresh:
                return Serve(hit.answer)
        return self._or_stale(call, self.inner.start(call, now), now)

    def on_success(self, attempt: Attempt, now: float, latency_s: float) -> None:
        """TODO(student): tell the inner client first (its breaker needs the result),
        then store ``attempt.call.answer`` if the call carries a request and an answer."""
        self.inner.on_success(attempt, now, latency_s)
        request = attempt.call.request
        answer = attempt.call.answer
        if request is not None and answer is not None:
            self.cache.store(request, answer, now)

    def on_error(self, attempt: Attempt, err: ProviderError, now: float) -> Action:
        """TODO(student): ``self.inner.on_error(...)``, passed through ``_or_stale``."""
        action = self.inner.on_error(attempt, err, now)
        return self._or_stale(attempt.call, action, now)

    def on_content_update(self, source: str, now: float) -> None:
        """TODO(student): a document changed (webhook) — invalidate the answers built on it."""
        self.inner.on_content_update(source, now)
        self.cache.invalidate(source, now)

    def _or_stale(self, call: CallContext, action: Action, now: float) -> Action:
        """TODO(student): stale-if-error.

        If ``action`` is a GiveUp and the call is a FAQ call, look the request up
        *again now* (an invalidation may have happened meanwhile): any hit ->
        Serve(hit.answer, stale=not hit.fresh). Otherwise return ``action`` unchanged.
        """
        if isinstance(action, GiveUp) and call.request is not None:
            hit = self.cache.lookup(call.request, now)
            if hit is not None:
                return Serve(hit.answer, stale=not hit.fresh)
        return action
