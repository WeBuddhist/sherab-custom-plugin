"""
Tests for phase 2: the draft course (suggest, shape, build) and writing it into Studio. Run under CMS
test settings.
"""

import io
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest import mock

import redis
from botocore.exceptions import ClientError
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from opaque_keys.edx.keys import UsageKey
from rest_framework.test import APIClient

from ai_course_creator.services import course_builder, llm_client
from common.djangoapps.student.tests.factories import UserFactory
from course_from_recording import course
from course_from_recording.models import RecordingJob
from course_from_recording.tests import views_outside_transactions
from xmodule.modulestore.django import modulestore
from xmodule.modulestore.tests.django_utils import ModuleStoreTestCase
from xmodule.modulestore.tests.factories import CourseFactory

PUBLIC_URL = "https://public-assets.s3.ap-south-1.amazonaws.com"
CONFIG = {
    "REDIS_URL": "redis://redis:6379/3",
    "QUEUE": "studio_tool_service:jobs",
    "BUCKET": "private-bucket",
    "PREFIX": "studio-tools",
    "TOKEN": "secret-token",
    "PUBLIC_BUCKET": "public-assets",
    "PUBLIC_URL": PUBLIC_URL,
}

# What the service wrote in 3-transcript/transcript.json: the teacher's lines and everyone else's turns.
TRANSCRIPT = [
    {"start": 0.0, "end": 10.0, "speaker": "teacher", "text": "ཀ་ཁ།", "warnings": []},
    {"start": 10.0, "end": 20.0, "speaker": "translator", "text": None, "warnings": []},
    {"start": 20.0, "end": 30.0, "speaker": "teacher", "text": "ག་ང།", "warnings": []},
    {"start": 30.0, "end": 40.0, "speaker": "teacher", "text": "ཅ་ཆ།", "warnings": []},
    {"start": 40.0, "end": 55.0, "speaker": "teacher", "text": "ཇ་ཉ།", "warnings": []},
    {"start": 55.0, "end": 70.0, "speaker": "translator", "text": None, "warnings": []},
    {"start": 70.0, "end": 80.0, "speaker": "teacher", "text": "ཏ་ཐ།", "warnings": []},
    {"start": 80.0, "end": 90.0, "speaker": "no speech", "text": None, "warnings": []},
]


def lesson(lesson_id, title, first, last, start, end, include=True):
    return {
        "id": lesson_id,
        "title": title,
        "include": include,
        "first_line": first,
        "last_line": last,
        "start": start,
        "end": end,
        "minutes": round((end - start) / 60, 1),
        "summary": "བསྡུས་དོན་ཞིག",
        "key_points": ["གནད་དང་པོ།", "གནད་གཉིས་པ།", "གནད་གསུམ་པ།"],
        "mcq": [
            {"question": "སྐྱབས་གནས་གང་ཡིན།", "options": ["སངས་རྒྱས།", "དཀོན་མཆོག་གསུམ།", "བླ་མ།"], "answer": 1,
             "explanation": "དཀོན་མཆོག་གསུམ་ལ་སྐྱབས་སུ་འགྲོ།"},
            {"question": "དྲི་བ་གཉིས་པ།", "options": ["ཡིན།", "མིན།"], "answer": 0, "explanation": ""},
        ],
        "reflection": ["བསམ་ཞིབ་དང་པོ།", "བསམ་ཞིབ་གཉིས་པ།", "བསམ་ཞིབ་གསུམ་པ།"],
    }


def a_draft():
    return {
        "version": 1,
        "course_title": "བྱང་ཆུབ་སེམས་དཔའི་སྤྱོད་པ།",
        "sections": [
            {"id": "s1", "title": "ལེའུ་དང་པོ།", "lessons": [
                lesson("s1-l1", "སྐྱབས་འགྲོ།", 0, 3, 0.0, 40.0),
                lesson("s1-l2", "སེམས་བསྐྱེད།", 4, 5, 40.0, 70.0, include=False),
            ]},
            {"id": "s2", "title": "ལེའུ་གཉིས་པ།", "lessons": [lesson("s2-l1", "བསྔོ་བ།", 6, 7, 70.0, 90.0)]},
        ],
    }


