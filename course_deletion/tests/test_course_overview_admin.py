"""Tests for the Studio Course Overview deletion action."""

from unittest import mock

from django.contrib.admin.models import DELETION, LogEntry
from django.contrib.admin.utils import quote
from django.test import Client
from django.urls import reverse

from common.djangoapps.student.models import CourseEnrollment
from common.djangoapps.student.tests.factories import UserFactory
from openedx.core.djangoapps.content.course_overviews.models import CourseOverview
from xmodule.modulestore.tests.django_utils import ModuleStoreTestCase
from xmodule.modulestore.tests.factories import CourseFactory


class CourseOverviewDeletionAdminTest(ModuleStoreTestCase):
    ENABLED_SIGNALS = ["course_deleted", "course_published"]

    def setUp(self):
        super().setUp()
        self.course = CourseFactory.create(emit_signals=True, display_name="Course to remove")
        self.overview = CourseOverview.objects.get(id=self.course.id)
        self.user.is_staff = True
        self.user.is_superuser = True
        self.user.save(update_fields=["is_staff", "is_superuser"])
        self.client = Client()
        assert self.client.login(username=self.user.username, password=self.user_password)
        self.url = reverse(
            "admin:course_overviews_courseoverview_delete_course",
            args=[quote(self.course.id)],
        )

    def test_confirmation_shows_active_and_total_enrollments(self):
        active_user = UserFactory()
        inactive_user = UserFactory()
        CourseEnrollment.enroll(active_user, self.course.id)
        CourseEnrollment.enroll(inactive_user, self.course.id)
        CourseEnrollment.unenroll(inactive_user, self.course.id)

        response = self.client.get(self.url)

        assert response.status_code == 200
        assert response.context_data["active_enrollments"] == 1
        assert response.context_data["total_enrollments"] == 2

    @mock.patch("course_deletion.admin.delete_course")
    def test_exact_course_id_is_required(self, delete_course_mock):
        response = self.client.post(self.url, {"confirmation": "wrong-course"})

        assert response.status_code == 200
        self.assertContains(response, "Enter the complete course ID exactly as shown.")
        delete_course_mock.assert_not_called()

    @mock.patch("course_deletion.admin.SearchEngine.get_search_engine")
    @mock.patch("course_deletion.admin.delete_course")
    def test_delete_uses_platform_service_and_writes_admin_log(self, delete_course_mock, search_engine_mock):
        response = self.client.post(self.url, {"confirmation": str(self.course.id)})

        assert response.status_code == 302
        delete_course_mock.assert_called_once_with(self.course.id, self.user.id)
        search_engine_mock.return_value.remove.assert_called_once_with([str(self.course.id)])
        assert LogEntry.objects.filter(
            user=self.user,
            object_id=str(self.course.id),
            action_flag=DELETION,
        ).exists()

    @mock.patch(
        "course_deletion.admin.delete_course",
        side_effect=RuntimeError("deletion failed"),
    )
    def test_delete_failure_is_reported_without_success_audit(self, _delete_course_mock):
        response = self.client.post(self.url, {"confirmation": str(self.course.id)})

        assert response.status_code == 500
        self.assertContains(response, "The operation did not complete.", status_code=500)
        assert not LogEntry.objects.filter(
            user=self.user,
            object_id=str(self.course.id),
            action_flag=DELETION,
        ).exists()

    def test_staff_without_delete_permission_is_forbidden(self):
        password = "staff-password"
        staff_user = UserFactory(is_staff=True, password=password)
        assert self.client.login(username=staff_user.username, password=password)

        assert self.client.get(self.url).status_code == 403

    @mock.patch("course_deletion.admin.contentstore")
    @mock.patch("course_deletion.admin.delete_course")
    def test_selected_asset_option(self, delete_course_mock, contentstore_mock):
        response = self.client.post(self.url, {
            "confirmation": str(self.course.id),
            "keep_instructors": "on",
            "remove_assets": "on",
        })

        assert response.status_code == 302
        delete_course_mock.assert_called_once_with(self.course.id, self.user.id)
        contentstore_mock.return_value.delete_all_course_assets.assert_called_once_with(self.course.id)

    @mock.patch("course_deletion.admin.contentstore")
    def test_real_deletion_keeps_assets_by_default(self, contentstore_mock):
        response = self.client.post(self.url, {"confirmation": str(self.course.id)})

        assert response.status_code == 302
        assert self.store.get_course(self.course.id) is None
        contentstore_mock.assert_not_called()
