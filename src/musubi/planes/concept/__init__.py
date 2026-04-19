"""Concept plane — bridge between episodic and curated.

See [[04-data-model/synthesized-concept]] for the spec. The plane is the
single write path into ``musubi_concept`` — namespace-isolated CRUD,
write-side invariants on ``promoted_*`` / ``promotion_rejected_*`` /
``merged_from`` length, and the lifecycle transitions
(``synthesized → matured → {promoted, demoted, superseded, archived}``)
that the synthesis + maturation + promotion workers in
``src/musubi/lifecycle/`` invoke.

Synthesis (the LLM clustering of episodic memories into a concept) and
the promotion gate / curated write live downstream in
``src/musubi/lifecycle/`` per slice-lifecycle-synthesis +
slice-lifecycle-promotion.
"""

from musubi.planes.concept.plane import ConceptPlane

__all__ = ["ConceptPlane"]
