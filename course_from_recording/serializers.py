"""
Request validation for the course_from_recording APIs.
"""

import os
import posixpath

from django.conf import settings
from django.utils.translation import gettext as _
from rest_framework import serializers

from .models import SERVICE_STATUSES

ALLOWED_EXTENSIONS = (".mp3", ".wav", ".m4a", ".flac", ".ogg")

# Languages the teacher can be transcribed in: Tibetan (by Monlam AI) and English (by Gemini).
TRANSCRIBED_LANGUAGES = ("bo", "en")


class RecordingDetailsSerializer(serializers.Serializer):
    """What the author tells us about the recording; stored as the job's metadata."""

    teacher = serializers.CharField(max_length=255)
    # With more than one speaker (e.g. a teacher and a translator) the service finds the speakers,
    # the author tags them, and only the teacher is transcribed. With one, the whole recording is.
    multiple_speakers = serializers.BooleanField()
    teacher_language = serializers.CharField(max_length=16)
    translator_language = serializers.CharField(max_length=64, required=False, allow_blank=True)
    text = serializers.CharField(max_length=255, required=False, allow_blank=True)
    place = serializers.CharField(max_length=255, required=False, allow_blank=True)
    year = serializers.IntegerField(min_value=1900, max_value=2100, required=False)

    def validate_teacher_language(self, value):
        if value not in TRANSCRIBED_LANGUAGES:
            raise serializers.ValidationError(_("Only Tibetan and English recordings are supported for now."))
        return value

    def validate(self, attrs):
        if not attrs["multiple_speakers"]:
            attrs.pop("translator_language", None)
        return attrs


class JobCreateSerializer(serializers.Serializer):
    course_id = serializers.CharField()
    filename = serializers.CharField(max_length=255)
    content_type = serializers.CharField(max_length=100)
    size = serializers.IntegerField(min_value=1)
    consent = serializers.BooleanField()
    details = RecordingDetailsSerializer()

    def validate_filename(self, value):
        if os.path.splitext(value)[1].lower() not in ALLOWED_EXTENSIONS:
            raise serializers.ValidationError(
                _("Upload an audio file ({extensions}).").format(extensions=", ".join(ALLOWED_EXTENSIONS))
            )
        return value

    def validate_content_type(self, value):
        if not value.startswith("audio/"):
            raise serializers.ValidationError(_("Upload an audio file."))
        return value

    def validate_size(self, value):
        max_mb = settings.COURSE_FROM_RECORDING_MAX_UPLOAD_MB
        if value > max_mb * 1024 * 1024:
            raise serializers.ValidationError(_("The file is larger than {max_mb} MB.").format(max_mb=max_mb))
        return value

    def validate_consent(self, value):
        if not value:
            raise serializers.ValidationError(_("Confirm you have permission to use this recording."))
        return value


class TranscriptEditSerializer(serializers.Serializer):
    """One corrected line of the transcript; ``text: null`` puts the line back as it was written down."""

    line = serializers.IntegerField(min_value=0, max_value=100000)
    text = serializers.CharField(max_length=5000, allow_null=True, allow_blank=False, trim_whitespace=True)


class StartOverSerializer(serializers.Serializer):
    keep_recording = serializers.BooleanField(default=True)


class ReuseRecordingSerializer(serializers.Serializer):
    """A new job for a kept recording: the details again (the author may have changed them)."""

    consent = serializers.BooleanField()
    details = RecordingDetailsSerializer()

    validate_consent = JobCreateSerializer.validate_consent


def validate_result_path(path):
    """A path inside the job's S3 folder, e.g. 'transcript/corrected.txt'; nothing that climbs out of it."""
    normalised = posixpath.normpath(path)
    if not path or path.startswith("/") or normalised != path or normalised.startswith(".."):
        raise serializers.ValidationError(_("{path} is not a path inside the job folder.").format(path=path))
    return path


class SpeakerGroupSerializer(serializers.Serializer):
    """One voice the service found: how long it speaks, and short clips for the author to listen to."""

    group = serializers.CharField(max_length=32)
    minutes = serializers.FloatField(min_value=0)
    samples = serializers.ListField(
        child=serializers.CharField(max_length=512, validators=[validate_result_path]), min_length=1, max_length=5
    )


class MultipleChoiceSerializer(serializers.Serializer):
    """One practice question: options, the index of the right one, and why it is right."""

    question = serializers.CharField(max_length=1000)
    options = serializers.ListField(child=serializers.CharField(max_length=500), min_length=2, max_length=6)
    answer = serializers.IntegerField(min_value=0)
    explanation = serializers.CharField(max_length=2000, allow_blank=True, required=False, default="")

    def validate(self, attrs):
        options = [option.strip() for option in attrs["options"]]
        # The course writer marks the right option by its text, so two options can't be the same.
        if len(set(options)) != len(options):
            raise serializers.ValidationError(_("Each option of a question must be different."))
        if attrs["answer"] >= len(options):
            raise serializers.ValidationError(_("The right answer must be one of the options."))
        return {**attrs, "options": options}


