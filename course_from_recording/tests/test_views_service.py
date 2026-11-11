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
            metadata={"teacher": "Geshe Tenzin", "multiple_speakers": True},
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
        assert response.data["details"] == {"teacher": "Geshe Tenzin", "multiple_speakers": True}
        folder = (
            "studio-tools/course-from-recording/Sherab-tspc12-2026/"
            f"{self.job.created:%Y-%m-%d}_teaching_{self.job.id.hex[:8]}"
        )
        assert response.data["folder"] == folder
        assert response.data["original_key"] == f"{folder}/0-original/teaching.wav"
        assert response.data["transcribed_minutes"] == 0

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

    def test_a_job_the_author_started_over_tells_the_service_to_stop(self):
        self.job.status = "cancelled"
        self.job.save()

        response = self.patch({"progress": 10})

        assert response.status_code == 409
        assert response.data["status"] == "cancelled"

    def test_minutes_already_paid_for_are_still_recorded_after_starting_over(self):
        self.job.status = "cancelled"
        self.job.save()

        assert self.patch({"transcribed_minutes": 7.5}).status_code == 200
        assert self.patch({"transcribed_minutes": 8, "progress": 50}).status_code == 409
        self.job.refresh_from_db()
        assert (self.job.transcribed_minutes, self.job.status) == (7.5, "cancelled")

    # Tagging the voices

    def groups(self):
        return [
            {"group": "1", "minutes": 95.5, "samples": ["1-voices/samples/voice-1_sample-1.mp3", "1-voices/samples/voice-1_sample-2.mp3"]},
            {"group": "2", "minutes": 80.0, "samples": ["1-voices/samples/voice-2_sample-1.mp3"]},
        ]

    def test_pausing_for_the_author_to_tag_the_voices(self):
        response = self.patch({"status": "needs_speaker_labels", "speaker_groups": self.groups()})

        assert response.status_code == 200
        self.job.refresh_from_db()
        assert self.job.status == "needs_speaker_labels"
        assert self.job.speaker_groups == self.groups()
        # The job has left the queue: the service can't report on it until the author has tagged the voices.
        assert self.patch({"progress": 10}).status_code == 409

    def test_pausing_needs_the_groups(self):
        assert self.patch({"status": "needs_speaker_labels"}).status_code == 400

    def test_one_speaker_recordings_never_pause(self):
        self.job.metadata = {"teacher": "Geshe Tenzin", "multiple_speakers": False}
        self.job.save()
        assert self.patch({"status": "needs_speaker_labels", "speaker_groups": self.groups()}).status_code == 400

    def test_samples_must_stay_inside_the_job_folder(self):
        groups = [{"group": "1", "minutes": 1, "samples": ["../other-job/clip.mp3"]}]
        assert self.patch({"status": "needs_speaker_labels", "speaker_groups": groups}).status_code == 400

    def test_group_names_must_be_unique(self):
        groups = [
            {"group": "1", "minutes": 1, "samples": ["a.mp3"]},
            {"group": "1", "minutes": 2, "samples": ["b.mp3"]},
        ]
        assert self.patch({"status": "needs_speaker_labels", "speaker_groups": groups}).status_code == 400

    def test_get_includes_the_authors_tags(self):
        self.job.speaker_labels = {"1": "teacher", "2": "translator"}
        self.job.save()
        assert self.client.get(self.url()).data["speaker_labels"] == {"1": "teacher", "2": "translator"}