def not_found(*_args, **_kwargs):
    raise ClientError({"Error": {"Code": "404"}}, "HeadObject")


@override_settings(STUDIO_TOOL_SERVICE=CONFIG)
class CourseDraftTestCase(ModuleStoreTestCase):
    def setUp(self):
        super().setUp()
        self.course = CourseFactory.create()
        self.s3 = mock.MagicMock()
        self.s3.generate_presigned_url.return_value = "https://s3.example/signed"
        self.s3.get_object.side_effect = lambda **_: {"Body": io.BytesIO(json.dumps(TRANSCRIPT).encode())}
        self.s3.head_object.side_effect = not_found
        self.redis = mock.MagicMock()
        for name, fake in (("s3_client", self.s3), ("redis_client", self.redis)):
            patcher = mock.patch(f"course_from_recording.services.{name}", return_value=fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_job(self, **fields):
        values = {
            "course_id": self.course.id,
            "user": self.user,  # ModuleStoreTestCase's user is global staff
            "status": "course_draft_ready",
            "metadata": {"teacher": "Geshe Tenzin", "multiple_speakers": True, "teacher_language": "bo",
                         "translator_language": "Ladakhi"},
            "original_filename": "teaching.mp3",
            "content_type": "audio/mpeg",
            "file_size": 1000,
            "results": {"transcript_json": "3-transcript/transcript.json"},
            "course_draft": a_draft(),
        }
        values.update(fields)
        return RecordingJob.objects.create(**values)


class AuthorDraftAPITest(CourseDraftTestCase):
    """suggest/, course-draft/ and build/ for the course's authors."""

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        views_outside_transactions(self)

    def call(self, name, job, method="post", body=None):
        url = reverse(f"course_from_recording:{name}", kwargs={"job_id": job.id})
        return getattr(self.client, method)(url, body or {}, format="json")

    def test_suggesting_queues_a_checked_transcript(self):
        job = self.make_job(status="transcript_ready", course_draft={})

        response = self.call("job-suggest", job)

        assert response.status_code == 200
        assert response.data["job"]["status"] == "queued"
        self.redis.lpush.assert_called_once_with("studio_tool_service:jobs", f"course_from_recording:{job.id}")

    def test_suggesting_only_from_a_checked_transcript(self):
        for status in ("transcribing", "course_draft_ready", "course_ready"):
            assert self.call("job-suggest", self.make_job(status=status)).status_code == 409
        self.redis.lpush.assert_not_called()

    def test_the_job_shows_its_draft(self):
        job = self.make_job()
        data = self.client.get(reverse("course_from_recording:job", kwargs={"job_id": job.id})).data["job"]
        assert data["course_draft"]["sections"][0]["title"] == "ལེའུ་དང་པོ།"
        assert (data["course_approved"], data["course_sections"]) == (False, [])

    def test_the_author_renames_switches_off_and_edits(self):
        job = self.make_job()
        draft = a_draft()
        draft["sections"][0]["title"] = "ལེའུ་དང་པོ། སྐྱབས་འགྲོ།"
        draft["sections"][1]["lessons"][0]["include"] = False
        draft["sections"][0]["lessons"][0]["mcq"][0]["answer"] = 2
        draft["sections"][0]["lessons"][0]["reflection"] = ["བསམ་ཞིབ་གཅིག་པུ།"]

        response = self.call("job-course-draft", job, "put", draft)

        assert response.status_code == 200
        job.refresh_from_db()
        assert job.course_draft["sections"][0]["title"] == "ལེའུ་དང་པོ། སྐྱབས་འགྲོ།"
        assert job.course_draft["sections"][1]["lessons"][0]["include"] is False
        assert job.course_draft["sections"][0]["lessons"][0]["mcq"][0]["answer"] == 2
        assert job.course_draft["sections"][0]["lessons"][0]["reflection"] == ["བསམ་ཞིབ་གཅིག་པུ།"]

    def test_lessons_cant_be_moved_added_or_removed(self):
        job = self.make_job()
        moved, added, removed = a_draft(), a_draft(), a_draft()
        moved["sections"][0]["lessons"][0]["last_line"] = 2
        added["sections"][1]["lessons"].append(lesson("s2-l2", "གསར་པ།", 8, 9, 90.0, 99.0))
        removed["sections"].pop()

        for draft in (moved, added, removed):
            assert self.call("job-course-draft", job, "put", draft).status_code == 400
        job.refresh_from_db()
        assert job.course_draft == a_draft()

    def test_a_question_needs_different_options_and_a_real_answer(self):
        job = self.make_job()
        same, outside = a_draft(), a_draft()
        same["sections"][0]["lessons"][0]["mcq"][0]["options"] = ["ཀ", "ཀ", "ཁ"]
        outside["sections"][0]["lessons"][0]["mcq"][0]["answer"] = 3
        for draft in (same, outside):
            assert self.call("job-course-draft", job, "put", draft).status_code == 400

    def test_the_draft_can_only_change_before_the_course_is_made(self):
        job = self.make_job(status="cutting_audio")
        assert self.call("job-course-draft", job, "put", a_draft()).status_code == 409

    def test_building_needs_the_teachers_review(self):
        job = self.make_job()
        response = self.call("job-build", job, body={"approved": False})
        assert response.status_code == 400
        job.refresh_from_db()
        assert (job.status, job.course_approved_at) == ("course_draft_ready", None)
        self.redis.lpush.assert_not_called()

    def test_building_needs_a_lesson(self):
        draft = a_draft()
        for section in draft["sections"]:
            for item in section["lessons"]:
                item["include"] = False
        job = self.make_job(course_draft=draft)
        assert self.call("job-build", job, body={"approved": True}).status_code == 400

    def test_building_queues_the_approved_draft(self):
        job = self.make_job()

        response = self.call("job-build", job, body={"approved": True})

        assert response.status_code == 200
        assert (response.data["job"]["status"], response.data["job"]["course_approved"]) == ("queued", True)
        self.redis.lpush.assert_called_once_with("studio_tool_service:jobs", f"course_from_recording:{job.id}")
        assert self.call("job-build", job, body={"approved": True}).status_code == 409

    def test_the_approval_is_undone_when_the_job_cant_be_queued(self):
        job = self.make_job()
        self.redis.lpush.side_effect = redis.ConnectionError("down")

        assert self.call("job-build", job, body={"approved": True}).status_code == 503

        job.refresh_from_db()
        assert (job.status, job.course_approved_at) == ("course_draft_ready", None)

    def test_starting_over_clears_the_draft(self):
        job = self.make_job(course_approved_at="2026-11-06T10:00:00Z", course_sections=["block-v1:x"])
        self.s3.get_paginator.return_value.paginate.return_value = []

        assert self.call("job-start-over", job, body={"keep_recording": True}).status_code == 200

        job.refresh_from_db()
        assert (job.course_draft, job.course_approved_at) == ({}, None)
        assert job.course_sections == ["block-v1:x"]  # the course already written stays as it is


class ServiceDraftAPITest(CourseDraftTestCase):
    """The service reports the suggested draft (PATCH) and reads the job (GET)."""

    def setUp(self):
        super().setUp()
        self.client = APIClient(HTTP_X_SERVICE_TOKEN="secret-token")

    def url(self, job):
        return reverse("course_from_recording:service-job", kwargs={"job_id": job.id})

    def test_the_service_reports_the_suggested_draft(self):
        job = self.make_job(status="suggesting", course_draft={})

        response = self.client.patch(self.url(job), {"status": "course_draft_ready", "course_draft": a_draft()},
                                     format="json")

        assert response.status_code == 200
        job.refresh_from_db()
        assert (job.status, job.progress) == ("course_draft_ready", 100)
        assert job.course_draft["sections"][1]["lessons"][0]["title"] == "བསྔོ་བ།"

    def test_a_draft_comes_with_its_status_and_the_other_way_round(self):
        job = self.make_job(status="suggesting", course_draft={})
        for body in ({"progress": 50, "course_draft": a_draft()}, {"status": "course_draft_ready"}):
            assert self.client.patch(self.url(job), body, format="json").status_code == 400

    def test_a_broken_draft_is_refused(self):
        job = self.make_job(status="suggesting", course_draft={})
        draft = a_draft()
        draft["sections"][1]["lessons"][0]["first_line"] = 2  # overlaps the lesson before it
        response = self.client.patch(self.url(job), {"status": "course_draft_ready", "course_draft": draft},
                                     format="json")
        assert response.status_code == 400

    def test_the_service_reads_the_draft_and_whether_it_was_approved(self):
        job = self.make_job(status="queued", course_approved_at="2026-11-06T10:00:00Z")
        data = self.client.get(self.url(job)).data
        assert (data["course_approved"], data["course_draft"]) == (True, a_draft())

    def test_cutting_and_writing_are_running_steps(self):
        job = self.make_job(status="queued")
        for step in ("cutting_audio", "writing_course"):
            assert self.client.patch(self.url(job), {"status": step, "progress": 10}, format="json").status_code == 200


class WriteCourseTest(CourseDraftTestCase):
    """service/jobs/<id>/course/: copy the clips to the public bucket and write the course."""

    AUDIO = {
        "s1-l1": {"tracks": {"bo": "5-lessons/lesson-1-1_bo.mp3", "translator": "5-lessons/lesson-1-1_translator.mp3"}},
        "s2-l1": {"tracks": {"bo": "5-lessons/lesson-2-1_bo.mp3"}},
    }

    def setUp(self):
        super().setUp()
        self.client = APIClient(HTTP_X_SERVICE_TOKEN="secret-token")
        self.store = modulestore()

    def write(self, job, audio=None):
        url = reverse("course_from_recording:service-job-course", kwargs={"job_id": job.id})
        return self.client.post(url, {"lessons": audio or self.AUDIO}, format="json")

    def block(self, key):
        return self.store.get_item(UsageKey.from_string(key) if isinstance(key, str) else key)

    def test_writes_each_kept_lesson_as_four_pages(self):
        job = self.make_job(status="writing_course", transcript_edits={"3": "ཅ་ཆ་ཇ།"})

        response = self.write(job)

        assert response.status_code == 200
        assert response.data["status"] == "course_ready"
        job.refresh_from_db()
        assert (job.status, job.progress) == ("course_ready", 100)
        assert job.course_sections == response.data["sections"]
        assert len(job.course_sections) == 2

        first, second = (self.block(key) for key in job.course_sections)
        assert (first.display_name, second.display_name) == ("ལེའུ་དང་པོ།", "ལེའུ་གཉིས་པ།")
        # The lesson switched off is left out.
        assert [sub.display_name for sub in first.get_children()] == ["སྐྱབས་འགྲོ།"]
        units = first.get_children()[0].get_children()
        assert [unit.display_name for unit in units] == [
            course.LABELS["listen"], course.LABELS["key_points"], course.LABELS["check"], course.LABELS["reflect"]
        ]

        listen = units[0].get_children()[0].data
        assert f'src="{PUBLIC_URL}/course-audio/' in listen
        assert "lesson-1-1_bo.mp3" in listen and "lesson-1-1_translator.mp3" in listen
        assert "Ladakhi" in listen  # the translator's player is labelled with the translator's language
        # The teacher's own words, with the author's correction, a paragraph per turn.
        assert "<p>ཀ་ཁ།</p>" in listen and "<p>ག་ང། ཅ་ཆ་ཇ།</p>" in listen

        key_points = units[1].get_children()[0].data
        assert "བསྡུས་དོན་ཞིག" in key_points and "<li>གནད་གཉིས་པ།</li>" in key_points

        questions = units[2].get_children()
        assert len(questions) == 2
        assert '<choice correct="true">དཀོན་མཆོག་གསུམ།</choice>' in questions[0].data
        assert '<choice correct="false">སངས་རྒྱས།</choice>' in questions[0].data
        assert f"<p>{course.LABELS['explanation']}</p>" in questions[0].data
        # Practice: no limit of its own, so the course's setting applies (none here: unlimited).
        assert questions[0].max_attempts is None

        assert "<li>བསམ་ཞིབ་གཉིས་པ།</li>" in units[3].get_children()[0].data

        # Lesson 2-1 has only the Tibetan track.
        listen_2 = second.get_children()[0].get_children()[0].get_children()[0].data
        assert "lesson-2-1_bo.mp3" in listen_2 and "translator" not in listen_2

        # Nothing is published. (Sections and subsections are always "published" containers in Open edX; the
        # pages inside them stay drafts, so learners see none of it until an author publishes.)
        assert not any(self.store.has_published_version(unit) for unit in units)

    def test_the_clips_are_copied_to_the_public_bucket(self):
        job = self.make_job(status="writing_course")

        self.write(job)

        copies = {call.kwargs["Key"]: call.kwargs for call in self.s3.copy_object.call_args_list}
        folder = job.folder.split("/course-from-recording/")[1]
        key = f"course-audio/{folder}/lesson-1-1_bo.mp3"
        assert set(copies) == {key, f"course-audio/{folder}/lesson-1-1_translator.mp3",
                               f"course-audio/{folder}/lesson-2-1_bo.mp3"}
        assert copies[key]["Bucket"] == "public-assets"
        assert copies[key]["CopySource"] == {
            "Bucket": "private-bucket", "Key": f"{job.folder}/5-lessons/lesson-1-1_bo.mp3"
        }
        assert (copies[key]["ContentType"], copies[key]["MetadataDirective"]) == ("audio/mpeg", "REPLACE")

    def test_a_clip_already_in_the_public_bucket_is_not_copied_again(self):
        self.s3.head_object.side_effect = None
        self.write(self.make_job(status="writing_course"))
        self.s3.copy_object.assert_not_called()

    def test_writing_again_answers_with_the_course_already_written(self):
        job = self.make_job(status="writing_course")
        first = self.write(job).data["sections"]

        again = self.write(job)

        assert (again.status_code, again.data["sections"]) == (200, first)
        assert len(self.store.get_course(self.course.id).children) == 2

    def test_only_while_writing_the_course(self):
        response = self.write(self.make_job(status="cutting_audio"))
        assert (response.status_code, response.data["status"]) == (409, "cutting_audio")

    def test_fails_when_the_author_can_no_longer_edit_the_course(self):
        job = self.make_job(status="writing_course", user=UserFactory())

        response = self.write(job)

        assert (response.status_code, response.data["status"]) == (200, "failed")
        job.refresh_from_db()
        assert job.error == "The person who started this can no longer edit this course."
        assert self.store.get_course(self.course.id).children == []

    @override_settings(STUDIO_TOOL_SERVICE={**CONFIG, "PUBLIC_BUCKET": ""})
    def test_fails_without_the_public_bucket(self):
        job = self.make_job(status="writing_course")
        assert self.write(job).data["status"] == "failed"
        job.refresh_from_db()
        assert job.error == "The public audio bucket is not set up on this server."

    def test_fails_when_a_lessons_audio_is_missing(self):
        job = self.make_job(status="writing_course")
        assert self.write(job, {"s1-l1": self.AUDIO["s1-l1"]}).data["status"] == "failed"
        assert self.store.get_course(self.course.id).children == []

    def test_fails_when_a_lesson_has_no_tibetan_track(self):
        job = self.make_job(status="writing_course")
        audio = {**self.AUDIO, "s2-l1": {"tracks": {"translator": "5-lessons/lesson-2-1_translator.mp3"}}}

        assert self.write(job, audio).data["status"] == "failed"

        job.refresh_from_db()
        assert job.error == "The audio of some lessons is missing: s2-l1."
        assert self.store.get_course(self.course.id).children == []

    def test_a_course_still_being_written_is_not_written_again(self):
        job = self.make_job(status="writing_course")
        lock = f"course_from_recording:writing_course:{job.id}"
        cache.add(lock, True)
        self.addCleanup(cache.delete, lock)

        response = self.write(job)

        assert response.status_code == 503  # the service asks again later
        assert self.store.get_course(self.course.id).children == []
        job.refresh_from_db()
        assert job.status == "writing_course"

    def test_a_course_written_but_not_marked_ready_is_only_marked(self):
        job = self.make_job(status="writing_course", course_sections=["block-v1:x+y+z+type@chapter+block@a"])

        response = self.write(job)

        assert (response.data["status"], response.data["sections"]) == ("course_ready", job.course_sections)
        assert self.store.get_course(self.course.id).children == []  # nothing written a second time
        job.refresh_from_db()
        assert job.status == "course_ready"

    def test_a_start_over_while_writing_stays_and_keeps_the_sections(self):
        job = self.make_job(status="writing_course")
        real = course.write_course

        def start_over_meanwhile(*args):
            sections = real(*args)
            RecordingJob.objects.filter(id=job.id).update(status="cancelled")
            return sections

        with mock.patch("course_from_recording.views_service.write_course", side_effect=start_over_meanwhile):
            response = self.write(job)

        assert response.data["status"] == "cancelled"
        job.refresh_from_db()
        assert job.status == "cancelled"
        assert len(job.course_sections) == 2  # the sections written stay in the course, and are on record

    def test_a_failed_page_removes_everything_written(self):
        job = self.make_job(status="writing_course")
        real = course_builder._create_component  # pylint: disable=protected-access

        def no_questions(store, user, parent, component):
            if component.get("type") == "problem":
                raise ValueError("broken")
            return real(store, user, parent, component)

        with mock.patch.object(course_builder, "_create_component", side_effect=no_questions):
            response = self.write(job)

        assert response.data["status"] == "failed"
        assert self.store.get_course(self.course.id).children == []

    def test_an_unexpected_error_fails_the_job_instead_of_answering_500(self):
        job = self.make_job(status="writing_course")
        with mock.patch("course_from_recording.views_service.write_course", side_effect=RuntimeError("boom")):
            response = self.write(job)
        assert (response.status_code, response.data["status"]) == (200, "failed")


class TeacherParagraphsTest(SimpleTestCase):
    def test_one_paragraph_per_turn_with_the_corrections(self):
        assert course.teacher_paragraphs(TRANSCRIPT, {"3": "ཅ་ཆ་ཇ།"}, 0, 7) == [
            "ཀ་ཁ།", "ག་ང། ཅ་ཆ་ཇ། ཇ་ཉ།", "ཏ་ཐ།"
        ]

    def test_a_long_turn_is_split(self):
        lines = [{"speaker": "teacher", "text": str(number)} for number in range(14)]
        paragraphs = course.teacher_paragraphs(lines, {}, 0, 13)
        assert [len(paragraph.split()) for paragraph in paragraphs] == [6, 6, 2]


class BuilderOptionsTest(SimpleTestCase):
    """The two options added to the AI course creator's builder, off by default."""

    def test_the_explanation_heading_is_english_unless_given(self):
        question = {"problemType": "multiplechoice", "question": "Q", "choices": ["A", "B"], "correct": ["A"],
                    "explanation": "Because."}
        assert "<p>Explanation</p>" in course_builder.build_problem_olx(question)
        assert "<p>འགྲེལ་བཤད།</p>" in course_builder.build_problem_olx({**question, "explanationLabel": "འགྲེལ་བཤད།"})

    def test_an_unknown_question_type_keeps_the_heading(self):
        question = {"problemType": "multiple-choice", "question": "Q", "choices": ["A", "B"], "correct": ["A"],
                    "explanation": "Because.", "explanationLabel": "འགྲེལ་བཤད།"}
        assert "<p>འགྲེལ་བཤད།</p>" in course_builder.build_problem_olx(question)

    def test_one_try_unless_given(self):
        create = course_builder._create_component  # pylint: disable=protected-access
        question = {"type": "problem", "question": "Q", "choices": ["A"], "correct": ["A"]}

        def tries(**fields):
            block = SimpleNamespace()
            with mock.patch(
                "cms.djangoapps.contentstore.xblock_storage_handlers.create_xblock.create_xblock", return_value=block
            ):
                create(mock.MagicMock(), mock.MagicMock(), "parent", {**question, **fields})
            return getattr(block, "max_attempts", "unset")

        assert tries() == 1
        assert tries(maxAttempts=3) == 3
        assert tries(maxAttempts=None) == "unset"  # the course's Maximum Attempts setting applies
        # A model's odd values count as one try.
        assert [tries(maxAttempts=value) for value in ("3", 0, -2, True, 2.5)] == [1] * 5

    def test_strict_fails_the_whole_build_on_one_bad_page(self):
        create_xblock = "cms.djangoapps.contentstore.xblock_storage_handlers.create_xblock.create_xblock"
        course_json = {"sections": [{"title": "S", "subsections": [{"title": "L", "units": [
            {"title": "U", "components": [{"type": "html", "body": "x"}]}
        ]}]}]}
        store = mock.MagicMock()
        section = mock.MagicMock(location="block-v1:A+B+C+type@chapter+block@s")
        with mock.patch("xmodule.modulestore.django.modulestore", return_value=store), \
                mock.patch.object(course_builder, "user_can_author", return_value=True), \
                mock.patch(create_xblock, return_value=section), \
                mock.patch.object(course_builder, "_create_component", side_effect=ValueError("broken")):
            # Skipped by default...
            result = course_builder.apply_course_json("course-v1:A+B+C", mock.MagicMock(), course_json)
            assert result["counts"]["components"] == 0
            store.delete_item.assert_not_called()
            # ...but strict removes what was made and fails.
            with self.assertRaises(course_builder.CourseBuildError):
                course_builder.apply_course_json("course-v1:A+B+C", mock.MagicMock(), course_json, strict=True)
        store.delete_item.assert_called_once()


class OpenRouterClientTest(SimpleTestCase):
    """The AI course creator's OpenRouter client."""

    def test_retry_after_in_seconds_or_as_a_date(self):
        retry_after = llm_client._retry_after_header  # pylint: disable=protected-access
        assert (retry_after("7"), retry_after(None), retry_after("soon")) == (7.0, None, None)
        assert retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0  # a date in the past: no wait
        assert 50 < retry_after("Wed, 21 Oct 2099 07:28:00 GMT")  # a date ahead: the time until then

    def test_a_dropped_stream_is_tried_again(self):
        import requests  # pylint: disable=import-outside-toplevel
        error = requests.exceptions.ChunkedEncodingError("cut")
        assert llm_client._is_server_unavailable(error)  # pylint: disable=protected-access

    def test_the_stream_is_read_as_utf8(self):
        import requests  # pylint: disable=import-outside-toplevel
        resp = requests.models.Response()
        resp.status_code = 200
        resp.headers["Content-Type"] = "text/event-stream"  # no charset: requests would assume Latin-1
        chunk = {"choices": [{"delta": {"content": "བཀྲ་ཤིས་བདེ་ལེགས།"}}]}
        resp.raw = io.BytesIO(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\ndata: [DONE]\n".encode())
        assert list(llm_client._stream_chunks(resp)) == ["བཀྲ་ཤིས་བདེ་ལེགས།"]  # pylint: disable=protected-access


def test_draft_fixture_is_valid():
    """The fixture itself passes the draft's own checks, so the tests above test what they claim."""
    from course_from_recording.serializers import CourseDraftSerializer

    serializer = CourseDraftSerializer(data=deepcopy(a_draft()))
    assert serializer.is_valid(), serializer.errors
