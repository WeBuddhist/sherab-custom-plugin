"""
Models for course_from_recording.

If you make changes to these models, create an appropriate migration file and
check it in at the same time. To do that:

1. Go to the edx-platform dir
2. ./manage.py cms makemigrations course_from_recording
3. ./manage.py cms migrate course_from_recording
"""

import os
import uuid

from django.contrib.auth import get_user_model
from django.db import models
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel
from opaque_keys.edx.django.models import CourseKeyField

User = get_user_model()


class RecordingJob(TimeStampedModel):
    """
    One teaching recording on its way to becoming a course draft.

    The id is random and is also the job's S3 folder name, and the only thing the
    service gets from the queue; it reads everything else back from this table.
    """

    class Status(models.TextChoices):
        UPLOADING = "uploading", _("Waiting for the upload")
        QUEUED = "queued", _("Waiting to start")
        CLEANING = "cleaning", _("Cleaning the audio")
        FINDING_SPEAKERS = "finding_speakers", _("Finding the speakers")
        NEEDS_SPEAKER_LABELS = "needs_speaker_labels", _("Waiting for you to tag the voices")
        TRANSCRIBING = "transcribing", _("Transcribing")
        TRANSCRIPT_READY = "transcript_ready", _("Transcript ready")
        # Phase 2: the transcript becomes a draft course in Studio.
        SUGGESTING = "suggesting", _("Suggesting a course")
        COURSE_DRAFT_READY = "course_draft_ready", _("Waiting for you to shape the course")
        CUTTING_AUDIO = "cutting_audio", _("Cutting the lesson audio")
        WRITING_COURSE = "writing_course", _("Adding the course to Studio")
        COURSE_READY = "course_ready", _("Draft course ready")
        FAILED = "failed", _("Failed")
        # The author started over: the job stopped and its files were deleted (the recording too,
        # unless recording_kept). It stays as a record, e.g. of the minutes already transcribed.
        CANCELLED = "cancelled", _("Started over")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    course_id = CourseKeyField(max_length=255, db_index=True)
    user = models.ForeignKey(
        User, null=True, on_delete=models.SET_NULL, related_name="recording_jobs", help_text=_("Who started it.")
    )
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.UPLOADING)
    progress = models.PositiveSmallIntegerField(default=0, help_text=_("0-100, for the current step."))
    error = models.TextField(blank=True, default="", help_text=_("Shown to the author when a step fails."))
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("What the author entered: teacher, speakers, languages, text, place, year."),
    )
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    file_size = models.PositiveBigIntegerField(help_text=_("Bytes, as declared before the upload."))
    folder = models.CharField(
        max_length=512, blank=True, default="", help_text=_("The job's S3 folder, chosen once when it is created.")
    )
    duration_seconds = models.FloatField(null=True, blank=True)
    transcribed_minutes = models.FloatField(
        default=0, help_text=_("Minutes sent for transcription (Monlam or Gemini), for cost tracking.")
    )
    results = models.JSONField(
        default=dict, blank=True, help_text=_("Output name -> path inside the job's S3 folder, set by the service.")
    )
    # Recordings with more than one speaker: the service splits the voices into groups by itself and
    # pauses the job; the author listens to each group's samples and tags it, and the job continues.
    speaker_groups = models.JSONField(
        default=list, blank=True, help_text=_("Set by the service: [{group, minutes, samples: [path, ...]}, ...].")
    )
    speaker_labels = models.JSONField(
        default=dict, blank=True, help_text=_("Set by the author: group -> teacher, translator, chanting or other.")
    )
    recording_kept = models.BooleanField(
        default=False,
        help_text=_("Started over keeping the recording: it is still in S3, for the next job to reuse."),
    )
    # The service's transcript (3-transcript/transcript.json) is never rewritten; the author's corrections
    # are kept here, by line number, and laid over it wherever the transcript is used.
    transcript_edits = models.JSONField(
        default=dict, blank=True, help_text=_("Set by the author: line number -> corrected text.")
    )
    # Phase 2. The service suggests the draft (its own copy stays in S3, 4-course/suggestion.json); the
    # author shapes this copy, ticks that the teacher has reviewed it, and the course is written from it.
    course_draft = models.JSONField(
        default=dict, blank=True, help_text=_("The course outline and lessons, as the author last saved them.")
    )
    course_approved_at = models.DateTimeField(
        null=True, blank=True, help_text=_("When the author confirmed the teacher has reviewed the outline.")
    )
    course_sections = models.JSONField(
        default=list, blank=True, help_text=_("The sections written into the course (usage keys).")
    )

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return f"{self.course_id} {self.original_filename} ({self.status})"

    def save(self, *args, **kwargs):
        if not self.folder:
            from . import services  # services builds the folder name from the tool's settings

            self.folder = services.new_job_folder(self)
        super().save(*args, **kwargs)

    @property
    def extension(self):
        """'.mp3' for 'Day 1 Morning.MP3'."""
        return os.path.splitext(self.original_filename)[1].lower()


# Steps the audio service reports while it works.
SERVICE_STATUSES = (
    RecordingJob.Status.CLEANING,
    RecordingJob.Status.FINDING_SPEAKERS,
    RecordingJob.Status.NEEDS_SPEAKER_LABELS,
    RecordingJob.Status.TRANSCRIBING,
    RecordingJob.Status.TRANSCRIPT_READY,
    RecordingJob.Status.SUGGESTING,
    RecordingJob.Status.COURSE_DRAFT_READY,
    RecordingJob.Status.CUTTING_AUDIO,
    RecordingJob.Status.WRITING_COURSE,
    RecordingJob.Status.FAILED,
)

# The service may only update a job while it is queued or running. A job waiting for the author (to tag
# the voices, or to shape the course) is neither: it leaves the queue and comes back once the author is done.
ACTIVE_STATUSES = (
    RecordingJob.Status.QUEUED,
    RecordingJob.Status.CLEANING,
    RecordingJob.Status.FINDING_SPEAKERS,
    RecordingJob.Status.TRANSCRIBING,
    RecordingJob.Status.SUGGESTING,
    RecordingJob.Status.CUTTING_AUDIO,
    RecordingJob.Status.WRITING_COURSE,
)
