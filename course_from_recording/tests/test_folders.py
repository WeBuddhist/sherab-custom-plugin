"""
Tests for the S3 folder layout (services.py). Run under CMS test settings.
"""

import uuid
from unittest import mock
from datetime import datetime, timezone

from botocore.exceptions import ClientError
from django.test import SimpleTestCase, override_settings
from opaque_keys.edx.keys import CourseKey

from course_from_recording import services
from course_from_recording.models import RecordingJob

CONFIG = {"REDIS_URL": "redis://redis:6379/3", "QUEUE": "q", "BUCKET": "b", "PREFIX": "studio-tools", "TOKEN": "t"}
JOB_ID = uuid.UUID("a101e248-9420-45f5-aa20-875e33df1f8a")


def job(filename, course="course-v1:Sherab+tspc12+2026", folder=""):
    return RecordingJob(
        id=JOB_ID,
        course_id=CourseKey.from_string(course),
        original_filename=filename,
        folder=folder,
        created=datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc),
    )


@override_settings(STUDIO_TOOL_SERVICE=CONFIG)
class FolderTest(SimpleTestCase):
    def test_course_then_date_file_name_and_short_id(self):
        recording = job("Day 1 - Morning (Teachings).MP3")
        recording.folder = services.new_job_folder(recording)
        assert recording.folder == (
            "studio-tools/course-from-recording/Sherab-tspc12-2026/2026-11-03_day-1-morning-teachings_a101e248"
        )
        assert services.original_key(recording).endswith("_a101e248/0-original/day-1-morning-teachings.mp3")

    def test_a_tibetan_file_name_becomes_recording(self):
        recording = job("ལོ་རིམ་གཉིས་པ།.mp3")
        recording.folder = services.new_job_folder(recording)
        assert recording.folder.endswith("/2026-11-03_recording_a101e248")
        assert services.original_key(recording).endswith("/0-original/recording.mp3")

    def test_accents_are_kept_as_plain_letters(self):
        assert services.new_job_folder(job("Chöjuk ch2.wav")).endswith("/2026-11-03_chojuk-ch2_a101e248")

    def test_long_names_are_shortened(self):
        stem = services.new_job_folder(job("a" * 200 + ".mp3")).rsplit("/", 1)[1]
        assert stem == f"2026-11-03_{'a' * 60}_a101e248"

    def test_a_job_without_a_folder_is_an_error_not_a_guess(self):
        with self.assertRaises(ValueError):
            services.original_key(job("x.mp3"))

    def test_the_saved_folder_wins(self):
        recording = job("x.mp3", folder="studio-tools/course-from-recording/Old/2026-01-01_x_a101e248")
        assert services.job_folder(recording) == "studio-tools/course-from-recording/Old/2026-01-01_x_a101e248"


class MoveRecordingTest(SimpleTestCase):
    """The kept recording ends up in exactly one folder, even when S3 fails half-way."""

    def setUp(self):
        self.s3 = mock.MagicMock()
        patcher = mock.patch("course_from_recording.services.s3_client", return_value=self.s3)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.old = job("x.mp3", folder="studio-tools/course-from-recording/C/old")
        self.new = job("x.mp3", folder="studio-tools/course-from-recording/C/new")

    @override_settings(STUDIO_TOOL_SERVICE=CONFIG)
    def test_when_the_old_copy_cant_be_deleted_the_new_one_is_removed(self):
        self.s3.delete_object.side_effect = [ClientError({"Error": {"Code": "500"}}, "DeleteObject"), None]
        with self.assertRaises(ClientError):
            services.move_recording(self.old, self.new)
        assert [c.kwargs["Key"] for c in self.s3.delete_object.call_args_list] == [
            services.original_key(self.old), services.original_key(self.new)
        ]
