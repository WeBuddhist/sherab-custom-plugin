"""
Common Pluggable Django App settings for course_from_recording.
"""


def plugin_settings(settings):
    """
    Injects Course from a Recording settings into the Django settings object.
    """
    # Queue, bucket, folder and shared secret of the studio tool service. Set by the
    # studio_tool_service Tutor plugin; while it is empty the API answers 503.
    settings.STUDIO_TOOL_SERVICE = getattr(settings, "STUDIO_TOOL_SERVICE", {})

    # Largest recording an author can upload, in megabytes.
    settings.COURSE_FROM_RECORDING_MAX_UPLOAD_MB = getattr(settings, "COURSE_FROM_RECORDING_MAX_UPLOAD_MB", 2048)
