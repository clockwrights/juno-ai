"""Role to model, and the daily token budget for each (docs/06 §5.1).

This file is the reason the agent cannot choose its own model or raise its own
budget: it lives in `gates/`, which no proposal may touch (docs/08 §2), and the
caller's role is established by IAM rather than read from the payload.

The numbers are sized to the owner's $20/month budget (ADR-28). If every role spent
its full daily budget every single day, the model bill would be about
$18.25/month, leaving room for the sub-$1 infrastructure cost.
`test_the_daily_budgets_fit_the_monthly_envelope` recomputes that from these
numbers, so lowering a price or raising a budget cannot quietly break it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

BASE_URL: Final[str] = "https://api.together.xyz/v1"

#: Retried once when a call comes back `content_filter`. A different model in
#: the same family, because the point is to route around one model's refusal,
#: not to escalate capability.
FALLBACK_MODEL: Final[str] = "zai-org/GLM-5.2"

GLM: Final[str] = "zai-org/GLM-5.3"
GLM_FLASH: Final[str] = "zai-org/GLM-5.3-Flash"

EMBEDDING_MODEL: Final[str] = "intfloat/multilingual-e5-large-instruct"
EMBEDDING_DIM: Final[int] = 1024
#: Embeddings are cheap ($0.02/M) but not free, and a runaway re-index would be
#: the way to discover that.
EMBEDDING_DAILY_BUDGET: Final[int] = 500_000


@dataclass(frozen=True)
class RolePolicy:
    role: str
    model: str
    daily_token_budget: int
    reasoning: bool = True
    alias: str = "agent"
    why: str = ""


POLICIES: Final[dict[str, RolePolicy]] = {
    "conversation": RolePolicy(
        role="conversation",
        model=GLM,
        daily_token_budget=200_000,
        alias="agent",
        why="The owner is in the loop; quality here is the quality they see.",
    ),
    "heartbeat": RolePolicy(
        role="heartbeat",
        model=GLM_FLASH,
        daily_token_budget=300_000,
        alias="agent",
        why="Unattended and high-volume. Flash is what makes $20/month possible.",
    ),
    "consolidation": RolePolicy(
        role="consolidation",
        model=GLM,
        daily_token_budget=60_000,
        alias="agent",
        why=(
            "Rewriting memory is lossy and irreversible, so it gets the stronger "
            "model. 60k is a generous maintenance cycle — roughly 100 originals "
            "read and summarised — and it is what keeps the true worst case "
            "(every role spending every day) inside $20/month."
        ),
    ),
    "supervisor": RolePolicy(
        role="supervisor",
        model=GLM_FLASH,
        daily_token_budget=50_000,
        alias="supervisor",
        why="The Level 2 path is deterministic (ADR-13); the model only writes prose.",
    ),
    "canary": RolePolicy(
        role="canary",
        model=GLM_FLASH,
        daily_token_budget=50_000,
        alias="canary",
        why="Probes the candidate version; assertions are deterministic.",
    ),
    # Not a chat role. It shares the gateway because the gateway is the only
    # holder of the provider key (ADR-03, ADR-29), and it has its own alias so
    # the two agent roles can be granted it without also being granted
    # `supervisor` or `canary`.
    "embed": RolePolicy(
        role="embed",
        model=EMBEDDING_MODEL,
        daily_token_budget=EMBEDDING_DAILY_BUDGET,
        alias="embed",
        why=(
            "Embeddings cost $0.02/M — negligible per call, but a runaway "
            "re-index is how you would find that out, so it is metered like "
            "everything else."
        ),
    ),
}

#: Which roles each alias accepts. The alias is an IAM boundary first — the two
#: agent roles can only invoke `agent` — and this is the second check, made from
#: the invoked ARN rather than from anything the caller sent.
ALIAS_ROLES: Final[dict[str, frozenset[str]]] = {
    "agent": frozenset({"conversation", "heartbeat", "consolidation"}),
    "supervisor": frozenset({"supervisor"}),
    "canary": frozenset({"canary"}),
    "embed": frozenset({"embed"}),
}

#: Fields a caller may never set. `model` is the gateway's decision (ADR-05);
#: the rest would let a caller spend outside its budget or bypass recording.
FORBIDDEN_REQUEST_FIELDS: Final[frozenset[str]] = frozenset(
    {"model", "api_key", "base_url", "stream", "user", "n"}
)

MAX_TOKENS_CAP: Final[int] = 16_000


def policy_for(role: str) -> RolePolicy:
    try:
        return POLICIES[role]
    except KeyError:
        raise ValueError(f"unknown model role {role!r}") from None


def role_allowed_on_alias(role: str, alias: str) -> bool:
    return role in ALIAS_ROLES.get(alias, frozenset())


def alias_from_arn(invoked_arn: str) -> str:
    """The alias is the last ARN segment when a function is invoked through one.

    Falling back to `agent` would be the dangerous default, so an ARN without a
    recognised alias yields one that matches no role.
    """
    tail = invoked_arn.rsplit(":", 1)[-1]
    return tail if tail in ALIAS_ROLES else "unknown"


def counter_name(role: str) -> str:
    return f"tokens#{role}"
