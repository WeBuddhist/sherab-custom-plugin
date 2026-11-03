"""
Studio API for the "From a teaching recording" tool, used by the studio-extension MFE.

    POST api/course-from-recording/v1/jobs/                 -> create a job, returns an S3 upload link
    GET  api/course-from-recording/v1/jobs/?course_id=...   -> the course's latest job, or null
    GET  api/course-from-recording/v1/jobs/<id>/            -> one job, with links to its outputs
    POST api/course-from-recording/v1/jobs/<id>/start/      -> check the upload is in S3, then queue it
    POST api/course-from-recording/v1/jobs/<id>/speakers/   -> tag the voices the service found, then queue it
    POST api/course-from-recording/v1/jobs/<id>/retry/      -> queue a failed job again

Course authors only. The studio tool service's own endpoint is in views_service.py.
"""

import logging

import redis
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
from .serializers import JobCreateSerializer, SpeakerLabelsSerializer

log = logging.getLogger(__name__)


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
        "created": job.created.isoformat(),
        "modified": job.modified.isoformat(),
    }


class AuthorAPIView(APIView):
    authentication_classes = (JwtAuthentication, SessionAuthentication)
    permission_classes = (IsAuthenticated,)

    def get_job(self, request, job_id):
        """Return ``(job, None)``, or ``(None, Response)`` if it doesn't exist or isn't the user's course."""
        job = RecordingJob.objects.filter(id=job_id).first()
        if job is None:
            return None, Response({"error": _("Job not found.")}, status=status.HTTP_404_NOT_FOUND)
        if not has_course_author_access(request.user, job.course_id):
            return None, forbidden_response()
        try:
            services.tool_settings()
        except services.NotConfigured:
            return None, not_configured_response()
        return job, None

    def queue(self, job, from_status):
        """Move the job to "queued" and onto the service's queue; undo the move if Redis is down."""
        job.status = RecordingJob.Status.QUEUED
        job.progress = 0
        job.error = ""
        job.save(update_fields=["status", "progress", "error", "modified"])
        try:
            services.enqueue(job)
        except redis.RedisError:
            log.exception("Could not queue recording job %s", job.id)
            job.status = from_status
            job.save(update_fields=["status", "modified"])
            return Response(
                {"error": _("The recording could not be queued. Try again in a few minutes.")},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({"job": serialize_job(job)})


class JobsView(AuthorAPIView):
    """Create a job, or read a course's latest one."""

    def get(self, request):
        course_key, error = get_course_key_or_error(request.query_params.get("course_id"))
        if error:
            return error
        if not has_course_author_access(request.user, course_key):
            return forbidden_response()
        try:
            services.tool_settings()
        except services.NotConfigured:
            return not_configured_response()
        job = RecordingJob.objects.filter(course_id=course_key).first()
        return Response({"job": serialize_job(job) if job else None})

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
        try:
            services.tool_settings()
        except services.NotConfigured:
            return not_configured_response()

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
