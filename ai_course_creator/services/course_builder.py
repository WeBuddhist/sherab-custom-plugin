"""
Turn a COURSE_JSON outline (produced by the Sherab chatbot in Phase 5) into real
Open edX course structure: Sections (chapter) -> Subsections (sequential) ->
Units (vertical) -> Components (video / html / problem / discussion).

Runs inside the CMS process, so it calls ``contentstore``'s ``create_xblock``
and the modulestore directly rather than going back out over HTTP.

COURSE_JSON schema (see skill/SKILL.md):

    {
      "courseTitle": "...",
      "sections": [
        {"title": "...", "objective": "...", "subsections": [
            {"title": "...", "units": [
                {"title": "...", "components": [
                    {"type": "video", "title": "...", "source": "url"},
                    {"type": "html", "title": "...", "body": "..."},
                    {"type": "problem", "problemType": "multiplechoice|truefalse|shortanswer|scenario",
                     "question": "...", "choices": ["A","B"], "correct": "A"},
                    {"type": "discussion", "prompt": "..."}
                ]}
            ]}
        ]}
      ],
      "finalAssessment": [ {"type": "problem", ...} ]
    }
"""

import logging
from xml.sax.saxutils import escape

log = logging.getLogger(__name__)


class CourseBuildError(Exception):
    """Raised when the outline cannot be applied to the course."""


# --------------------------------------------------------------------------- #
# Permission helper
# --------------------------------------------------------------------------- #
def user_can_author(user, course_key):
    """Return True if ``user`` may author the given course."""
    from common.djangoapps.student.auth import has_course_author_access

    return has_course_author_access(user, course_key)


# --------------------------------------------------------------------------- #
# Problem OLX builders
# --------------------------------------------------------------------------- #
def _normalise_correct(correct):
    """Return the set of correct answer strings, accepting a str or a list."""
    if correct is None:
        return set()
    if isinstance(correct, (list, tuple)):
        return {str(c).strip() for c in correct if str(c).strip()}
    return {str(correct).strip()}


def _solution_xml(explanation, label=None):
    """
    Build the optional <solution> explanation block shared by all problems. ``label`` is its heading
    ("Explanation" unless given, e.g. in the course's own language).
    """
    if not explanation:
        return ""
    return (
        '  <solution>\n'
        '    <div class="detailed-solution">\n'
        f"      <p>{escape(label or 'Explanation')}</p>\n"
        f"      <p>{escape(explanation)}</p>\n"
        "    </div>\n"
        "  </solution>\n"
    )


def _multiple_choice_olx(question, choices, correct, explanation="", explanation_label=None):
    """Single-select MCQ (also used for True/False)."""
    correct_set = _normalise_correct(correct)
    choice_xml = []
    for choice in choices or []:
        is_correct = "true" if choice.strip() in correct_set else "false"
        choice_xml.append(f'      <choice correct="{is_correct}">{escape(choice)}</choice>')
    return (
        "<problem>\n"
        "  <multiplechoiceresponse>\n"
        f"    <label>{escape(question)}</label>\n"
        '    <choicegroup type="MultipleChoice">\n'
        + "\n".join(choice_xml)
        + "\n    </choicegroup>\n"
        "  </multiplechoiceresponse>\n"
        + _solution_xml(explanation, explanation_label)
        + "</problem>"
    )


def _multi_select_olx(question, choices, correct, explanation=""):
    """Checkbox / multi-answer problem (choiceresponse)."""
    correct_set = _normalise_correct(correct)
    choice_xml = []
    for choice in choices or []:
        is_correct = "true" if choice.strip() in correct_set else "false"
        choice_xml.append(f'      <choice correct="{is_correct}">{escape(choice)}</choice>')
    return (
        "<problem>\n"
        "  <choiceresponse>\n"
        f"    <label>{escape(question)}</label>\n"
        "    <checkboxgroup>\n"
        + "\n".join(choice_xml)
        + "\n    </checkboxgroup>\n"
        "  </choiceresponse>\n"
        + _solution_xml(explanation)
        + "</problem>"
    )


def _dropdown_olx(question, choices, correct, explanation=""):
    """Dropdown / select problem (optionresponse)."""
    correct_set = _normalise_correct(correct)
    option_xml = []
    for choice in choices or []:
        is_correct = "true" if choice.strip() in correct_set else "false"
        option_xml.append(f'      <option correct="{is_correct}">{escape(choice)}</option>')
    return (
        "<problem>\n"
        "  <optionresponse>\n"
        f"    <label>{escape(question)}</label>\n"
        "    <optioninput>\n"
        + "\n".join(option_xml)
        + "\n    </optioninput>\n"
        "  </optionresponse>\n"
        + _solution_xml(explanation)
        + "</problem>"
    )


