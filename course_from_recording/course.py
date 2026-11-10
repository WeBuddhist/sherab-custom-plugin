"""
Write the approved draft into the course (phase 2), with the AI course creator's builder.

    write_course(job, lesson_audio) -> the usage keys of the sections it created

Every lesson the author kept becomes a subsection of four unit pages:

    Listen       the lesson's audio (Tibetan track, translator's track) and the teacher's own words
    Key points   the summary and the key points
    Check        one practice question per Problem component, not graded; no try limit of its own (the
                 course's "Maximum Attempts" setting applies: unlimited unless the course sets a number)
    Reflect      the reflection questions, as text (nothing is submitted)

Sections are added at the end of the course outline and nothing is published: authors keep editing the
draft in Studio. If any page can't be written, what was written is removed again (the builder's strict mode).
"""

import logging
from xml.sax.saxutils import escape

from botocore.exceptions import BotoCoreError, ClientError
from django.utils.translation import gettext as _

from ai_course_creator.services.course_builder import CourseBuildError, apply_course_json, user_can_author

from . import services
from .serializers import included_lessons

log = logging.getLogger(__name__)

# The course is written in Tibetan. Its page names and labels, in one place, to be checked by a Tibetan
# reader. ("སྦྱོང་ཚན།", "དྲི་བ།" and "འགྲེལ་བཤད།" are the words the platform's Tibetan courses already use.)
LABELS = {
    "listen": "ཉན་པ།",
    "key_points": "གནད་དོན་གཙོ་བོ།",
    "check": "སྦྱོང་ཚན།",
    "reflect": "བསམ་ཞིབ།",
    "summary": "བསྡུས་དོན།",
    "teaching": "གསུང་ཆོས།",
    "tibetan_track": "བོད་སྐད།",
    "translator_track": "ལོ་ཙཱ་བ།",
    "question": "དྲི་བ།",
    "explanation": "འགྲེལ་བཤད།",
}

TIBETAN_DIGITS = str.maketrans("0123456789", "༠༡༢༣༤༥༦༧༨༩")

# A long run of the teacher's lines (one speaker, no translator) is split into paragraphs of this many lines.
PARAGRAPH_LINES = 6


class CourseWriteError(Exception):
    """The course could not be written; the message is for the author."""


def teacher_paragraphs(lines, edits, first, last):
    """
    The teacher's own words from transcript lines ``first``..``last``, with the author's corrections:
    one paragraph per turn (another voice or a silence ends it), long turns split every few lines.
    """
    paragraphs, current = [], []

    def end_paragraph():
        if current:
            paragraphs.append(" ".join(current))
            current.clear()

    for index in range(first, min(last, len(lines) - 1) + 1):
        line = lines[index]
        if line.get("speaker") != "teacher":
            end_paragraph()
            continue
        text = (edits.get(str(index)) or line.get("text") or "").strip()
        if text:
            current.append(text)
            if len(current) >= PARAGRAPH_LINES:
                end_paragraph()
    end_paragraph()
    return paragraphs


def _player(label, url):
    return (
        f'<p class="recording-lesson__track"><strong>{escape(label)}</strong></p>\n'
        f'<audio controls preload="none" src="{escape(url, {chr(34): "&quot;"})}"></audio>'
    )


def listen_html(tracks, translator_label, paragraphs):
    parts = []
    if tracks.get("bo"):
        parts.append(_player(LABELS["tibetan_track"], tracks["bo"]))
    if tracks.get("translator"):
        parts.append(_player(translator_label, tracks["translator"]))
    if paragraphs:
        parts.append(f"<h3>{escape(LABELS['teaching'])}</h3>")
        parts.append('<div lang="bo">\n' + "\n".join(f"<p>{escape(text)}</p>" for text in paragraphs) + "\n</div>")
    return '<div class="recording-lesson recording-lesson--listen">\n' + "\n".join(parts) + "\n</div>"


def key_points_html(lesson):
    parts = []
    if lesson["summary"].strip():
        parts.append(f"<h3>{escape(LABELS['summary'])}</h3>\n<p>{escape(lesson['summary'].strip())}</p>")
    if lesson["key_points"]:
        items = "\n".join(f"<li>{escape(point)}</li>" for point in lesson["key_points"])
        parts.append(f"<h3>{escape(LABELS['key_points'])}</h3>\n<ul>\n{items}\n</ul>")
    return '<div class="recording-lesson" lang="bo">\n' + "\n".join(parts) + "\n</div>"


def reflect_html(lesson):
    items = "\n".join(f"<li>{escape(question)}</li>" for question in lesson["reflection"])
    return f'<div class="recording-lesson" lang="bo">\n<ol>\n{items}\n</ol>\n</div>'


