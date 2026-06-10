"""Recipe schema dataclasses and strict validation for autoheal declarative recipes.

Two recipe types:
- HtmlRecipe: CSS-selector + per-field map for HTML documents — email bodies
  (surface "email") and rendered careers pages (surface "careers", D4).
- AtsAliasRecipe: extra field alias lists for ATS platforms (merged after canonical lists).

validate_recipe() is pure (no I/O), strict (unknown keys raise), and returns
frozen dataclasses. Phase C reads use config.get('autoheal', {}).get(...) — never
bracket access — to avoid crashing installs that haven't added the config block yet.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

# ---------------------------------------------------------------------------
# FieldRule (used inside HtmlRecipe.fields)
# ---------------------------------------------------------------------------

_ALLOWED_FIELD_RULE_KEYS = frozenset({"selector", "attr", "regex", "group"})
_ALLOWED_HTML_FIELD_NAMES = frozenset({"title", "url", "company", "location"})
_HTML_REQUIRED_FIELDS = frozenset({"title", "url"})

_ALLOWED_HTML_RECIPE_KEYS = frozenset({"source", "container_selector", "fields"})
_ALLOWED_ATS_RECIPE_KEYS = frozenset({"source", "title_fields", "url_fields", "array_keys"})


@dataclass(frozen=True)
class FieldRule:
    """A per-field extraction rule for HtmlRecipe.

    Attributes:
        selector: CSS selector applied to a container block.
        attr: ``"text"`` → ``get_text(strip=True)``; any other value → element attribute.
        regex: Optional regex pattern; applied to the extracted string after attr extraction.
        group: Regex capture group to use (default 0 = full match).
    """

    selector: str
    attr: str
    regex: str | None = None
    group: int = 0


@dataclass(frozen=True)
class HtmlRecipe:
    """Declarative recipe for extracting jobs from an HTML email body.

    Attributes:
        source: The SENDER_LABEL value (e.g. ``"linkedin"``).
        container_selector: CSS selector matching one container block per job.
        fields: Map of field name → FieldRule. Required keys: ``title``, ``url``.
                Optional: ``company``, ``location``.
    """

    source: str
    container_selector: str
    fields: dict[str, FieldRule]


@dataclass(frozen=True)
class AtsAliasRecipe:
    """Declarative recipe adding extra field aliases for an ATS platform.

    Aliases are appended AFTER the canonical lists so first-match-wins on
    un-renamed fields is preserved. At least one list must be non-empty.

    Attributes:
        source: The ATS source key (e.g. ``"ats:lever"``).
        title_fields: Extra keys to try for job title (after canonical list).
        url_fields: Extra keys to try for job URL (after canonical list).
        array_keys: Extra keys to try when locating the jobs array (after canonical list).
    """

    source: str
    title_fields: list[str]
    url_fields: list[str]
    array_keys: list[str]


# ---------------------------------------------------------------------------
# validate_recipe
# ---------------------------------------------------------------------------


def recipe_to_dict(recipe: HtmlRecipe | AtsAliasRecipe) -> dict:
    """Serialize a frozen recipe back to the plain-dict form validate_recipe accepts.

    Round-trip guarantee: ``validate_recipe(surface, recipe_to_dict(r)) == r``.
    Used by the validator worker protocol and by write_override on adoption.
    """
    return asdict(recipe)


def validate_recipe(surface: str, data: dict) -> HtmlRecipe | AtsAliasRecipe:
    """Strictly validate a raw recipe dict and return the appropriate frozen dataclass.

    Args:
        surface: ``"email"``, ``"careers"`` (both → HtmlRecipe), or ``"ats"``.
        data: Raw dict (typically from JSON deserialization).

    Returns:
        A frozen ``HtmlRecipe`` or ``AtsAliasRecipe``.

    Raises:
        ValueError: On any schema violation — unknown surface, unknown keys,
                    missing required fields, empty lists, non-string values.
    """
    if surface not in ("email", "ats", "careers"):
        raise ValueError(f"Unknown surface {surface!r}; expected 'email', 'ats', or 'careers'")
    if surface in ("email", "careers"):
        return _validate_html_recipe(data)
    return _validate_ats_alias_recipe(data)


def _validate_html_recipe(data: dict) -> HtmlRecipe:
    # Strict key check
    unknown = set(data.keys()) - _ALLOWED_HTML_RECIPE_KEYS
    if unknown:
        raise ValueError(f"Unknown top-level key(s) in HTML recipe: {sorted(unknown)}")

    source = data.get("source", "")
    container_selector = data.get("container_selector")
    if not container_selector:
        raise ValueError("HTML recipe requires a non-empty 'container_selector'")

    raw_fields = data.get("fields")
    if not raw_fields:
        raise ValueError("HTML recipe 'fields' must be a non-empty mapping")

    # Check for unknown field names
    unknown_fields = set(raw_fields.keys()) - _ALLOWED_HTML_FIELD_NAMES
    if unknown_fields:
        raise ValueError(f"Unknown field name(s) in HTML recipe: {sorted(unknown_fields)}")

    # Check required field names
    for req in _HTML_REQUIRED_FIELDS:
        if req not in raw_fields:
            raise ValueError(f"HTML recipe 'fields' must include required key '{req}'")

    parsed_fields: dict[str, FieldRule] = {}
    for name, rule_data in raw_fields.items():
        parsed_fields[name] = _parse_field_rule(name, rule_data)

    return HtmlRecipe(
        source=source,
        container_selector=container_selector,
        fields=parsed_fields,
    )


def _parse_field_rule(field_name: str, rule_data: dict) -> FieldRule:
    if not isinstance(rule_data, dict):
        raise ValueError(
            f"FieldRule for '{field_name}' must be a dict, got {type(rule_data).__name__}"
        )
    unknown = set(rule_data.keys()) - _ALLOWED_FIELD_RULE_KEYS
    if unknown:
        raise ValueError(f"Unknown key(s) in FieldRule for '{field_name}': {sorted(unknown)}")
    selector = rule_data.get("selector", "")
    attr = rule_data.get("attr", "")
    regex = rule_data.get("regex")
    group = rule_data.get("group", 0)
    return FieldRule(selector=selector, attr=attr, regex=regex, group=group)


def _validate_ats_alias_recipe(data: dict) -> AtsAliasRecipe:
    # Strict key check
    unknown = set(data.keys()) - _ALLOWED_ATS_RECIPE_KEYS
    if unknown:
        raise ValueError(f"Unknown top-level key(s) in ATS alias recipe: {sorted(unknown)}")

    source = data.get("source", "")

    title_fields = data.get("title_fields", [])
    url_fields = data.get("url_fields", [])
    array_keys = data.get("array_keys", [])

    for list_name, lst in [
        ("title_fields", title_fields),
        ("url_fields", url_fields),
        ("array_keys", array_keys),
    ]:
        if not isinstance(lst, list):
            raise ValueError(
                f"ATS alias recipe '{list_name}' must be a list, got {type(lst).__name__}"
            )
        for item in lst:
            if not isinstance(item, str):
                raise ValueError(
                    f"ATS alias recipe '{list_name}' must be a list of strings; "
                    f"got {type(item).__name__}"
                )
            if not item:
                raise ValueError(
                    f"ATS alias recipe '{list_name}' contains an empty string; "
                    "all alias values must be non-empty"
                )

    if not title_fields and not url_fields and not array_keys:
        raise ValueError(
            "ATS alias recipe must have at least one non-empty alias list "
            "(title_fields, url_fields, or array_keys)"
        )

    return AtsAliasRecipe(
        source=source,
        title_fields=list(title_fields),
        url_fields=list(url_fields),
        array_keys=list(array_keys),
    )
