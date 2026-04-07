"""Foundation-layer constants shared across all layers."""

PIPELINE_STATUSES = (
    "discovered",
    "reviewing",
    "applied",
    "phone_screen",
    "technical",
    "onsite",
    "offer",
    "accepted",
    "archived",
    "rejected",
    "withdrawn",
    "dismissed",
)

VALID_PIPELINE_STATUSES = frozenset(PIPELINE_STATUSES)
