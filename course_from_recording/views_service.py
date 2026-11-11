"""
API for the studio tool service (the background container), not for browsers.

    GET   api/course-from-recording/v1/service/jobs/<id>/  -> what the service needs to run the job
    PATCH api/course-from-recording/v1/service/jobs/<id>/  -> a progress report: status, progress, results...
    POST  api/course-from-recording/v1/service/jobs/<id>/course/ -> the lesson clips are cut: write the course

The service takes only a job id from the Redis queue and reads everything else
here, so an entry nobody created through the Studio API leads nowhere. Every
request must carry the shared secret (STUDIO_TOOL_SERVICE["TOKEN"]) in the
X-Service-Token header; there is no user login.
"""

import hmac
import logging

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .course import CourseWriteError, write_course
from .models import ACTIVE_STATUSES, RecordingJob
from .serializers import CourseAudioSerializer, ServiceUpdateSerializer

log = logging.getLogger(__name__)

TOKEN_HEADER = "X-Service-Token"

# How long one request may hold the course-writing lock. Far longer than writing takes; it only matters if a
# request dies without releasing it.
WRITE_LOCK_SECONDS = 30 * 60

# Statuses where the job waits for the author: the step that reports one is finished.
WAITING_STATUSES = (
    RecordingJob.Status.TRANSCRIPT_READY,
    RecordingJob.Status.COURSE_DRAFT_READY,
)


class HasServiceToken(BasePermission):
    def has_permission(self, request, view):
        expected = (getattr(settings, "STUDIO_TOOL_SERVICE", None) or {}).get("TOKEN", "")
        given = request.headers.get(TOKEN_HEADER, "")
        # compare_digest takes as long for a wrong token as a right one, so it can't be guessed by timing.
        return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())


