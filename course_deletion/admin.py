"""Add safe course deletion to the Studio Course Overview admin."""

import logging

from django.contrib import admin, messages
from django.contrib.admin.sites import NotRegistered
from django.contrib.admin.utils import quote, unquote
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.decorators import method_decorator
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods

from cms.djangoapps.contentstore.utils import delete_course
from common.djangoapps.student.models import CourseEnrollment
from openedx.core.djangoapps.content.course_overviews.admin import CourseOverviewAdmin
from openedx.core.djangoapps.content.course_overviews.models import CourseOverview
from xmodule.modulestore.django import modulestore
from xmodule.contentstore.django import contentstore

log = logging.getLogger(__name__)


class SherabCourseOverviewAdmin(CourseOverviewAdmin):
    """Keep the platform listing and add a CMS-backed deletion action."""

    def get_urls(self):
        custom_urls = [
            path(
                "<path:object_id>/delete-course/",
                self.admin_site.admin_view(self.delete_course_view),
                name="course_overviews_courseoverview_delete_course",
            ),
        ]
        return custom_urls + super().get_urls()

    def get_list_display(self, request):
        fields = [
            'id',
            'display_name',
            'created',
            'modified',
        ]
        if self._can_delete_courses(request):
            fields.append("delete_course_link")
        return fields

    def has_delete_permission(self, request, obj=None):
        """Disable Django's ORM deletion, which only removes the overview row."""
        return False

    @admin.display(description=_("Actions"))
    def delete_course_link(self, obj):
        url = reverse(
            "admin:course_overviews_courseoverview_delete_course",
            args=[quote(obj.pk)],
        )
        return format_html('<a class="button" style="white-space: nowrap" href="{}">{}</a>', url, _("Delete course"))

    @method_decorator(require_http_methods(["GET", "POST"]))
    def delete_course_view(self, request, object_id):
        if not self._can_delete_courses(request):
            raise PermissionDenied

        course_overview = self.get_object(request, unquote(object_id))
        if course_overview is None:
            raise Http404
        course = modulestore().get_course(course_overview.id)
        if course is None or course.id != course_overview.id:
            raise Http404(_("The course content no longer exists."))

        enrollment_query = CourseEnrollment.objects.filter(course_id=course_overview.id)
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": _("Delete course"),
            "course": course_overview,
            "active_enrollments": enrollment_query.filter(is_active=True).count(),
            "total_enrollments": enrollment_query.count(),
            "remove_assets_selected": request.POST.get("remove_assets") == "on",
        }

        if request.method == "POST":
            if request.POST.get("confirmation", "").strip() != str(course_overview.id):
                context["confirmation_error"] = _("Enter the complete course ID exactly as shown.")
            else:
                remove_assets = context["remove_assets_selected"]
                log.info(
                    "Course deletion requested: course=%s actor=%s active_enrollments=%s total_enrollments=%s "
                    "remove_assets=%s",
                    course_overview.id,
                    request.user.username,
                    context["active_enrollments"],
                    context["total_enrollments"],
                    remove_assets,
                )
                try:
                    delete_course(course_overview.id, request.user.id)
                    self.log_deletions(request, [course_overview])
                    if remove_assets:
                        contentstore().delete_all_course_assets(course_overview.id)
                except Exception:  # pylint: disable=broad-except
                    log.exception(
                        "Course deletion failed: course=%s actor=%s",
                        course_overview.id,
                        request.user.username,
                    )
                    context["deletion_error"] = _("The operation did not complete. Check the server logs before retrying.")
                    return TemplateResponse(
                        request,
                        "admin/course_overviews/delete_course_confirmation.html",
                        context,
                        status=500,
                    )

                log.info(
                    "Course deleted: course=%s actor=%s remove_assets=%s",
                    course_overview.id, request.user.username, remove_assets,
                )
                self.message_user(
                    request,
                    _("Course %(course_id)s was deleted.") % {"course_id": course_overview.id},
                    messages.SUCCESS,
                )
                return HttpResponseRedirect(reverse("admin:course_overviews_courseoverview_changelist"))

        return TemplateResponse(
            request,
            "admin/course_overviews/delete_course_confirmation.html",
            context,
        )

    @staticmethod
    def _can_delete_courses(request):
        return request.user.has_perm("course_overviews.delete_courseoverview")


try:
    admin.site.unregister(CourseOverview)
except NotRegistered:
    pass

admin.site.register(CourseOverview, SherabCourseOverviewAdmin)
