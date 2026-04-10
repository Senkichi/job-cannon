"""Guidelines blueprint -- style guide migration and guidelines merge routes."""

import html as html_module
import json
import logging
from pathlib import Path

from flask import Blueprint, current_app, request

from job_finder.config import DEFAULT_MODEL_SONNET
from job_finder.web.db_helpers import get_db
from job_finder.web.resume_style_guide import (
    FIELD_LABELS,
    STYLE_GUIDE_SCHEMA,
    load_style_guide,
    merge_guidelines_into_guide,
    migrate_style_guide,
    save_style_guide,
)

logger = logging.getLogger(__name__)

guidelines_bp = Blueprint("guidelines", __name__, url_prefix="/settings")

@guidelines_bp.route("/migrate-style-guide", methods=["POST"], strict_slashes=False)
def migrate_style_guide_route():
    """Run Sonnet-powered migration of the style guide to populate new guideline fields."""
    try:
        config = current_app.config.get("JF_CONFIG", {})
        db_path = current_app.config["DB_PATH"]
        conn = get_db(db_path)
        result = migrate_style_guide(config, conn)
        if result:
            field_count = sum(1 for v in result.values() if v)
            return (
                f'<div id="style-guide-migrate-section" class="text-xs text-emerald-400">'
                f'Style guide migrated. {field_count} fields now populated.'
                f'</div>',
                200,
            )
        else:
            return (
                '<div id="style-guide-migrate-section" class="text-xs text-red-400">'
                'Migration failed: Sonnet returned no result. Check logs for details.'
                '<button type="button" hx-post="/settings/migrate-style-guide" '
                'hx-target="#style-guide-migrate-section" hx-swap="outerHTML" '
                'class="ml-2 text-xs text-violet-400 hover:text-violet-300">Retry</button>'
                '</div>',
                200,
            )
    except Exception as exc:
        logger.warning("migrate_style_guide_route: %s", exc)
        return (
            f'<div id="style-guide-migrate-section" class="text-xs text-red-400">'
            f'Migration failed: {exc}. Check logs for details.'
            f'<button type="button" hx-post="/settings/migrate-style-guide" '
            f'hx-target="#style-guide-migrate-section" hx-swap="outerHTML" '
            f'class="ml-2 text-xs text-violet-400 hover:text-violet-300">Retry</button>'
            f'</div>',
            200,
        )

