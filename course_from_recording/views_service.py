"""
API for the studio tool service (the background container), not for browsers.

    GET   api/course-from-recording/v1/service/jobs/<id>/  -> what the service needs to run the job
    PATCH api/course-from-recording/v1/service/jobs/<id>/  -> a progress report: status, progress, results...

The service takes only a job id from the Redis queue and reads everything else
here, so an entry nobody created through the Studio API leads nowhere. Every
request must carry the shared secret (STUDIO_TOOL_SERVICE["TOKEN"]) in the
X-Service-Token header; there is no user login.
"""

import hmac

from django.conf import settings
from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import ACTIVE_STATUSES, RecordingJob
from .serializers import ServiceUpdateSerializer

TOKEN_HEADER = "X-Service-Token"


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
            }
        )

    def patch(self, request, job_id):
        job = RecordingJob.objects.filter(id=job_id).first()
        if job is None:
            return Response({"error": _("Job not found.")}, status=status.HTTP_404_NOT_FOUND)
        if job.status not in ACTIVE_STATUSES:
            return Response(
                {"error": _("The job is {status}; it is not running.").format(status=job.status)},
                status=status.HTTP_409_CONFLICT,
            )
        serializer = ServiceUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Outputs are added step by step, so new ones are merged with the earlier ones.
        if "results" in data:
            job.results = {**job.results, **data.pop("results")}
        for field, value in data.items():
            setattr(job, field, value)
        if job.status == RecordingJob.Status.TRANSCRIPT_READY:
            job.progress = 100
        job.save()
        return Response({"status": job.status})