class LessonSerializer(serializers.Serializer):
    """
    One lesson (a subsection in the course). ``first_line``/``last_line`` are line numbers in the
    service's 3-transcript/transcript.json (the same numbers as transcript_edits); start/end are seconds.
    """

    id = serializers.CharField(max_length=32)
    title = serializers.CharField(max_length=255)
    include = serializers.BooleanField(default=True)
    first_line = serializers.IntegerField(min_value=0)
    last_line = serializers.IntegerField(min_value=0)
    start = serializers.FloatField(min_value=0)
    end = serializers.FloatField(min_value=0)
    minutes = serializers.FloatField(min_value=0)
    summary = serializers.CharField(max_length=5000, allow_blank=True)
    key_points = serializers.ListField(child=serializers.CharField(max_length=1000), max_length=10)
    mcq = MultipleChoiceSerializer(many=True, max_length=10)
    reflection = serializers.ListField(child=serializers.CharField(max_length=1000), max_length=10)

    def validate(self, attrs):
        if attrs["first_line"] > attrs["last_line"] or attrs["start"] >= attrs["end"]:
            raise serializers.ValidationError(_("A lesson must end after it starts."))
        return attrs


class SectionSerializer(serializers.Serializer):
    id = serializers.CharField(max_length=32)
    title = serializers.CharField(max_length=255)
    lessons = LessonSerializer(many=True, min_length=1, max_length=50)


class CourseDraftSerializer(serializers.Serializer):
    """The draft course: sections of lessons, in the order of the teaching."""

    version = serializers.IntegerField(min_value=1, max_value=1)
    course_title = serializers.CharField(max_length=255, allow_blank=True)
    sections = SectionSerializer(many=True, min_length=1, max_length=30)

    def validate(self, attrs):
        ids = [section["id"] for section in attrs["sections"]]
        lessons = [lesson for section in attrs["sections"] for lesson in section["lessons"]]
        ids += [lesson["id"] for lesson in lessons]
        if len(set(ids)) != len(ids):
            raise serializers.ValidationError(_("Each section and lesson needs its own id."))
        # Lessons follow the teaching: each starts after the one before it ends.
        for before, after in zip(lessons, lessons[1:]):
            if after["first_line"] <= before["last_line"]:
                raise serializers.ValidationError(_("The lessons must follow the order of the teaching."))
        return attrs


def draft_structure(draft):
    """What the author may not change: the sections and lessons, their ids and where each lesson is."""
    return [
        (
            section["id"],
            [
                (lesson["id"], lesson["first_line"], lesson["last_line"], lesson["start"], lesson["end"])
                for lesson in section["lessons"]
            ],
        )
        for section in draft.get("sections", [])
    ]


def included_lessons(draft):
    return [lesson for section in draft.get("sections", []) for lesson in section["lessons"] if lesson["include"]]


class BuildCourseSerializer(serializers.Serializer):
    approved = serializers.BooleanField()

    def validate_approved(self, value):
        if not value:
            raise serializers.ValidationError(_("Confirm the teacher or their office has reviewed the outline."))
        return value


class LessonAudioSerializer(serializers.Serializer):
    """The clips the service cut for one lesson: track -> path inside the job folder."""

    tracks = serializers.DictField(child=serializers.CharField(max_length=512, validators=[validate_result_path]))

    def validate_tracks(self, value):
        unknown = set(value) - {"bo", "translator"}
        if unknown:
            raise serializers.ValidationError(_("Unknown audio track: {tracks}.").format(tracks=", ".join(unknown)))
        return value


class CourseAudioSerializer(serializers.Serializer):
    """The service's call to write the course: each included lesson's clips, by lesson id."""

    lessons = serializers.DictField(child=LessonAudioSerializer())


class ServiceUpdateSerializer(serializers.Serializer):
    """A progress report from the studio tool service. Every field is optional."""

    status = serializers.ChoiceField(choices=[status.value for status in SERVICE_STATUSES], required=False)
    progress = serializers.IntegerField(min_value=0, max_value=100, required=False)
    error = serializers.CharField(allow_blank=True, required=False)
    results = serializers.DictField(
        child=serializers.CharField(max_length=512, validators=[validate_result_path]), required=False
    )
    duration_seconds = serializers.FloatField(min_value=0, required=False)
    transcribed_minutes = serializers.FloatField(min_value=0, required=False)
    speaker_groups = SpeakerGroupSerializer(many=True, required=False, max_length=10)
    course_draft = CourseDraftSerializer(required=False)

    def validate(self, attrs):
        if "course_draft" in attrs and attrs.get("status") != "course_draft_ready":
            raise serializers.ValidationError(_("A course draft comes with the status course_draft_ready."))
        return attrs

    def validate_speaker_groups(self, value):
        groups = [group["group"] for group in value]
        if len(set(groups)) != len(groups):
            raise serializers.ValidationError(_("Each speaker group needs its own name."))
        return value


SPEAKER_LABELS = ("teacher", "translator", "chanting", "other")


class SpeakerLabelsSerializer(serializers.Serializer):
    """The author's tag for every speaker group, e.g. {"1": "teacher", "2": "translator"}."""

    labels = serializers.DictField(child=serializers.ChoiceField(choices=SPEAKER_LABELS))

    def validate_labels(self, value):
        groups = {group["group"] for group in self.context["speaker_groups"]}
        if set(value) != groups:
            raise serializers.ValidationError(_("Tag every voice, and only the voices that were found."))
        # Several groups may be the teacher: the same voice is sometimes split in two.
        if "teacher" not in value.values():
            raise serializers.ValidationError(_("Tag at least one voice as the teacher."))
        return value
