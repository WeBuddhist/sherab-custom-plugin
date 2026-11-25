"""
Signal handlers for offline content generation.
"""
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from openedx.core.djangoapps.waffle_utils.models import WaffleFlagCourseOverrideModel
from openedx.features.offline_mode.tasks import generate_offline_content_for_course
from openedx.features.offline_mode.toggles import WAFFLE_FLAG_NAMESPACE, is_offline_mode_enabled

OFFLINE_MODE_FLAG_NAME = f"{WAFFLE_FLAG_NAMESPACE}.enable_offline_mode"


@receiver(post_save, sender=WaffleFlagCourseOverrideModel)
def build_zips_when_override_saved(sender, instance, **kwargs):  # pylint: disable=unused-argument
    """
    Trigger 2: fires when an admin saves a course-level waffle override in Django Admin.

    If a course is already published and an admin turns on the offline mode flag,
    no publish happens. This receiver kicks off the ZIP build immediately on commit.
    """
    if instance.waffle_flag != OFFLINE_MODE_FLAG_NAME:
        return

    if not is_offline_mode_enabled(instance.course_id):
        return

    transaction.on_commit(
        lambda: generate_offline_content_for_course.apply_async([str(instance.course_id)])
    )
