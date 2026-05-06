"""Module-level constants for the pipeline detector.

Extracted from the legacy ``pipeline_detector.py`` monolith so the
classification and signal logic can be read without 130 lines of
keyword tables crowding the top of the file. All values are immutable
(``frozenset`` / ``tuple`` semantics where appropriate) and meant to
be swapped only by code review, not at runtime.
"""

# ---------------------------------------------------------------------------
# Gmail query patterns (from research — OR-combined for maximum recall)
# ---------------------------------------------------------------------------

REJECTION_QUERY = (
    'subject:("unfortunately" OR "not moving forward" OR '
    '"other direction" OR "other candidates" OR "not selected" OR '
    '"position has been filled" OR "no longer" OR "decided not to proceed") '
    "newer_than:3d"
)

INTERVIEW_QUERY = (
    'subject:("interview" OR "next steps" OR "phone screen" OR '
    '"technical interview" OR "schedule time" OR "meet with") '
    "newer_than:3d"
)

CONFIRMATION_QUERY = (
    'subject:("application received" OR "thank you for applying" OR '
    '"application confirmation" OR "we received your application" OR '
    '"successfully submitted") '
    "newer_than:3d"
)

# ---------------------------------------------------------------------------
# Classification keyword sets
# ---------------------------------------------------------------------------

REJECTION_KEYWORDS = [
    "unfortunately",
    "not moving forward",
    "other candidates",
    "not selected",
    "position has been filled",
    "no longer moving forward",
    "decided not to proceed",
    "other direction",
    "will not be moving forward",
    "not proceed",
    "filled the position",
]

INTERVIEW_KEYWORDS = [
    "interview",
    "phone screen",
    "next steps",
    "technical interview",
    "schedule time",
    "meet with",
    "speak with",
    "chat with",
    "call with",
    "video call",
    "hiring process",
]

CONFIRMATION_KEYWORDS = [
    "application received",
    "thank you for applying",
    "application confirmation",
    "we received your application",
    "successfully submitted",
    "received your application",
    "thank you for your application",
]

# Maps Gmail query detection_type to classification
QUERY_DETECTION_TYPES = {
    REJECTION_QUERY: "rejection",
    INTERVIEW_QUERY: "interview",
    CONFIRMATION_QUERY: "confirmation",
}

# Maps detection_type to the pipeline status transition target
DETECTION_TYPE_TO_STATUS = {
    "rejection": "rejected",
    "interview": "phone_screen",
    "confirmation": "applied",
}

# Signal keywords for snippet extraction
SIGNAL_KEYWORDS = {
    "rejection": REJECTION_KEYWORDS,
    "interview": INTERVIEW_KEYWORDS,
    "confirmation": CONFIRMATION_KEYWORDS,
}

# ---------------------------------------------------------------------------
# ATS domain list
# ---------------------------------------------------------------------------

ATS_DOMAINS = {
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "workday.com",
    "myworkday.com",
    "taleo.net",
    "icims.com",
    "jobvite.com",
    "smartrecruiters.com",
    "breezy.hr",
    "jazz.co",
    "workable.com",
    "recruitee.com",
    "bamboohr.com",
    "successfactors.com",
    "kronos.net",
    "rippling.com",
    "pinpointhq.com",
}

# Pipeline statuses that indicate a job is no longer active
INACTIVE_STATUSES = {"archived", "rejected", "withdrawn"}

# Common job title words to exclude from title matching
TITLE_STOP_WORDS = {
    "senior",
    "staff",
    "lead",
    "data",
    "the",
    "and",
    "for",
    "with",
    "principal",
    "associate",
    "junior",
    "mid",
    "level",
}