@guidelines_bp.route("/preview-guidelines-merge", methods=["POST"], strict_slashes=False)
def preview_guidelines_merge():
    """Preview a field-by-field diff of merging updated guidelines into the style guide.

    Calls Sonnet once with mode="merge_updates" and returns a diff fragment
    with the merged result stashed as a hidden input. Applying uses the stash
    without a second API call.
    """
    try:
        guidelines_text = request.form.get("guidelines_text", "").strip()
        if not guidelines_text:
            return (
                '<div id="guidelines-diff-container" class="text-xs text-red-400">'
                "Please enter guidelines text."
                "</div>",
                200,
            )

        existing_guide = load_style_guide()
        config = current_app.config.get("JF_CONFIG", {})
        db_path = current_app.config["DB_PATH"]
        conn = get_db(db_path)
        model = (
            config.get("scoring", {})
            .get("models", {})
            .get("sonnet", DEFAULT_MODEL_SONNET)
        )

        result = merge_guidelines_into_guide(
            guidelines_text=guidelines_text,
            existing_guide=existing_guide,
            model=model,
            conn=conn,
            config=config,
            mode="merge_updates",
        )

        if result is None:
            return (
                '<div id="guidelines-diff-container" class="text-xs text-red-400">'
                "Preview failed: Sonnet returned no result. Check logs for details."
                "</div>",
                200,
            )

        # Build field-by-field diff HTML
        diff_rows = []
        for field in STYLE_GUIDE_SCHEMA["properties"]:
            old_val = existing_guide.get(field, "")
            new_val = result.get(field, "")
            label = FIELD_LABELS.get(field, field)

            # Stringify lists/dicts for comparison
            old_str = json.dumps(old_val, ensure_ascii=False) if isinstance(old_val, (list, dict)) else str(old_val) if old_val else ""
            new_str = json.dumps(new_val, ensure_ascii=False) if isinstance(new_val, (list, dict)) else str(new_val) if new_val else ""

            if old_str == new_str:
                diff_rows.append(
                    f'<div class="flex items-start gap-2 py-1 border-b border-slate-700/50">'
                    f'<span class="text-slate-500 w-40 flex-shrink-0 font-medium">{label}</span>'
                    f'<span class="text-slate-500 text-xs italic">No change</span>'
                    f"</div>"
                )
            elif not old_str and new_str:
                diff_rows.append(
                    f'<div class="flex items-start gap-2 py-1 border-b border-slate-700/50">'
                    f'<span class="text-slate-300 w-40 flex-shrink-0 font-medium">{label}</span>'
                    f'<span class="text-slate-500 italic mr-2">(empty)</span>'
                    f'<span class="text-slate-400 mr-2">&rarr;</span>'
                    f'<span class="text-emerald-400">{new_str}</span>'
                    f"</div>"
                )
            elif old_str and not new_str:
                # Sonnet returned empty for a field that had content — likely
                # a token-limit truncation.  Preserve the existing value and
                # warn instead of showing a destructive diff.
                result[field] = old_val  # patch the merged result
                diff_rows.append(
                    f'<div class="flex items-start gap-2 py-1 border-b border-slate-700/50">'
                    f'<span class="text-amber-400 w-40 flex-shrink-0 font-medium">{label}</span>'
                    f'<span class="text-amber-400 text-xs italic">Kept existing (model returned empty)</span>'
                    f"</div>"
                )
            else:
                diff_rows.append(
                    f'<div class="flex items-start gap-2 py-1 border-b border-slate-700/50">'
                    f'<span class="text-slate-300 w-40 flex-shrink-0 font-medium">{label}</span>'
                    f'<span class="text-red-400 line-through mr-2">{old_str}</span>'
                    f'<span class="text-slate-400 mr-2">&rarr;</span>'
                    f'<span class="text-emerald-400">{new_str}</span>'
                    f"</div>"
                )

        merged_json = json.dumps(result, ensure_ascii=False)
        escaped_json = html_module.escape(merged_json, quote=True)

        diff_html = "\n".join(diff_rows)
        apply_button = (
            f'<div class="mt-4 flex items-center gap-3">'
            f'<input type="hidden" name="merged_guide_json" value="{escaped_json}">'
            f'<input type="hidden" name="guidelines_text" id="stashed-guidelines-text" value="{html_module.escape(guidelines_text, quote=True)}">'
            f'<button type="button"'
            f' hx-post="/settings/apply-guidelines-merge"'
            f' hx-target="#guidelines-diff-container"'
            f' hx-swap="innerHTML"'
            f' hx-include="[name=merged_guide_json],[name=guidelines_text]"'
            f' hx-disabled-elt="this"'
            f' class="px-3 py-1 bg-emerald-700 hover:bg-emerald-600 text-white rounded text-sm transition-colors">'
            f"Apply Changes"
            f"</button>"
            f'<span class="text-xs text-slate-500">No extra API cost — uses the cached preview result.</span>'
            f"</div>"
        )

        fragment = (
            f'<div id="guidelines-diff-container" class="mt-2">'
            f'<h4 class="text-xs font-semibold text-slate-300 mb-2">Field-by-field preview</h4>'
            f'<div class="text-xs font-mono space-y-0">{diff_html}</div>'
            f"{apply_button}"
            f"</div>"
        )

        return fragment, 200

    except Exception as exc:
        logger.warning("preview_guidelines_merge: %s", exc, exc_info=True)
        return (
            f'<div id="guidelines-diff-container" class="text-xs text-red-400">'
            f"Preview failed: {exc}. Check logs for details."
            f"</div>",
            200,
        )

@guidelines_bp.route("/apply-guidelines-merge", methods=["POST"], strict_slashes=False)
def apply_guidelines_merge():
    """Apply the stashed merged style guide result (from preview) without a second Sonnet call."""
    try:
        merged_json = request.form.get("merged_guide_json", "").strip()
        if not merged_json:
            return (
                '<div id="guidelines-diff-container" class="text-xs text-red-400">'
                "Apply failed: no merged guide data found. Please preview again."
                "</div>",
                200,
            )

        merged_guide = json.loads(merged_json)

        save_style_guide(merged_guide)

        # Also persist the guidelines text to docs/resume_generation_guidelines.md if provided
        guidelines_text = request.form.get("guidelines_text", "").strip()
        if guidelines_text:
            guidelines_path = Path(__file__).resolve().parent.parent.parent.parent / "docs" / "resume_generation_guidelines.md"
            guidelines_path.write_text(guidelines_text, encoding="utf-8")
            logger.info("apply_guidelines_merge: updated resume_generation_guidelines.md")

        logger.info("apply_guidelines_merge: saved style guide with %d fields", len(merged_guide))
        return (
            '<div id="guidelines-diff-container" class="text-xs text-emerald-400">'
            "Guidelines applied successfully. Style guide updated."
            "</div>",
            200,
        )

    except json.JSONDecodeError as exc:
        logger.warning("apply_guidelines_merge: invalid JSON: %s", exc)
        return (
            '<div id="guidelines-diff-container" class="text-xs text-red-400">'
            "Apply failed: invalid merged guide data. Please preview again."
            "</div>",
            200,
        )
    except Exception as exc:
        logger.warning("apply_guidelines_merge: %s", exc, exc_info=True)
        return (
            f'<div id="guidelines-diff-container" class="text-xs text-red-400">'
            f"Apply failed: {exc}. Check logs for details."
            f"</div>",
            200,
        )
