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
            "details": {
                "teacher": "Geshe Tenzin",
                "multiple_speakers": True,
                "teacher_language": "bo",
                "translator_language": "Ladakhi",
                "text": "Bodhicaryavatara",
            },
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
        details = self.create_payload()["details"]
        assert self.create(consent=False).status_code == 400
        assert self.create(details={**details, "teacher": ""}).status_code == 400

    def test_create_needs_the_speakers_and_the_teachers_language(self):
        details = self.create_payload()["details"]
        for missing in ("multiple_speakers", "teacher_language"):
            without = {key: value for key, value in details.items() if key != missing}
            assert self.create(details=without).status_code == 400, missing

    def test_only_tibetan_and_english_teachers_are_supported(self):
        details = self.create_payload()["details"]
        response = self.create(details={**details, "teacher_language": "hi"})

        assert response.status_code == 400
        assert response.data["details"]["teacher_language"] == [
            "Only Tibetan and English recordings are supported for now."
        ]
        assert self.create(details={**details, "teacher_language": "en"}).status_code == 201

    def test_one_speaker_has_no_translator(self):
        details = self.create_payload()["details"]
        self.create(details={**details, "multiple_speakers": False})

        assert RecordingJob.objects.get().metadata == {
            "teacher": "Geshe Tenzin",
            "multiple_speakers": False,
            "teacher_language": "bo",
            "text": "Bodhicaryavatara",
        }

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

    # Tagging the voices

    def waiting_job(self):
        return self.make_job(
            status="needs_speaker_labels",
            metadata={"teacher": "Geshe Tenzin", "multiple_speakers": True, "teacher_language": "bo"},
            speaker_groups=[
                {"group": "1", "minutes": 95.5, "samples": ["speakers/1-a.mp3"]},
                {"group": "2", "minutes": 80.0, "samples": ["speakers/2-a.mp3", "speakers/2-b.mp3"]},
                {"group": "3", "minutes": 4.0, "samples": ["speakers/3-a.mp3"]},
            ],
        )

    def tag(self, job, labels):
        url = reverse("course_from_recording:job-speakers", kwargs={"job_id": job.id})
        return self.client.post(url, {"labels": labels}, format="json")

    def test_job_lists_the_voices_with_links_to_their_samples(self):
        job = self.waiting_job()

        groups = self.client.get(reverse("course_from_recording:job", kwargs={"job_id": job.id})).data["job"][
            "speaker_groups"
        ]

        assert [(group["group"], group["minutes"]) for group in groups] == [("1", 95.5), ("2", 80.0), ("3", 4.0)]
        assert groups[1]["samples"] == ["https://s3.example/signed", "https://s3.example/signed"]
        keys = [call.kwargs["Params"]["Key"] for call in self.s3.generate_presigned_url.call_args_list]
        assert f"studio-tools/course-from-recording/{job.id}/speakers/2-b.mp3" in keys

    def test_tagging_the_voices_queues_the_job_again(self):
        job = self.waiting_job()

        response = self.tag(job, {"1": "teacher", "2": "translator", "3": "chanting"})

        assert response.status_code == 200
        job.refresh_from_db()
        assert job.status == "queued"
        assert job.speaker_labels == {"1": "teacher", "2": "translator", "3": "chanting"}
        self.redis.lpush.assert_called_once_with("studio_tool_service:jobs", f"course_from_recording:{job.id}")

    def test_the_same_voice_split_in_two_can_both_be_the_teacher(self):
        job = self.waiting_job()
        assert self.tag(job, {"1": "teacher", "2": "teacher", "3": "other"}).status_code == 200

    def test_every_voice_needs_a_tag(self):
        job = self.waiting_job()
        assert self.tag(job, {"1": "teacher", "2": "translator"}).status_code == 400
        assert self.tag(job, {"1": "teacher", "2": "translator", "3": "other", "9": "other"}).status_code == 400

    def test_at_least_one_voice_is_the_teacher(self):
        job = self.waiting_job()
        response = self.tag(job, {"1": "translator", "2": "translator", "3": "chanting"})
        assert response.status_code == 400
        assert response.data["labels"] == ["Tag at least one voice as the teacher."]

    def test_only_known_tags(self):
        job = self.waiting_job()
        assert self.tag(job, {"1": "teacher", "2": "audience", "3": "other"}).status_code == 400

    def test_tagging_only_when_the_job_is_waiting_for_it(self):
        job = self.make_job(status="transcribing")
        assert self.tag(job, {"1": "teacher"}).status_code == 409
