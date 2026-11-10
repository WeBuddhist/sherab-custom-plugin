"""
Studio API for the "From a teaching recording" tool, used by the studio-extension MFE.

    POST api/course-from-recording/v1/jobs/                 -> create a job, returns an S3 upload link
    GET  api/course-from-recording/v1/jobs/?course_id=...   -> the course's latest job (or null), and the upload limit
    GET  api/course-from-recording/v1/jobs/<id>/            -> one job, with links to its outputs
    POST api/course-from-recording/v1/jobs/<id>/start/      -> check the upload is in S3, then queue it
    POST api/course-from-recording/v1/jobs/<id>/speakers/   -> tag the voices the service found, then queue it
    POST api/course-from-recording/v1/jobs/<id>/retry/      -> queue a failed job again
    POST api/course-from-recording/v1/jobs/<id>/transcript-edits/ -> save the author's correction of one line
    POST api/course-from-recording/v1/jobs/<id>/start-over/ -> stop the job and delete its files (the recording too,
                                                               unless keep_recording)
    POST api/course-from-recording/v1/jobs/<id>/reuse-recording/ -> a new job with the recording a started-over job
                                                                    kept, queued straight away
    POST api/course-from-recording/v1/jobs/<id>/suggest/      -> queue a transcribed job to suggest a course
    PUT  api/course-from-recording/v1/jobs/<id>/course-draft/ -> save the author's changes to the draft course
    POST api/course-from-recording/v1/jobs/<id>/build/        -> the teacher has reviewed it: cut the audio and
                                                                 write the course

Course authors only. The studio tool service's own endpoints are in views_service.py.
"""

import logging

import redis
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from edx_rest_framework_extensions.auth.jwt.authentication import JwtAuthentication
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from common.djangoapps.student.auth import has_course_author_access
from course_partnerships.helpers import get_course_key_or_error
from xmodule.modulestore.django import modulestore

from . import services
from .models import RecordingJob
from .serializers import (
    BuildCourseSerializer,
    CourseDraftSerializer,
    JobCreateSerializer,
    ReuseRecordingSerializer,
    SpeakerLabelsSerializer,
    StartOverSerializer,
    TranscriptEditSerializer,
    draft_structure,
    included_lessons,
)

log = logging.getLogger(__name__)

# More corrections than a 4-hour recording has lines; only there to keep the row a sensible size.
MAX_TRANSCRIPT_EDITS = 5000


def not_configured_response():
    return Response(
        {"error": _("The recording tool is not set up on this server.")}, status=status.HTTP_503_SERVICE_UNAVAILABLE
    )


def forbidden_response():
    return Response({"error": _("You do not have permission to edit this course.")}, status=status.HTTP_403_FORBIDDEN)


def serialize_job(job):
    return {
        "id": str(job.id),
        "course_id": str(job.course_id),
        "status": job.status,
        "status_label": job.get_status_display(),
        "progress": job.progress,
        "error": job.error,
        "details": job.metadata,
        "original_filename": job.original_filename,
        "file_size": job.file_size,
        "duration_seconds": job.duration_seconds,
        "downloads": services.download_urls(job),
        "speaker_groups": services.speaker_groups_with_urls(job),
        "speaker_labels": job.speaker_labels,
        "transcribed_minutes": job.transcribed_minutes,
        "recording_kept": job.recording_kept,
        "transcript_edits": job.transcript_edits,
        "course_draft": job.course_draft,
        "course_approved": job.course_approved_at is not None,
        "course_sections": job.course_sections,
        "created": job.created.isoformat(),
        "modified": job.modified.isoformat(),
    }


