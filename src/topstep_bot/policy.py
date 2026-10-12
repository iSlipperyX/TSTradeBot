"""The decision rule the adaptive strategy trades with (``knowledge.policy``).

``Judge.verdict`` answers "may this strategy trade now, and how good is it?" with either rule:

* classic - ``KnowledgeBase.verdict`` (version 1.2), the default until the decision lab says otherwise;
* evidence - ``evidence.evidence_verdict`` (version 2.2), backed by the long-run memory.

``auto`` asks the decision lab's last result (decision_lab.py) which rule to use, so the bot only switches
once the evidence rule has won on replayed history. The decision lab and the live bot call the very
same functions, so what the lab measured is what the bot does.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from topstep_bot.evidence import EvidenceModel, evidence_verdict, prior_observations, signal_evidence
from topstep_bot.knowledge import KnowledgeBase, Verdict


class Judge:
    def __init__(self, knowledge: KnowledgeBase, prior: Callable[[], KnowledgeBase | None] = lambda: None,
                 policy: str = "classic", why: str = ""):
        self.knowledge = knowledge
        self.prior = prior  # the long-run memory's knowledge base, when there is one (it can arrive later)
        self.policy = policy
        self.why = why  # why this rule is in use, in a few words (shown in explanations)
        self._model: tuple[tuple, tuple[EvidenceModel, list]] | None = None

    def set_policy(self, policy: str, why: str = "") -> None:
        self.policy, self.why = policy, why

    @property
    def version(self) -> tuple:
        """Changes whenever a verdict could change (for callers that cache verdicts)."""
        prior = self.prior()
        return self.policy, self.knowledge.updated, prior.updated if prior else -1

    def _cached(self, day: date) -> tuple[EvidenceModel, list]:
        prior = self.prior()
        key = (day, self.knowledge.updated, len(self.knowledge.obs), id(prior), prior.updated if prior else -1)
        if self._model is None or self._model[0] != key:
            kb = self.knowledge
            older = prior_observations(kb, prior)
            self._model = (key, (EvidenceModel(kb.obs, older, today=day, half_life_days=kb.half_life_days,
                                               real_weight=kb.real_weight), older))
        return self._model[1]

    def model(self, day: date) -> EvidenceModel:
        return self._cached(day)[0]

    def evidence(self, strategy: str, side: str, slot: str, regime: str, ctx: dict | None, day: date) -> dict:
        """Everything the bot's memory says about one signal (for its explanation), whichever rule decides."""
        model, older = self._cached(day)
        return signal_evidence(self.knowledge, model, older, strategy, side, slot, regime, ctx, day)

    def verdict(self, strategy: str, slot: str, regime: str, day: date) -> Verdict:
        if self.policy == "evidence":
            kb = self.knowledge
            return evidence_verdict(self.model(day), strategy, slot, regime, min_samples=kb.min_samples,
                                    min_edge_r=kb.min_edge_r)
        return self.knowledge.verdict(strategy, slot, regime, day)

    def describe(self) -> str:
        name = {"classic": "the classic rule (1.2)", "evidence": "the evidence rule (2.2)"}.get(self.policy, self.policy)
        return f"{name} - {self.why}" if self.why else name
