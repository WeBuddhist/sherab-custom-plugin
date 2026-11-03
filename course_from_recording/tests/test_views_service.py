"""
Tests for the studio tool service's API (views_service.py). Run under CMS test settings.
"""

import uuid

from django.test import TestCase, override_settings
from django.urls import reverse
from opaque_keys.edx.keys import CourseKey
from rest_framework.test import APIClient

from course_from_recording.models import RecordingJob

CONFIG = {
    "REDIS_URL": "redis://redis:6379/3",
    "QUEUE": "studio_tool_service:jobs",
    "BUCKET": "test-bucket",
    "PREFIX": "studio-tools",
    "TOKEN": "secret-token",
}


@override_settings(STUDIO_TOOL_SERVICE=CONFIG)
class ServiceJobAPITest(TestCase):
    """api/course-from-recording/v1/service/jobs/<id>/"""

    def setUp(self):
        self.job = RecordingJob.objects.create(
            course_id=CourseKey.from_string("course-v1:Sherab+tspc12+2026"),
            status="queued",
            metadata={"teacher": "Geshe Tenzin"},
            original_filename="teaching.wav",
            content_type="audio/wav",
            file_size=1000,
        )
        self.client = APIClient(HTTP_X_SERVICE_TOKEN="secret-token")

    def url(self, job_id=None):
        return reverse("course_from_recording:service-job", kwargs={"job_id": job_id or self.job.id})

    def patch(self, data):
        return self.client.patch(self.url(), data, format="json")

    def test_needs_the_shared_secret(self):
        assert APIClient().get(self.url()).status_code == 403
        assert APIClient(HTTP_X_SERVICE_TOKEN="wrong").get(self.url()).status_code == 403
        assert APIClient(HTTP_X_SERVICE_TOKEN="wrong").patch(self.url(), {"progress": 5}).status_code == 403

    @override_settings(STUDIO_TOOL_SERVICE={**CONFIG, "TOKEN": ""})
    def test_refuses_everyone_when_no_secret_is_set(self):
        assert APIClient(HTTP_X_SERVICE_TOKEN="").get(self.url()).status_code == 403

    def test_get_tells_the_service_where_the_files_are(self):
        response = self.client.get(self.url())

        assert response.status_code == 200
        assert response.data["course_id"] == "course-v1:Sherab+tspc12+2026"
        assert response.data["details"] == {"teacher": "Geshe Tenzin"}
        assert response.data["folder"] == f"studio-tools/course-from-recording/{self.job.id}"
        assert response.data["original_key"] == f"studio-tools/course-from-recording/{self.job.id}/original.wav"

    def test_unknown_job(self):
        assert self.client.get(self.url(uuid.uuid4())).status_code == 404

    def test_progress_reports_update_the_job_and_merge_outputs(self):
        reports = [
            {"status": "cleaning", "results": {"clean": "clean.wav"}},
            {"status": "transcribing", "progress": 60, "duration_seconds": 3600.5},
            {"results": {"transcript": "transcript/raw.txt"}, "transcribed_minutes": 41.2},
        ]
        for report in reports:
            assert self.patch(report).status_code == 200

        self.job.refresh_from_db()
        assert (self.job.status, self.job.progress, self.job.duration_seconds) == ("transcribing", 60, 3600.5)
        assert self.job.transcribed_minutes == 41.2
        assert self.job.results == {"clean": "clean.wav", "transcript": "transcript/raw.txt"}

    def test_transcript_ready_means_done(self):
        self.patch({"status": "transcript_ready"})
        self.job.refresh_from_db()
        assert (self.job.status, self.job.progress) == ("transcript_ready", 100)

    def test_failure_keeps_the_error_for_the_author(self):
        self.patch({"status": "failed", "error": "Monlam timed out"})
        self.job.refresh_from_db()
        assert (self.job.status, self.job.error) == ("failed", "Monlam timed out")

    def test_outputs_must_stay_inside_the_job_folder(self):
        for path in ("../other-job/x.txt", "/etc/passwd", "a/../../b", ""):
            assert self.patch({"results": {"x": path}}).status_code == 400, path

    def test_service_cannot_set_author_statuses(self):
        assert self.patch({"status": "uploading"}).status_code == 400
        assert self.patch({"status": "queued"}).status_code == 400

    def test_finished_or_not_started_jobs_cannot_be_updated(self):
        for job_status in ("uploading", "transcript_ready", "failed"):
            self.job.status = job_status
            self.job.save()
            assert self.patch({"progress": 10}).status_code == 409, job_status