# Open edX runs each request in one database transaction (ATOMIC_REQUESTS). Not these: a job must be saved
# before it goes onto the queue, or the service could read it before the change is visible and run the wrong
# step. Views that read and write a job together lock it in their own transaction.atomic() block.
@method_decorator(transaction.non_atomic_requests, name="dispatch")
class AuthorAPIView(APIView):
    authentication_classes = (JwtAuthentication, SessionAuthentication)
    permission_classes = (IsAuthenticated,)

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        # Once the user is known: nothing works without the service's settings.
        services.tool_settings()

    def handle_exception(self, exc):
        if isinstance(exc, services.NotConfigured):
            return not_configured_response()
        return super().handle_exception(exc)

    def get_job(self, request, job_id):
        """Return ``(job, None)``, or ``(None, Response)`` if it doesn't exist or isn't the user's course."""
        job = RecordingJob.objects.filter(id=job_id).first()
        if job is None:
            return None, Response({"error": _("Job not found.")}, status=status.HTTP_404_NOT_FOUND)
        if not has_course_author_access(request.user, job.course_id):
            return None, forbidden_response()
        return job, None

    def queue(self, job, from_status, fail_if_redis_is_down=False, also_set=None):
        """
        Move the job from ``from_status`` to "queued" and onto the service's queue. The move is one
        conditional update, so two requests at once (a double click) queue the job only once.
        ``also_set``: other fields that change with the move (e.g. the author's approval).

        If Redis is down the move is undone (``also_set`` too), or with ``fail_if_redis_is_down`` the job is
        marked failed instead, so the author gets the Retry button (for a job that never was "uploading" for
        them).
        """
        also_set = also_set or {}
        before = {field: getattr(job, field) for field in also_set}
        moved = RecordingJob.objects.filter(id=job.id, status=from_status).update(
            status=RecordingJob.Status.QUEUED, progress=0, error="", modified=timezone.now(), **also_set
        )
        if not moved:
            return Response({"error": _("This job has already been started.")}, status=status.HTTP_409_CONFLICT)
        try:
            services.enqueue(job)
        except redis.RedisError:
            log.exception("Could not queue recording job %s", job.id)
            message = _("The recording could not be queued. Try again in a few minutes.")
            undo = {"status": RecordingJob.Status.FAILED, "error": message} if fail_if_redis_is_down else {
                "status": from_status, **before
            }
            RecordingJob.objects.filter(id=job.id, status=RecordingJob.Status.QUEUED).update(
                **undo, modified=timezone.now()
            )
            if not fail_if_redis_is_down:
                return Response({"error": message}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        job.refresh_from_db()
        return Response({"job": serialize_job(job)})


class JobsView(AuthorAPIView):
    """Create a job, or read a course's latest one."""

    def get(self, request):
        course_key, error = get_course_key_or_error(request.query_params.get("course_id"))
        if error:
            return error
        if not has_course_author_access(request.user, course_key):
            return forbidden_response()
        job = RecordingJob.objects.filter(course_id=course_key).first()
        # The upload limit too, so the tool page checks a file against the same limit Studio does.
        return Response(
            {"job": serialize_job(job) if job else None, "max_upload_mb": settings.COURSE_FROM_RECORDING_MAX_UPLOAD_MB}
        )

    def post(self, request):
        serializer = JobCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        course_key, error = get_course_key_or_error(data["course_id"])
        if error:
            return error
        if not has_course_author_access(request.user, course_key):
            return forbidden_response()
        if modulestore().get_course(course_key) is None:
            return Response({"error": _("Course not found.")}, status=status.HTTP_404_NOT_FOUND)

        job = RecordingJob.objects.create(
            course_id=course_key,
            user=request.user,
            metadata=data["details"],
            original_filename=data["filename"],
            content_type=data["content_type"],
            file_size=data["size"],
        )
        return Response(
            {
                "job": serialize_job(job),
                "upload": {
                    "method": "PUT",
                    "url": services.upload_url(job),
                    "headers": {"Content-Type": job.content_type},
                },
            },
            status=status.HTTP_201_CREATED,
        )


class JobDetailView(AuthorAPIView):
    def get(self, request, job_id):
        job, error = self.get_job(request, job_id)
        return error or Response({"job": serialize_job(job)})


class JobStartView(AuthorAPIView):
    """Called by the tool page once the browser has finished uploading."""

    def post(self, request, job_id):
        job, error = self.get_job(request, job_id)
        if error:
            return error
        if job.status != RecordingJob.Status.UPLOADING:
            return Response({"error": _("This job has already started.")}, status=status.HTTP_409_CONFLICT)
        size = services.uploaded_size(job)
        if size is None:
            return Response({"error": _("The upload is not finished yet.")}, status=status.HTTP_409_CONFLICT)
        if size != job.file_size:
            return Response(
                {"error": _("The uploaded file is not the one this job was created for.")},
                status=status.HTTP_409_CONFLICT,
            )
        return self.queue(job, from_status=RecordingJob.Status.UPLOADING)


class JobSpeakersView(AuthorAPIView):
    """The author's tags for the voices the service found; the job then continues with transcription."""

    def post(self, request, job_id):
        job, error = self.get_job(request, job_id)
        if error:
            return error
        if job.status != RecordingJob.Status.NEEDS_SPEAKER_LABELS:
            return Response(
                {"error": _("This job is not waiting for the voices to be tagged.")}, status=status.HTTP_409_CONFLICT
            )
        serializer = SpeakerLabelsSerializer(data=request.data, context={"speaker_groups": job.speaker_groups})
        serializer.is_valid(raise_exception=True)
        job.speaker_labels = serializer.validated_data["labels"]
        job.save(update_fields=["speaker_labels", "modified"])
        return self.queue(job, from_status=RecordingJob.Status.NEEDS_SPEAKER_LABELS)


class JobRetryView(AuthorAPIView):
    """Queue a failed job again; the service continues from the step that failed."""

    def post(self, request, job_id):
        job, error = self.get_job(request, job_id)
        if error:
            return error
        if job.status != RecordingJob.Status.FAILED:
            return Response({"error": _("Only a failed job can be retried.")}, status=status.HTTP_409_CONFLICT)
        return self.queue(job, from_status=RecordingJob.Status.FAILED)


class JobTranscriptEditView(AuthorAPIView):
    """The author's correction of one transcript line, saved as they go; the transcript file stays as it was."""

    def post(self, request, job_id):
        job, error = self.get_job(request, job_id)
        if error:
            return error
        serializer = TranscriptEditSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        line, text = str(serializer.validated_data["line"]), serializer.validated_data["text"]
        # Locked while it is read and written back, so two saves at once (two tabs) both keep their line.
        with transaction.atomic():
            job = RecordingJob.objects.select_for_update().get(id=job.id)
            if job.status != RecordingJob.Status.TRANSCRIPT_READY:
                return Response({"error": _("The transcript is not ready yet.")}, status=status.HTTP_409_CONFLICT)
            edits = dict(job.transcript_edits)
            if text is None:
                edits.pop(line, None)
            else:
                edits[line] = text
            if len(edits) > MAX_TRANSCRIPT_EDITS:
                return Response(
                    {"error": _("This transcript has too many corrections to save more.")},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            job.transcript_edits = edits
            job.save(update_fields=["transcript_edits", "modified"])
        return Response({"transcript_edits": job.transcript_edits})


class JobStartOverView(AuthorAPIView):
    """
    Stop the job and delete its files, so the author can start again: with the same recording
    (kept in S3, to reuse without uploading it again) or with a different one (then it is deleted too).
    """

    def post(self, request, job_id):
        job, error = self.get_job(request, job_id)
        if error:
            return error
        serializer = StartOverSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        keep = serializer.validated_data["keep_recording"]

        # Locked, so a progress report the service is saving at the same moment can't undo the stop
        # (views_service locks the row too).
        with transaction.atomic():
            job = RecordingJob.objects.select_for_update().get(id=job.id)
            if job.status == RecordingJob.Status.UPLOADING:
                # The browser may still be sending the file, which would land after the files were deleted.
                return Response(
                    {"error": _("The recording is still uploading. Start over once it has finished.")},
                    status=status.HTTP_409_CONFLICT,
                )
            # A deleted recording can't come back.
            if keep and job.status == RecordingJob.Status.CANCELLED and not job.recording_kept:
                return Response(
                    {"error": _("The recording is no longer there to keep. Upload it again.")},
                    status=status.HTTP_409_CONFLICT,
                )
            # Stop the job first: the service's next progress report is refused, and it stops and writes nothing
            # more.
            job.status = RecordingJob.Status.CANCELLED
            job.progress = 0
            job.error = ""
            job.results = {}
            job.speaker_groups = []
            job.speaker_labels = {}
            job.transcript_edits = {}
            # A course already written stays in the course outline, with its audio; only the draft goes.
            job.course_draft = {}
            job.course_approved_at = None
            job.recording_kept = keep
            job.save()
        try:
            deleted = services.delete_files(job, keep_recording=keep)
        except (BotoCoreError, ClientError):
            log.exception("Could not delete the files of recording job %s", job.id)
            return Response(
                {
                    "error": _(
                        "The job was stopped, but its files could not all be deleted. Try again in a few minutes."
                    )
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        log.info("Recording job %s started over: %s files deleted, recording kept: %s", job.id, deleted, keep)
        return Response({"job": serialize_job(job)})


class JobReuseRecordingView(AuthorAPIView):
    """A new job for the recording a started-over job kept, with the author's details; queued straight away."""

    def post(self, request, job_id):
        old, error = self.get_job(request, job_id)
        if error:
            return error
        serializer = ReuseRecordingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Claim the recording (locked), so two requests at once can't both start a job with it.
        with transaction.atomic():
            old = RecordingJob.objects.select_for_update().get(id=old.id)
            if old.status != RecordingJob.Status.CANCELLED or not old.recording_kept:
                return Response(
                    {"error": _("This recording is no longer kept. Upload it again.")},
                    status=status.HTTP_409_CONFLICT,
                )
            old.recording_kept = False
            old.save(update_fields=["recording_kept", "modified"])
            job = RecordingJob.objects.create(
                course_id=old.course_id,
                user=request.user,
                metadata=serializer.validated_data["details"],
                original_filename=old.original_filename,
                content_type=old.content_type,
                file_size=old.file_size,
            )
        try:
            services.move_recording(old, job)
        except (BotoCoreError, ClientError):
            log.exception("Could not move the recording of job %s to job %s", old.id, job.id)
            job.delete()
            old.recording_kept = True
            old.save(update_fields=["recording_kept", "modified"])
            return Response(
                {"error": _("The recording could not be reused. Try again in a few minutes.")},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        # The recording is in the new job now, so if Redis is down that job fails (with Retry) rather than
        # going back to waiting for an upload that already happened.
        return self.queue(job, from_status=RecordingJob.Status.UPLOADING, fail_if_redis_is_down=True)


class JobSuggestCourseView(AuthorAPIView):
    """Once the transcript is checked: the service suggests a course from it (outline, lessons, questions)."""

    def post(self, request, job_id):
        job, error = self.get_job(request, job_id)
        if error:
            return error
        if job.status != RecordingJob.Status.TRANSCRIPT_READY:
            return Response({"error": _("The transcript is not ready yet.")}, status=status.HTTP_409_CONFLICT)
        return self.queue(job, from_status=RecordingJob.Status.TRANSCRIPT_READY)


class JobCourseDraftView(AuthorAPIView):
    """
    The author's changes to the suggested course, saved as they go: titles, lessons switched off, summaries,
    key points and questions. Where each lesson starts and ends can't change.
    """

    def put(self, request, job_id):
        job, error = self.get_job(request, job_id)
        if error:
            return error
        serializer = CourseDraftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        draft = serializer.validated_data
        with transaction.atomic():
            job = RecordingJob.objects.select_for_update().get(id=job.id)
            if job.status != RecordingJob.Status.COURSE_DRAFT_READY:
                return Response(
                    {"error": _("The course can only be changed here before it is created.")},
                    status=status.HTTP_409_CONFLICT,
                )
            if draft_structure(draft) != draft_structure(job.course_draft):
                return Response(
                    {"error": _("Lessons can't be added, removed or moved here; only switched off.")},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            job.course_draft = draft
            job.save(update_fields=["course_draft", "modified"])
        return Response({"course_draft": job.course_draft})


class JobBuildCourseView(AuthorAPIView):
    """The author confirms the teacher has reviewed the outline: cut the lesson audio and write the course."""

    def post(self, request, job_id):
        job, error = self.get_job(request, job_id)
        if error:
            return error
        serializer = BuildCourseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if job.status != RecordingJob.Status.COURSE_DRAFT_READY:
            return Response({"error": _("This course has already been created.")}, status=status.HTTP_409_CONFLICT)
        if not included_lessons(job.course_draft):
            return Response({"error": _("Keep at least one lesson.")}, status=status.HTTP_400_BAD_REQUEST)
        # The approval is saved with the move to "queued" and undone with it if the job can't be queued, so a
        # draft the author can still change is never marked as reviewed.
        return self.queue(
            job, from_status=RecordingJob.Status.COURSE_DRAFT_READY, also_set={"course_approved_at": timezone.now()}
        )
