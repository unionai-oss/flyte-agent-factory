"""Triage of `ask-human` questions: the factory decides engineering-judgement ones, the owner gets the rest.

`TriageMixin` is the part of `Bridge` that runs when a judge is set (`Bridge.judge`). It lives in its own
module to keep `_bridge` under the module size limit; it relies on the `Bridge` methods and fields
declared below.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

# Keep runtime imports usable when deployment tools load `_bridge` by path.
if TYPE_CHECKING:
    from ._bridge_common import (
        _EXIT_WITHDRAWN,
        _STATED_DEFAULT,
        ANSWERS,
        ASK_GRACE_S,
        _Close,
        _write_atomic,
        gave_up_path,
    )
    from ._judgement import Judgement
else:
    from flyte_agent_factory._bridge_common import (
        _EXIT_WITHDRAWN,
        _STATED_DEFAULT,
        ANSWERS,
        ASK_GRACE_S,
        _Close,
        _write_atomic,
        gave_up_path,
    )
    from flyte_agent_factory._judgement import Judgement

#: Bounds on what a decision record keeps: the dashboard page counts JSON bytes.
_DECISION_TEXT_MAX = 2000
_DECISION_ID = re.compile(r"\bdecision-(\d{3,})\b", re.IGNORECASE)
#: What makes direction naming a decision overrule it: a verb that can only mean overruling when aimed
#: at a decision, or one of the two phrasings people type for it. Naming a decision alone ("decision-003 was
#: right") only refers to it: marking that overruled would tell the owner the opposite of what they said.
#: Words that also occur in endorsements ("do not change it", "stop second-guessing", "instead") are left
#: out on purpose: a missed overrule shows on the next status read, a false one records the opposite.
_OVERRULING = re.compile(
    r"\b(?:overrul\w*|overrid\w*|revers\w*|revert\w*|undo|undoes|reject\w*|withdraw\w*|cancel\w*|"
    r"scrap|drop)\b"
    r"|\b(?:do not|don't|never) (?:follow|do|apply|implement|keep|use)\b.{0,40}?\bdecision-\d{3,}\b"
    r"|\bdecision-\d{3,} (?:is|was) wrong\b",
    re.IGNORECASE,
)
_NEGATION_BEFORE = re.compile(
    r"\b(?:not|never|without|cannot)\b|n't\b",
    re.IGNORECASE,
)
_NEED_TO = re.compile(r"\bno need to$", re.IGNORECASE)


def _negated(before: str) -> bool:
    """Whether a negation sits in the two words before a match, within the same clause ("no need to undo")."""
    clause = re.split(r"[,.!?;\n]", before)[-1]
    words = clause.split()
    # "no need to" is three words, the one phrase that reaches past two.
    return bool(_NEGATION_BEFORE.search(" ".join(words[-2:])) or _NEED_TO.search(" ".join(words[-3:])))


def pending_path(answer_path: Path) -> Path:
    """Marker the supervisor keeps while it triages (or queues) the ask for `answer_path`.

    The owner's timeout starts when the question reaches the owner, not while the factory judges it, so
    the CLI does not count down while this exists.
    """
    return answer_path.with_suffix(".pending")


def _print_decided(decided: dict[str, Any], approve: bool) -> int:
    """Print a question the factory decided itself, and return the exit code."""
    decision = decided.get("decision", "")
    verdict = decided.get("approve")
    if approve and verdict is not None:
        print("approved" if verdict else "declined")
        # Stderr, after the one-word answer on stdout: the session cites the id if the owner overrules it.
        print(f"Decided in the factory as {decided.get('id', '')}.", file=sys.stderr)
    else:
        print(
            f"Decided in the factory ({decided.get('id', '')}, engineering judgement — not escalated to "
            f"the owner): {decision}"
        )
    print(f"Why: {decided.get('reasoning', '')}")
    print(
        "Act on this decision. It is recorded in the project timeline and factory.status; owner direction "
        "that overrules it wins — act on that if it arrives.",
        file=sys.stderr,
    )
    if approve:
        # A yes/no that is neither reads as withdrawn, as an in-session reply does: the decision is in words.
        return _EXIT_WITHDRAWN if verdict is None else (0 if verdict else 1)
    return 0


class TriageMixin:
    """The `Bridge` methods that judge an ask and record what the factory decided."""

    if TYPE_CHECKING:
        # Supplied by `Bridge`; declared so the methods below type-check on their own.
        root: Path
        judge: Callable[[dict[str, Any]], Awaitable[Judgement]] | None
        questions: dict[str, dict[str, Any]]
        decisions: list[dict[str, Any]]
        _registry: Path | None
        _decision_seq: int
        _queued: dict[str, _Close]
        _unopened: dict[str, float]
        _owner_turn: asyncio.Lock

        def _judge_cap_s(self) -> float: ...
        def _decisions_max(self) -> int: ...
        def _emit(self, icon: str, text: str) -> None: ...
        def _remember(self, entry: dict[str, Any]) -> None: ...
        def _save(self) -> None: ...
        def _forget(self, request_id: str) -> None: ...
        def _prepare(self, request: dict[str, Any]) -> tuple[str, str, float, str] | None: ...
        async def _service(
            self,
            request: dict[str, Any],
            seq: int,
            *,
            entry: dict[str, Any] | None = None,
            prompt: str = "",
            prepared: tuple[str, str, float, str] | None = None,
        ) -> None: ...
        async def close_question(self, target: str, why: str) -> str | None: ...

    def _decisions_path(self) -> Path:
        return cast(Path, self._registry).with_name("decisions.json")

    async def _serve(self, request: dict[str, Any], seq: int) -> None:
        """Triage an ask, then either decide it here or put it to the owner in its turn."""
        prepared = self._prepare(request)
        if prepared is None:
            return
        question, kind, timeout, identity = prepared
        if self.judge is None or identity:  # a browser grant is the owner's by nature: never judged
            await self._service(request, seq)
            return
        request_id = request["id"]
        answer_path = self.root / ANSWERS / f"{request_id}.json"
        asked_at = time.time()
        # When it was made, as the untriaged path compares: a reply typed after the ask but before this scan
        # closes it.
        self._unopened[request_id] = float(request.get("created_at") or asked_at)
        try:
            judged = await self._triage(request, question, kind, answer_path)
            if judged is None:
                return
            prompt = (
                f"{question}\n\n**Factory triage:** owner-reserved \u2014 {judged.why_owner}. "
                f"**Recommendation:** {judged.recommendation or '(none given)'}"
            )
            self._emit("🙋", f"for the owner ({judged.why_owner})")
            entry = self._new_entry(request_id, kind, question, asked_at, timeout)
            entry["triage"] = judged.why_owner
            # Before waiting for the turn: the question being replaced may be the one holding it.
            await self._supersede(request, f"superseded by {request_id}")
            async with self._owner_turn:
                self._unopened.pop(request_id, None)
                if request_id in self._queued or gave_up_path(answer_path).exists():
                    # Closed while it waited (its CLI was told then), or its CLI left: never opened.
                    self._queued.pop(request_id, None)
                    entry["never_opened"] = True  # recorded, but it never reached the owner: no ask
                    self._forget(request_id)
                    return
                # The owner's clock starts now, not when the ask was made.
                now = time.time()
                entry.update(opened_at=now, applies_at=now + timeout, deadline_at=now + timeout + ASK_GRACE_S)
                await self._service(request, seq, entry=entry, prompt=prompt, prepared=prepared)
        finally:
            self._unopened.pop(request_id, None)
            pending_path(answer_path).unlink(missing_ok=True)

    def _new_entry(
        self, request_id: str, kind: str, question: str, opened: float, timeout: float
    ) -> dict[str, Any]:
        """Record a question the owner has not yet been shown (no condition until it opens)."""
        stated = _STATED_DEFAULT.search(question)
        entry = self.questions[request_id] = {
            "id": request_id,
            "kind": "approve" if kind == "approve" else "ask",
            "text": question,
            "opened_at": opened,
            "applies_at": opened + timeout,
            # Slightly past the CLI's own deadline, so the question is still open
            # while the asker is listening and closed once it has given up.
            "deadline_at": opened + timeout + ASK_GRACE_S,
            "default": stated.group(0).strip() if stated else "",
            "condition": "",
            "timed_out": False,
        }
        self._remember(entry)
        self._save()
        return entry

    async def _triage(
        self, request: dict[str, Any], question: str, kind: str, answer_path: Path
    ) -> Judgement | None:
        """Have the judge classify an ask. Returns the owner-class call, or None once the ask is settled here.

        Settled here means decided (engineering judgement: the CLI has its answer) or closed while it was
        being judged. The marker keeps the CLI's clock stopped: judging time is not the owner's time.
        """
        request_id = request["id"]
        assert self.judge is not None
        _write_atomic(pending_path(answer_path), {"id": request_id, "at": time.time()})
        self._emit("🧭", f"judging ask: {question[:300]}")
        judgement = await self._judged(request, question)
        if request_id in self._queued:
            # Withdrawn or answered in the session while judging: its CLI was told then.
            self._queued.pop(request_id, None)
            return None
        if judgement.klass != "engineering":
            return judgement
        decision = self._record_decision(request_id, question, kind, judgement)
        await self._supersede(request, f"superseded by {decision['id']}")
        # The answer before the marker goes: a CLI that sees no marker must find the answer.
        _write_atomic(answer_path, {"id": request_id, "decided": decision})
        return None

    async def _judged(self, request: dict[str, Any], question: str) -> Judgement:
        """Return the judge's call, or an owner-class one saying why there is none.

        This keeps the question: a judge that times out, raises or returns something else sends it to the
        owner, reported on the timeline with the reason, rather than ending this task and losing it. The
        judge runs as a task so its outcome is read, not caught.
        """
        assert self.judge is not None
        task = asyncio.ensure_future(self.judge({**request, "question": question}))
        try:
            done, _ = await asyncio.wait({task}, timeout=self._judge_cap_s())
        except asyncio.CancelledError:
            # The bridge is stopping: the judge stops with it, and is awaited, rather than outliving it.
            task.cancel()
            await asyncio.wait({task})
            raise
        if not done:
            task.cancel()
            why = f"judge could not decide: TimeoutError: no answer in {self._judge_cap_s():g}s"
        elif task.cancelled():
            why = "judge could not decide: it was cancelled"
        elif (error := task.exception()) is not None:
            traceback.print_exception(error)
            why = f"judge could not decide: {type(error).__name__}: {error}"
        else:
            result: object = task.result()
            if not isinstance(result, Judgement):
                why = f"judge could not decide: it returned {type(result).__name__}"
            elif result.klass == "engineering" and not result.reasoning.strip():
                # Whatever judge is plugged in: a decision without its reasons is never delivered.
                why = "judge could not decide: it gave an engineering decision with no reasoning"
            else:
                return result
        self._emit("⚠️", why[:500])
        return Judgement(klass="owner", why_owner=why)

    def _record_decision(
        self, request_id: str, question: str, kind: str, judgement: Judgement
    ) -> dict[str, Any]:
        """Keep a decision the factory made in the owner's place, and put it on the timeline."""
        self._decision_seq += 1
        decision: dict[str, Any] = {
            "id": f"decision-{self._decision_seq:03d}",
            "at": time.time(),
            "question": question[:_DECISION_TEXT_MAX],
            "kind": "approve" if kind == "approve" else "text",
            "decision": judgement.decision[:_DECISION_TEXT_MAX],
            "reasoning": judgement.reasoning[:_DECISION_TEXT_MAX],
            "approve": judgement.approve,
            "request": request_id,
        }
        self.decisions.append(decision)
        del self.decisions[: -self._decisions_max()]
        # In the ask history, but marked, so the benchmark does not count it as a question to the owner.
        self._remember(
            {
                "id": request_id,
                "kind": "approve" if kind == "approve" else "ask",
                "text": question,
                "opened_at": decision["at"],
                "applies_at": decision["at"],
                "deadline_at": decision["at"],
                "closed_at": decision["at"],
                "decided": decision["id"],
            }
        )
        self._save()
        self._emit(
            "⚖️",
            f"decided {decision['id']} (engineering judgement): {decision['decision'][:300]} "
            f"\u2014 why: {decision['reasoning'][:500]}",
        )
        return decision

    def overrule(self, text: str, source: str) -> list[str]:
        """Mark the decisions owner direction overrules as overruled; return their ids.

        A decision is overruled by a sentence that names it (`decision-NNN`) and says so with an overruling
        verb or phrase ("overrule decision-003", "decision-003 was wrong"), unless a negation governs it
        ("no need to undo decision-003"); a sentence that only names it is a reference, shown on the
        timeline and nothing more.
        """
        marked = []
        for sentence in re.split(r"(?<=[.!?;\n])\s+", text):
            ids = [f"decision-{n}" for n in dict.fromkeys(m.lower() for m in _DECISION_ID.findall(sentence))]
            overruling = any(not _negated(sentence[: m.start()]) for m in _OVERRULING.finditer(sentence))
            for decision_id in ids:
                decision = next((d for d in self.decisions if d["id"] == decision_id), None)
                if decision is None or decision_id in marked:
                    continue
                if not overruling:
                    self._emit("📎", f"{decision_id} referenced by owner direction ({source})")
                    continue
                decision["overruled_at"] = time.time()
                decision["overruled_by"] = f"{source}: {text[:500]}"
                marked.append(decision_id)
                self._emit("↩️", f"{decision_id} overruled by owner direction ({source})")
        if marked:
            self._save()
        return marked

    async def _supersede(self, request: dict[str, Any], why: str) -> None:
        for old in request.get("supersedes") or []:
            if old != request["id"]:
                await self.close_question(old, why)

    def _close_unopened(self, request_id: str, close: _Close) -> None:
        """Close an ask that has no condition and will get none: tell its CLI now, and `_service` drops it."""
        self._queued[request_id] = close
        _write_atomic(self.root / ANSWERS / f"{request_id}.json", {"id": request_id, **close["outcome"]})
        self._emit("🗑️", f"closed {request_id}: {close['reason'].removeprefix('closed: ')}")
