"""
Tests for the Studio API (views.py). Run under CMS test settings.
"""

from unittest import mock

import redis
from botocore.exceptions import ClientError
from django.test import override_settings
from django.urls import reverse
from opaque_keys.edx.keys import CourseKey
from rest_framework.test import APIClient

from common.djangoapps.student.tests.factories import UserFactory
from course_from_recording.models import RecordingJob
from xmodule.modulestore.tests.django_utils import ModuleStoreTestCase
from xmodule.modulestore.tests.factories import CourseFactory

CONFIG = {
    "REDIS_URL": "redis://redis:6379/3",
    "QUEUE": "studio_tool_service:jobs",
    "BUCKET": "test-bucket",
    "PREFIX": "studio-tools",
    "TOKEN": "secret-token",
    "MAX_RECORDING_MINUTES": 240,
}


@override_settings(STUDIO_TOOL_SERVICE=CONFIG, COURSE_FROM_RECORDING_MAX_UPLOAD_MB=100)
class StudioJobsAPITest(ModuleStoreTestCase):
    """api/course-from-recording/v1/jobs/..."""

    def setUp(self):
        super().setUp()
        self.course = CourseFactory.create()
        self.client = APIClient()
        self.client.force_authenticate(self.user)  # ModuleStoreTestCase's user is global staff

        self.s3 = mock.MagicMock()
        self.s3.generate_presigned_url.return_value = "https://s3.example/signed"
        self.redis = mock.MagicMock()
        for name, fake in (("s3_client", self.s3), ("redis_client", self.redis)):
            patcher = mock.patch(f"course_from_recording.services.{name}", return_value=fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def create_payload(self, **changes):
        payload = {
            "course_id": str(self.course.id),
            "filename": "Day 1 Morning.MP3",
            "content_type": "audio/mpeg",
            "size": 50 * 1024 * 1024,
            "consent": True,
            "details": {"teacher": "Geshe Tenzin", "text": "Bodhicaryavatara", "languages": ["bo", "lad"]},
        }
        payload.update(changes)
        return payload

    def create(self, **changes):
        return self.client.post(reverse("course_from_recording:jobs"), self.create_payload(**changes), format="json")

    def make_job(self, **fields):
        values = {
            "course_id": self.course.id,
            "user": self.user,
            "original_filename": "teaching.mp3",
            "content_type": "audio/mpeg",
            "file_size": 1000,
        }
        values.update(fields)
        return RecordingJob.objects.create(**values)

    def post_action(self, name, job):
        return self.client.post(reverse(f"course_from_recording:{name}", kwargs={"job_id": job.id}))

    # Create

    def test_create_returns_a_signed_upload_link_for_the_jobs_folder(self):
        response = self.create()

        assert response.status_code == 201
        job = RecordingJob.objects.get()
        assert response.data["job"]["id"] == str(job.id)
        assert response.data["job"]["status"] == "uploading"
        assert response.data["upload"] == {
            "method": "PUT",
            "url": "https://s3.example/signed",
            "headers": {"Content-Type": "audio/mpeg"},
        }
        self.s3.generate_presigned_url.assert_called_once()
        params = self.s3.generate_presigned_url.call_args.kwargs["Params"]
        assert params == {
            "Bucket": "test-bucket",
            "Key": f"studio-tools/course-from-recording/{job.id}/original.mp3",
            "ContentType": "audio/mpeg",
            "ContentLength": 50 * 1024 * 1024,
        }
        assert job.metadata["teacher"] == "Geshe Tenzin"
        assert job.user == self.user

    def test_create_rejects_files_that_are_not_audio(self):
        assert self.create(filename="slides.pdf").status_code == 400
        assert self.create(content_type="application/pdf").status_code == 400
        assert not RecordingJob.objects.exists()

    def test_create_needs_consent_and_a_teacher(self):
        assert self.create(consent=False).status_code == 400
        assert self.create(details={"text": "No teacher"}).status_code == 400

    def test_create_rejects_files_over_the_size_limit(self):
        response = self.create(size=101 * 1024 * 1024)
        assert response.status_code == 400
        assert "size" in response.data

    def test_create_for_a_missing_course_is_not_found(self):
        assert self.create(course_id="course-v1:Nope+Nope+2026").status_code == 404

    def test_non_author_is_forbidden(self):
        job = self.make_job()
        self.client.force_authenticate(UserFactory())
        assert self.create().status_code == 403
        assert self.client.get(reverse("course_from_recording:job", kwargs={"job_id": job.id})).status_code == 403
        assert self.post_action("job-start", job).status_code == 403

    def test_anonymous_is_rejected(self):
        self.client.force_authenticate(None)
        assert self.create().status_code == 401

    @override_settings(STUDIO_TOOL_SERVICE={})
    def test_tool_not_set_up_on_the_server(self):
        assert self.create().status_code == 503

    # Read

    def test_latest_job_of_the_course(self):
        url = reverse("course_from_recording:jobs")
        assert self.client.get(url, {"course_id": str(self.course.id)}).data == {"job": None}

        self.make_job(original_filename="old.mp3")
        newer = self.make_job(original_filename="new.mp3")
        self.make_job(course_id=CourseKey.from_string("course-v1:Other+Course+2026"))

        response = self.client.get(url, {"course_id": str(self.course.id)})
        assert response.data["job"]["id"] == str(newer.id)

    def test_job_lists_download_links_for_its_outputs(self):
        job = self.make_job(status="transcript_ready", results={"transcript": "transcript/corrected.txt"})

        response = self.client.get(reverse("course_from_recording:job", kwargs={"job_id": job.id}))

        assert response.data["job"]["downloads"] == {"transcript": "https://s3.example/signed"}
        params = self.s3.generate_presigned_url.call_args.kwargs["Params"]
        assert params["Key"] == f"studio-tools/course-from-recording/{job.id}/transcript/corrected.txt"

    # Start and retry

    def test_start_queues_the_job_once_the_upload_is_in_s3(self):
        job = self.make_job()
        self.s3.head_object.return_value = {"ContentLength": 1000}

        response = self.post_action("job-start", job)

        assert response.status_code == 200
        assert response.data["job"]["status"] == "queued"
        self.redis.lpush.assert_called_once_with("studio_tool_service:jobs", f"course_from_recording:{job.id}")

    def test_start_before_the_upload_finished(self):
        job = self.make_job()
        self.s3.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")

        assert self.post_action("job-start", job).status_code == 409
        job.refresh_from_db()
        assert job.status == "uploading"
        self.redis.lpush.assert_not_called()

    def test_start_with_a_different_file_than_declared(self):
        job = self.make_job()
        self.s3.head_object.return_value = {"ContentLength": 999}

        assert self.post_action("job-start", job).status_code == 409
        self.redis.lpush.assert_not_called()

    def test_start_twice(self):
        job = self.make_job(status="queued")
        assert self.post_action("job-start", job).status_code == 409

    def test_start_when_redis_is_down_leaves_the_job_waiting(self):
        job = self.make_job()
        self.s3.head_object.return_value = {"ContentLength": 1000}
        self.redis.lpush.side_effect = redis.ConnectionError("down")

        assert self.post_action("job-start", job).status_code == 503
        job.refresh_from_db()
        assert job.status == "uploading"

    def test_retry_queues_a_failed_job_again(self):
        job = self.make_job(status="failed", error="Monlam timed out", progress=40)

        response = self.post_action("job-retry", job)

        assert response.status_code == 200
        job.refresh_from_db()
        assert (job.status, job.error, job.progress) == ("queued", "", 0)
        self.redis.lpush.assert_called_once()

    def test_only_failed_jobs_can_be_retried(self):
        job = self.make_job(status="transcribing")
        assert self.post_action("job-retry", job).status_code == 409