class ServiceJobView(APIView):
    # No user login: the shared secret is the only way in, and no CSRF check applies.
    authentication_classes = ()
    permission_classes = (HasServiceToken,)

    def get(self, request, job_id):
        job = RecordingJob.objects.filter(id=job_id).first()
        if job is None:
            return Response({"error": _("Job not found.")}, status=status.HTTP_404_NOT_FOUND)
        return Response(
            {
                "id": str(job.id),
                "course_id": str(job.course_id),
                "status": job.status,
                "details": job.metadata,
                "original_filename": job.original_filename,
                "folder": services.job_folder(job),
                "original_key": services.original_key(job),
                "results": job.results,
                "speaker_groups": job.speaker_groups,
                "speaker_labels": job.speaker_labels,
                "transcribed_minutes": job.transcribed_minutes,
                "transcript_edits": job.transcript_edits,
                "course_draft": job.course_draft,
                "course_approved": job.course_approved_at is not None,
            }
        )

    # Locked from reading the job to saving it, so a report can't undo a start-over the author makes at
    # the same moment (it waits, then sees the job is cancelled).
    @transaction.atomic
    def patch(self, request, job_id):
        job = RecordingJob.objects.select_for_update().filter(id=job_id).first()
        if job is None:
            return Response({"error": _("Job not found.")}, status=status.HTTP_404_NOT_FOUND)
        if job.status == RecordingJob.Status.CANCELLED and set(request.data) == {"transcribed_minutes"}:
            # The author started over during transcription: the minutes already sent were paid for, so the
            # service's last count is still kept, for cost tracking.
            serializer = ServiceUpdateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            job.transcribed_minutes = serializer.validated_data["transcribed_minutes"]
            job.save(update_fields=["transcribed_minutes", "modified"])
            return Response({"status": job.status})
        if job.status not in ACTIVE_STATUSES:
            return Response(
                # The service stops on this; "cancelled" tells it the author started over.
                {"error": _("The job is {status}; it is not running.").format(status=job.status), "status": job.status},
                status=status.HTTP_409_CONFLICT,
            )
        serializer = ServiceUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if data.get("status") == RecordingJob.Status.NEEDS_SPEAKER_LABELS:
            if not job.metadata.get("multiple_speakers"):
                return Response(
                    {"error": _("The author said this recording has only one speaker.")},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not data.get("speaker_groups", job.speaker_groups):
                return Response(
                    {"error": _("Send the speaker groups for the author to tag.")}, status=status.HTTP_400_BAD_REQUEST
                )
        if data.get("status") == RecordingJob.Status.COURSE_DRAFT_READY and "course_draft" not in data:
            return Response({"error": _("Send the course draft.")}, status=status.HTTP_400_BAD_REQUEST)

        # Outputs are added step by step, so new ones are merged with the earlier ones.
        if "results" in data:
            job.results = {**job.results, **data.pop("results")}
        for field, value in data.items():
            setattr(job, field, value)
        if job.status in WAITING_STATUSES:
            job.progress = 100
        job.save()
        return Response({"status": job.status})


@method_decorator(transaction.non_atomic_requests, name="dispatch")
class ServiceCourseView(APIView):
    """
    The service has cut the lesson clips (into the job's folder): copy them to the public bucket and write
    the approved draft into the course. Called once the job is at writing_course.

    Writing takes a while (S3 copies, then every page of the course), so it runs outside any database
    transaction and without locking the job's row: the author's Start over and the service's reports aren't
    kept waiting. Instead a short-lived cache lock lets one request write at a time, and the new sections are
    saved as soon as they exist, so the course is never written twice.

    Safe to call again (e.g. the service's request timed out): a course already written is answered as it
    is; one still being written gets 503, so the service asks again later. When writing fails, the job fails
    with a message for the author, and the answer is still 200: the service has nothing to retry, the author
    does (Retry).
    """

    authentication_classes = ()
    permission_classes = (HasServiceToken,)

    def post(self, request, job_id):
        job = RecordingJob.objects.filter(id=job_id).first()
        if job is None:
            return Response({"error": _("Job not found.")}, status=status.HTTP_404_NOT_FOUND)
        serializer = CourseAudioSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        lock = f"course_from_recording:writing_course:{job.id}"
        if not cache.add(lock, True, WRITE_LOCK_SECONDS):
            return Response(
                {"error": _("The course is still being written."), "status": job.status},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        try:
            return self.write(job, serializer.validated_data["lessons"])
        finally:
            cache.delete(lock)

    def write(self, job, lessons):
        job.refresh_from_db()  # read again under the lock: an earlier request may have just finished
        if job.status == RecordingJob.Status.COURSE_READY:
            return Response({"status": job.status, "sections": job.course_sections})
        if job.status != RecordingJob.Status.WRITING_COURSE:
            return Response(
                {"error": _("The job is {status}; it is not writing a course.").format(status=job.status),
                 "status": job.status},
                status=status.HTTP_409_CONFLICT,
            )
        if job.course_sections:
            # An earlier request wrote the course but didn't get as far as marking the job ready.
            return self.finish(job, job.course_sections)

        try:
            sections = write_course(job, lessons)
        except CourseWriteError as error:
            log.warning("Recording job %s: the course could not be written: %s", job.id, error)
            return self.fail(job, str(error))
        except Exception:  # pylint: disable=broad-except
            # Not a 500: the service would take that for Studio being down and keep sending the job again.
            log.exception("Recording job %s: the course could not be written", job.id)
            return self.fail(job, _("Something went wrong while writing the course. Try again, or start over."))

        # Saved at once (no transaction is open here), before anything else can go wrong.
        RecordingJob.objects.filter(id=job.id).update(course_sections=sections, modified=timezone.now())
        log.info("Recording job %s: course written, %s sections", job.id, len(sections))
        return self.finish(job, sections)

    @staticmethod
    def finish(job, sections):
        with transaction.atomic():
            job = RecordingJob.objects.select_for_update().get(id=job.id)
            # Unless the author started over meanwhile: the sections written stay in the course either way.
            if job.status in ACTIVE_STATUSES:
                job.status = RecordingJob.Status.COURSE_READY
                job.progress = 100
                job.error = ""
                job.course_sections = sections
                job.save(update_fields=["course_sections", "status", "progress", "error", "modified"])
        return Response({"status": job.status, "sections": sections})

    @staticmethod
    def fail(job, message):
        with transaction.atomic():
            job = RecordingJob.objects.select_for_update().get(id=job.id)
            if job.status in ACTIVE_STATUSES:
                job.status = RecordingJob.Status.FAILED
                job.error = message
                job.save(update_fields=["status", "error", "modified"])
        return Response({"status": job.status, "error": message})
