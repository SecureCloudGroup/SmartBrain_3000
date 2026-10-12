"""The query layer (Round 20, Q3a): an information ask → a closed Query IR over one declared
Library answer. ``recognize`` (two pinned recognizer libraries as validators), ``plan_query``
(one local model call + the v1 normalization + code-owned clauses + the rules floor),
``apply_query`` (the IR over a fetched payload, pure Python) and ``say`` (the interpretation
line). Wired into the flow's declared build in Q3b (``ni_flow._try_answers_build``) —
docs/internal/ni-format.md §36."""
from .apply import apply_query
from .plan import QueryPlan, plan_query
from .recognize import recognize
from .say import say

__all__ = ["QueryPlan", "apply_query", "plan_query", "recognize", "say"]
