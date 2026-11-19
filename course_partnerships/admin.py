from django.contrib import admin
from django.utils import timezone

from .models import *


class PartnerAdmin(admin.ModelAdmin):
    list_display = ["name", "activate_school_admin"]
    search_fields = ["name"]
    prepopulated_fields = {"slug": ("name",)}


class CenterAdmin(admin.ModelAdmin):
    list_display = ["name", "partner"]
    search_fields = ["name"]
    prepopulated_fields = {"slug": ("name",)}


class CategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "partner", "show_on_homepage"]
    search_fields = ["name"]
    raw_id_fields = ["partner"]


class EnhancedCourseAdmin(admin.ModelAdmin):
    list_display = ["course", "partner", "center", "category", "display_order"]
    list_editable = ["display_order"]
    list_filter = ["partner"]
    ordering = ["partner__name", "display_order", "course__display_name"]
    search_fields = ["course_id"]
    raw_id_fields = ["course", "partner", "center", "category"]


class HeroCourseAdmin(admin.ModelAdmin):
    # Editable inline so rearranging cards or extending a badge is one submit.
    list_display = ["course", "order", "is_active", "new_until", "badge_showing"]
    list_editable = ["order", "is_active", "new_until"]
    list_filter = ["is_active"]
    # Searches the course title, not just the raw key.
    search_fields = ["course__id", "course__display_name"]
    raw_id_fields = ["course"]
    ordering = ["order", "id"]

    # Shows whether the badge is actually live today. Computed, so it can't be
    # list_editable or list_filter.
    @admin.display(boolean=True, description="Badge live")
    def badge_showing(self, obj):
        """
        Returns whether this pick's badge is showing on the site right now.
        """
        return bool(obj.new_until and obj.new_until >= timezone.localdate())


class PartnerOrganizationMappingAdmin(admin.ModelAdmin):
    list_display = ("partner", "organization", "display_name", "show_in_mobile_app")
    list_filter = ("show_in_mobile_app", "partner", "organization")
    search_fields = (
        "partner__name",
        "organization__name",
        "organization__short_name",
        "display_name",
    )


class CourseCreatorAdmin(admin.ModelAdmin):
    list_display = ("name", "partner", "title", "experience")
    list_filter = ("partner",)
    search_fields = ("name", "title", "partner__name")
    raw_id_fields = ("partner",)
    readonly_fields = ("created", "modified")
    fieldsets = (
        (
            "Basic Information",
            {
                "fields": ("partner", "name", "profile_picture"),
            },
        ),
        (
            "Professional Details",
            {
                "fields": ("title", "experience", "bio"),
            },
        ),
    )


class HomepageAnnouncementAdmin(admin.ModelAdmin):
    list_display = ["message_preview", "tone", "enabled", "start_at", "end_at"]
    list_display_links = ["message_preview"]
    list_editable = ["enabled"]
    list_filter = ["tone", "enabled"]
    ordering = ["-id"]

    @admin.display(description="Message")
    def message_preview(self, obj):
        return str(obj)


admin.site.register(Partner, PartnerAdmin)
admin.site.register(HomepageAnnouncement, HomepageAnnouncementAdmin)
admin.site.register(Category, CategoryAdmin)
admin.site.register(EnhancedCourse, EnhancedCourseAdmin)
admin.site.register(Center, CenterAdmin)
admin.site.register(HeroCourse, HeroCourseAdmin)
admin.site.register(PartnerOrganizationMapping, PartnerOrganizationMappingAdmin)
admin.site.register(CourseCreator, CourseCreatorAdmin)