def _short_answer_olx(question, correct, explanation=""):
    """Free-text answer problem (stringresponse, case-insensitive)."""
    correct_list = sorted(_normalise_correct(correct))
    answer = escape(correct_list[0]) if correct_list else ""
    extra_answers = "".join(
        f'    <additional_answer answer="{escape(a)}"/>\n' for a in correct_list[1:]
    )
    return (
        "<problem>\n"
        '  <stringresponse answer="' + answer + '" type="ci">\n'
        f"    <label>{escape(question)}</label>\n"
        + extra_answers
        + '    <textline size="40"/>\n'
        + _solution_xml(explanation)
        + "  </stringresponse>\n"
        "</problem>"
    )


def _numerical_olx(question, correct, explanation="", tolerance="5%"):
    """Numerical answer problem (numericalresponse)."""
    correct_list = sorted(_normalise_correct(correct))
    answer = escape(correct_list[0]) if correct_list else "0"
    return (
        "<problem>\n"
        f'  <numericalresponse answer="{answer}">\n'
        f"    <label>{escape(question)}</label>\n"
        f'    <responseparam type="tolerance" default="{tolerance}"/>\n'
        "    <formulaequationinput/>\n"
        + _solution_xml(explanation)
        + "  </numericalresponse>\n"
        "</problem>"
    )


def build_problem_olx(component):
    """Build CAPA OLX for a problem component dict (one of the 7 supported types)."""
    problem_type = (component.get("problemType") or "multiplechoice").lower()
    question = component.get("question") or component.get("title") or "Question"
    choices = component.get("choices") or []
    correct = component.get("correct")
    explanation = component.get("explanation") or ""
    # The explanation's heading, e.g. in the course's own language ("Explanation" if not given).
    label = component.get("explanationLabel")

    if problem_type in ("truefalse", "true_false", "true/false"):
        # Normalise the correct answer to one of the two labels.
        correct_set = {c.lower() for c in _normalise_correct(correct)}
        correct_label = "False" if correct_set & {"false", "f", "no"} else "True"
        return _multiple_choice_olx(question, ["True", "False"], correct_label, explanation)
    if problem_type in ("multiselect", "multi_select", "checkbox", "choiceresponse"):
        return _multi_select_olx(question, choices, correct, explanation)
    if problem_type in ("dropdown", "optionresponse", "select"):
        return _dropdown_olx(question, choices, correct, explanation)
    if problem_type in ("numerical", "numericalresponse", "number"):
        return _numerical_olx(question, correct, explanation)
    if problem_type in ("shortanswer", "short_answer", "stringresponse", "text", "textinput"):
        return _short_answer_olx(question, correct, explanation)
    if problem_type in ("multiplechoice", "multiple_choice", "mcq", "singleselect", "single_select"):
        return _multiple_choice_olx(question, choices, correct, explanation, label)
    # Anything unknown: multiple choice if we have options, else short answer.
    if choices:
        return _multiple_choice_olx(question, choices, correct, explanation, label)
    return _short_answer_olx(question, correct, explanation)


# --------------------------------------------------------------------------- #
# Component creation
# --------------------------------------------------------------------------- #
def _max_attempts(component):
    """
    How many tries a problem allows: 1 unless the component says otherwise. ``"maxAttempts": None`` sets no
    limit of the problem's own, so the course's "Maximum Attempts" advanced setting applies (unlimited
    while that is empty). Anything that isn't a positive whole number (e.g. from a model's JSON) counts as 1.
    """
    value = component.get("maxAttempts", 1)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 1
    return value


def _create_component(store, user, parent_locator, component):
    """Create a single leaf component under a unit (vertical)."""
    from cms.djangoapps.contentstore.xblock_storage_handlers.create_xblock import create_xblock

    comp_type = (component.get("type") or "html").lower()
    title = component.get("title")
    if not title and comp_type == "problem":
        # Use the problem type as the display_name rather than the question text.
        # Open edX renders display_name in both the Studio block header AND as an
        # internal bold heading inside the block frame, so using the question text
        # would cause it to appear twice before the OLX <label> adds a third copy.
        problem_type = (component.get("problemType") or "multiplechoice").lower()
        _type_labels = {
            "multiplechoice": "Multiple Choice",
            "multiple_choice": "Multiple Choice",
            "mcq": "Multiple Choice",
            "truefalse": "True / False",
            "true_false": "True / False",
            "multiselect": "Multi-Select",
            "multi_select": "Multi-Select",
            "checkbox": "Multi-Select",
            "dropdown": "Dropdown",
            "shortanswer": "Short Answer",
            "short_answer": "Short Answer",
            "numerical": "Numerical",
        }
        title = _type_labels.get(problem_type, "Question")
    title = title or comp_type.capitalize()

    if comp_type == "video":
        # We never auto-fill a video URL. Instead we drop an empty video block
        # whose name tells the creator exactly what to find or record, plus a
        # short HTML note for the same guidance, so the slot is obvious in the
        # outline. The creator supplies the real video later.
        description = (component.get("description") or component.get("body") or "").strip()
        guidance = description or "Add a video for this unit."
        note = create_xblock(parent_locator, user, "html", "🎥 Video guidance")
        note.data = (
            '<div style="border-left:3px solid #0075b4;padding:0.25rem 0.75rem;">'
            f"<p><strong>🎥 Video needed:</strong> {escape(guidance)}</p>"
            "<p><em>Replace this note and the empty video below with your own video.</em></p>"
            "</div>"
        )
        store.update_item(note, user.id)
        video_name = ("🎥 " + guidance)[:120]
        block = create_xblock(parent_locator, user, "video", video_name)
        store.update_item(block, user.id)
        return "video"

    if comp_type == "problem":
        block = create_xblock(parent_locator, user, "problem", title)
        block.data = build_problem_olx(component)
        max_attempts = _max_attempts(component)
        if max_attempts is not None:  # None: left unset, so the course's setting applies
            block.max_attempts = max_attempts
        store.update_item(block, user.id)
        return "problem"

    # Default: HTML/text component.
    block = create_xblock(parent_locator, user, "html", title)
    body = component.get("body") or component.get("summary") or ""
    block.data = body if body.strip().startswith("<") else f"<p>{escape(body)}</p>"
    store.update_item(block, user.id)
    return "html"


