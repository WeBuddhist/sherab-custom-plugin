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
        TRANSCRIBING = "transcribing", _("Transcribing")
        TRANSCRIPT_READY = "transcript_ready", _("Transcript ready")
        FAILED = "failed", _("Failed")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    course_id = CourseKeyField(max_length=255, db_index=True)
    user = models.ForeignKey(
        User, null=True, on_delete=models.SET_NULL, related_name="recording_jobs", help_text=_("Who started it.")
    )
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.UPLOADING)
    progress = models.PositiveSmallIntegerField(default=0, help_text=_("0-100, for the current step."))
    error = models.TextField(blank=True, default="", help_text=_("Shown to the author when a step fails."))
    metadata = models.JSONField(default=dict, blank=True, help_text=_("Teacher, text, languages, place, year."))
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    file_size = models.PositiveBigIntegerField(help_text=_("Bytes, as declared before the upload."))
    duration_seconds = models.FloatField(null=True, blank=True)
    transcribed_minutes = models.FloatField(default=0, help_text=_("Minutes sent to Monlam, for cost tracking."))
    results = models.JSONField(
        default=dict, blank=True, help_text=_("Output name -> path inside the job's S3 folder, set by the service.")
    )

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return f"{self.course_id} {self.original_filename} ({self.status})"

    @property
    def extension(self):
        """'.mp3' for 'Day 1 Morning.MP3'."""
        return os.path.splitext(self.original_filename)[1].lower()


# Steps the audio service reports while it works.
SERVICE_STATUSES = (
    RecordingJob.Status.CLEANING,
    RecordingJob.Status.FINDING_SPEAKERS,
    RecordingJob.Status.TRANSCRIBING,
    RecordingJob.Status.TRANSCRIPT_READY,
    RecordingJob.Status.FAILED,
)

# The service may only update a job while it is queued or running.
ACTIVE_STATUSES = (
    RecordingJob.Status.QUEUED,
    RecordingJob.Status.CLEANING,
    RecordingJob.Status.FINDING_SPEAKERS,
    RecordingJob.Status.TRANSCRIBING,
)
