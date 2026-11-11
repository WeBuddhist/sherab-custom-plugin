from django.contrib import admin

from .models import RecordingJob


@admin.register(RecordingJob)
class RecordingJobAdmin(admin.ModelAdmin):
    list_display = ("id", "course_id", "original_filename", "status", "progress", "user", "modified")
    list_filter = ("status",)
    search_fields = ("id", "course_id", "original_filename", "user__username")
    readonly_fields = ("id", "created", "modified")