def apply_course_json(course_key, user, course_json, strict=False):
    """
    Build the full course structure described by ``course_json``.

    Args:
        course_key: a CourseKey (or string) for the target course.
        user: the authoring user.
        course_json: the parsed COURSE_JSON dict.
        strict: if True, a component that can't be created fails the whole build (and what was made is
            removed again) instead of being skipped.

    Returns:
        dict summary: counts of created blocks and the created section locators.

    Raises:
        CourseBuildError: on bad input or permission failure.
    """
    from opaque_keys import InvalidKeyError
    from opaque_keys.edx.keys import CourseKey
    from cms.djangoapps.contentstore.xblock_storage_handlers.create_xblock import create_xblock
    from xmodule.modulestore.django import modulestore

    if isinstance(course_key, str):
        try:
            course_key = CourseKey.from_string(course_key)
        except InvalidKeyError as exc:
            raise CourseBuildError(f"Invalid course id: {course_key}") from exc

    if not user_can_author(user, course_key):
        raise CourseBuildError("You do not have permission to edit this course.")

    if not isinstance(course_json, dict) or not course_json.get("sections"):
        raise CourseBuildError("The generated outline has no sections to create.")

    store = modulestore()
    counts = {"sections": 0, "subsections": 0, "units": 0, "components": 0}
    section_locators = []

    try:
        with store.bulk_operations(course_key):
            course = store.get_course(course_key)
            if course is None:
                raise CourseBuildError(f"Course not found: {course_key}")
            course_locator = str(course.location)

            for section in course_json["sections"]:
                chapter = create_xblock(
                    course_locator, user, "chapter", section.get("title") or "Section"
                )
                counts["sections"] += 1
                section_locators.append(str(chapter.location))
                chapter_locator = str(chapter.location)

                for subsection in section.get("subsections", []):
                    sequential = create_xblock(
                        chapter_locator, user, "sequential", subsection.get("title") or "Subsection"
                    )
                    counts["subsections"] += 1
                    sequential_locator = str(sequential.location)

                    for unit in subsection.get("units", []):
                        vertical = create_xblock(
                            sequential_locator, user, "vertical", unit.get("title") or "Unit"
                        )
                        counts["units"] += 1
                        vertical_locator = str(vertical.location)

                        for component in unit.get("components", []):
                            # A single bad component shouldn't abort the build.
                            try:
                                _create_component(store, user, vertical_locator, component)
                                counts["components"] += 1
                            except Exception:  # pylint: disable=broad-except
                                if strict:
                                    raise
                                log.exception(
                                    "ai_course_creator: failed to create component %s", component
                                )
    except CourseBuildError:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        # Structural failure (section/subsection/unit). Roll back what we made so
        # the course is never left half-built, then surface a clean error.
        log.exception("ai_course_creator: build failed, rolling back %s", section_locators)
        delete_sections(course_key, user, section_locators)
        raise CourseBuildError(
            "Could not build the full course. Any partial content was removed — please retry."
        ) from exc

    log.info("ai_course_creator: built course %s -> %s", course_key, counts)
    return {"counts": counts, "sectionLocators": section_locators}


def delete_sections(course_key, user, section_locators):
    """
    Best-effort deletion of previously created top-level sections (chapters).

    Used both for rollback after a failed build and to clear a prior Sherab run
    before regenerating. Deleting a chapter removes its whole subtree.
    """
    from opaque_keys import InvalidKeyError
    from opaque_keys.edx.keys import CourseKey, UsageKey
    from xmodule.modulestore.django import modulestore

    if isinstance(course_key, str):
        try:
            course_key = CourseKey.from_string(course_key)
        except InvalidKeyError:
            return

    store = modulestore()
    with store.bulk_operations(course_key):
        for locator in section_locators or []:
            try:
                store.delete_item(UsageKey.from_string(locator), user.id)
            except Exception:  # pylint: disable=broad-except
                log.warning("ai_course_creator: could not delete %s during rollback", locator)
