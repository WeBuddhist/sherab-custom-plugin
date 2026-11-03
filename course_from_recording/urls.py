"""
URL routes for the "From a teaching recording" tool (CMS).

Mounted at the site root (REGEX "^") via the plugin config in apps.py, so the
absolute paths are e.g. ``/api/course-from-recording/v1/jobs/`` on the Studio host.
"""

from django.urls import path

from .views import JobDetailView, JobRetryView, JobStartView, JobsView
from .views_service import ServiceJobView

app_name = "course_from_recording"

urlpatterns = [
    path("api/course-from-recording/v1/jobs/", JobsView.as_view(), name="jobs"),
    path("api/course-from-recording/v1/jobs/<uuid:job_id>/", JobDetailView.as_view(), name="job"),
    path("api/course-from-recording/v1/jobs/<uuid:job_id>/start/", JobStartView.as_view(), name="job-start"),
    path("api/course-from-recording/v1/jobs/<uuid:job_id>/retry/", JobRetryView.as_view(), name="job-retry"),
    path("api/course-from-recording/v1/service/jobs/<uuid:job_id>/", ServiceJobView.as_view(), name="service-job"),
]
