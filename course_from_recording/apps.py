"""
Course from a Recording - App Configuration.

The Studio side of the "From a teaching recording" tool: a course author uploads
a teaching recording, the studio tool service (a separate container, see the
wb-academy-studio-tool-service repo) processes it in the background, and this
app keeps track of each job.

This app keeps the job records, hands out S3 upload links, puts jobs on the
service's Redis queue, and receives the service's progress updates. The heavy
audio work never runs inside Open edX.

Studio-only (CMS).
"""

import logging

from django.apps import AppConfig

from edx_django_utils.plugins import PluginSettings, PluginURLs
from openedx.core.djangoapps.plugins.constants import ProjectType, SettingsType

log = logging.getLogger(__name__)


class CourseFromRecordingConfig(AppConfig):
    name = "course_from_recording"
    label = "course_from_recording"

    # Title shown for this app's models group in the Django admin.
    verbose_name = "Course from a Recording"

    # See: https://edx.readthedocs.io/projects/edx-django-utils/en/latest/edx_django_utils.plugins.html
    plugin_app = {
        PluginURLs.CONFIG: {
            ProjectType.CMS: {
                PluginURLs.NAMESPACE: name,
                PluginURLs.REGEX: "^",
                PluginURLs.RELATIVE_PATH: "urls",
            }
        },
        PluginSettings.CONFIG: {
            ProjectType.CMS: {
                SettingsType.PRODUCTION: {PluginSettings.RELATIVE_PATH: "settings.production"},
                SettingsType.COMMON: {PluginSettings.RELATIVE_PATH: "settings.common"},
            }
        },
    }

    def ready(self):
        log.debug("{label} is ready.".format(label=self.label))
