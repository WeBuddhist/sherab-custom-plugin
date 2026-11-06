"""
URL routes for the "From a teaching recording" tool (CMS).

Mounted at the site root (REGEX "^") via the plugin config in apps.py, so the
absolute paths are e.g. ``/api/course-from-recording/v1/jobs/`` on the Studio host.
"""

from django.urls import path

from .views import (
    JobDetailView,
    JobRetryView,
    JobReuseRecordingView,
    JobSpeakersView,
    JobStartOverView,
    JobStartView,
    JobsView,
    JobTranscriptEditView,
)
from .views_service import ServiceJobView

app_name = "course_from_recording"

urlpatterns = [
    path("api/course-from-recording/v1/jobs/", JobsView.as_view(), name="jobs"),
    path("api/course-from-recording/v1/jobs/<uuid:job_id>/", JobDetailView.as_view(), name="job"),
    path("api/course-from-recording/v1/jobs/<uuid:job_id>/start/", JobStartView.as_view(), name="job-start"),
    path("api/course-from-recording/v1/jobs/<uuid:job_id>/speakers/", JobSpeakersView.as_view(), name="job-speakers"),
    path("api/course-from-recording/v1/jobs/<uuid:job_id>/retry/", JobRetryView.as_view(), name="job-retry"),
    path(
        "api/course-from-recording/v1/jobs/<uuid:job_id>/transcript-edits/",
        JobTranscriptEditView.as_view(),
        name="job-transcript-edits",
    ),
    path(
        "api/course-from-recording/v1/jobs/<uuid:job_id>/start-over/", JobStartOverView.as_view(), name="job-start-over"
    ),
    path(
        "api/course-from-recording/v1/jobs/<uuid:job_id>/reuse-recording/",
        JobReuseRecordingView.as_view(),
        name="job-reuse-recording",
    ),
    path("api/course-from-recording/v1/service/jobs/<uuid:job_id>/", ServiceJobView.as_view(), name="service-job"),
]