def _html(title, body):
    return {"type": "html", "title": title, "body": body}


def lesson_units(lesson, tracks, translator_label, paragraphs):
    """The four pages of one lesson, in the AI course creator's course JSON. Empty pages are left out."""
    units = [{"title": LABELS["listen"], "components": [_html(LABELS["listen"], listen_html(
        tracks, translator_label, paragraphs
    ))]}]
    if lesson["summary"].strip() or lesson["key_points"]:
        units.append({"title": LABELS["key_points"], "components": [
            _html(LABELS["key_points"], key_points_html(lesson))
        ]})
    if lesson["mcq"]:
        units.append({"title": LABELS["check"], "components": [
            {
                "type": "problem",
                "problemType": "multiplechoice",
                "title": f"{LABELS['question']} {str(number).translate(TIBETAN_DIGITS)}",
                "question": question["question"],
                "choices": question["options"],
                "correct": [question["options"][question["answer"]]],
                "explanation": question.get("explanation", ""),
                "explanationLabel": LABELS["explanation"],
                # Practice: no limit of its own, so the course's Maximum Attempts setting applies.
                "maxAttempts": None,
            }
            for number, question in enumerate(lesson["mcq"], start=1)
        ]})
    if lesson["reflection"]:
        units.append({"title": LABELS["reflect"], "components": [_html(LABELS["reflect"], reflect_html(lesson))]})
    return units


def build_course_json(draft, lines, edits, audio_urls, translator_label):
    """The draft as the AI course creator's course JSON: only the lessons kept, and sections that have any."""
    sections = []
    for section in draft["sections"]:
        subsections = [
            {
                "title": lesson["title"],
                "units": lesson_units(
                    lesson,
                    audio_urls.get(lesson["id"], {}),
                    translator_label,
                    teacher_paragraphs(lines, edits, lesson["first_line"], lesson["last_line"]),
                ),
            }
            for lesson in section["lessons"]
            if lesson["include"]
        ]
        if subsections:
            sections.append({"title": section["title"], "subsections": subsections})
    return {"courseTitle": draft.get("course_title", ""), "sections": sections}


def write_course(job, lesson_audio):
    """
    Copy the lesson clips to the public bucket and write the job's approved draft into its course, as the
    person who started the job. ``lesson_audio``: lesson id -> {"tracks": {"bo"|"translator": path}}.
    Returns the new sections' usage keys. Raises CourseWriteError with a message for the author.
    """
    user = job.user
    if user is None or not user_can_author(user, job.course_id):
        raise CourseWriteError(_("The person who started this can no longer edit this course."))

    lessons = included_lessons(job.course_draft)
    if not lessons:
        raise CourseWriteError(_("No lessons were kept, so there is nothing to add to the course."))
    # Every lesson has the teacher's voice, so at least its Tibetan track.
    tracks = {lesson_id: audio["tracks"] for lesson_id, audio in lesson_audio.items()}
    missing = [lesson["id"] for lesson in lessons if not tracks.get(lesson["id"], {}).get("bo")]
    if missing:
        raise CourseWriteError(_("The audio of some lessons is missing: {lessons}.").format(lessons=", ".join(missing)))

    try:
        lines = services.read_json(job, job.results["transcript_json"])
    except (KeyError, ValueError, BotoCoreError, ClientError) as error:
        log.exception("Could not read the transcript of recording job %s", job.id)
        raise CourseWriteError(_("The transcript could not be read. Try again in a few minutes.")) from error

    try:
        audio_urls = {
            lesson["id"]: {
                track: services.copy_to_public(job, path)
                for track, path in tracks[lesson["id"]].items()
            }
            for lesson in lessons
        }
    except services.NotConfigured as error:
        raise CourseWriteError(_("The public audio bucket is not set up on this server.")) from error
    except (BotoCoreError, ClientError) as error:
        log.exception("Could not copy the lesson audio of recording job %s", job.id)
        raise CourseWriteError(_("The lesson audio could not be copied. Try again in a few minutes.")) from error

    translator_label = (job.metadata.get("translator_language") or "").strip() or LABELS["translator_track"]
    course_json = build_course_json(job.course_draft, lines, job.transcript_edits, audio_urls, translator_label)
    # Strict: a lesson missing a page is not what the author approved, so one page that can't be made
    # removes everything written and fails.
    try:
        result = apply_course_json(job.course_id, user, course_json, strict=True)
    except CourseBuildError as error:
        log.warning("Recording job %s: the builder failed: %s", job.id, error)
        raise CourseWriteError(
            _("The course could not be written into Studio, so nothing was added. Try again in a few minutes.")
        ) from error
    return result["sectionLocators"]
