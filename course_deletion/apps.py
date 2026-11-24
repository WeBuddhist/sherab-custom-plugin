"""CMS-only Course Overview admin extension for course deletion."""

from django.apps import AppConfig


class CourseDeletionConfig(AppConfig):
    name = "course_deletion"
    verbose_name = "Course Deletion"
    plugin_app = {}
