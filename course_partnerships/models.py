"""
Models for course_partnerships

If you make changes to this model, be sure to create an appropriate migration
file and check it in at the same time as your model changes. To do that,

1. Go to the edx-platform dir
2. ./manage.py lms makemigrations --settings=production
3. ./manage.py lms migrate --settings=production
"""

import uuid

from ckeditor.fields import RichTextField
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel
from openedx.core.djangoapps.content.course_overviews.models import CourseOverview
from organizations.models import Organization

from .validators import validate_bannner_extension
from .storage import PartnerLogoStorage, CenterLogoStorage, CourseCreatorStorage


class Partner(TimeStampedModel):
    """
    Model for store schools and partners details
    """

    name = models.CharField(
        max_length=255,
        db_index=True,
    )
    slug = models.SlugField(
        max_length=255,
        db_index=True,
    )
    logo = models.ImageField(
        "logo",
        upload_to="logos/",
        storage=PartnerLogoStorage(),
        help_text=_(
            "Upload only image file with .png, .jpeg, .jpg extension. Recommended image size: W 240px * H 340px"
        ),
        validators=[validate_bannner_extension],
    )
    banner = models.ImageField(
        "Banner",
        blank=True,
        null=True,
        upload_to="banners/",
        storage=PartnerLogoStorage(),
        help_text=_("Upload only image file with .png, .jpeg, .jpg extension."),
        validators=[validate_bannner_extension],
    )
    content = RichTextField("Description", null=True, blank=True)
    invite_instructions = RichTextField("Invite Instructions", null=True, blank=True)
    activate_school_admin = models.BooleanField(default=False)

    def __str__(self):
        return self.name

    class Meta:
        app_label = "course_partnerships"
        verbose_name = "Schools and Partners"
        verbose_name_plural = "Schools and Partners"


class Center(TimeStampedModel):
    """
    Model for store Center details
    """

    partner = models.ForeignKey(Partner, db_index=True, on_delete=models.CASCADE)
    name = models.CharField(
        max_length=255,
        db_index=True,
    )
    slug = models.SlugField(
        max_length=255,
        db_index=True,
    )
    logo = models.ImageField(
        "logo",
        upload_to="logos/",
        storage=CenterLogoStorage(),
        help_text=_(
            "Upload only image file with .png, .jpeg, .jpg extension. Recommended image size: W 240px * H 340px"
        ),
        validators=[validate_bannner_extension],
    )
    banner = models.ImageField(
        "Banner",
        blank=True,
        null=True,
        upload_to="banners/",
        storage=CenterLogoStorage(),
        help_text=_("Upload only image file with .png, .jpeg, .jpg extension."),
        validators=[validate_bannner_extension],
    )
    content = RichTextField("Description", null=True, blank=True)

    def __str__(self):
        return self.name

    class Meta:
        app_label = "course_partnerships"
        verbose_name = "Centers"
        verbose_name_plural = "Centers"


class Category(TimeStampedModel):
    """
    Model for store course categories details
    """

    name = models.CharField(max_length=100)
    partner = models.ForeignKey(Partner, null=True, blank=True, db_index=True, on_delete=models.CASCADE)
    show_on_homepage = models.BooleanField(default=False)

    def __str__(self):
        return self.name

    class Meta:
        app_label = "course_partnerships"
        verbose_name = "Course Categories"
        verbose_name_plural = "Course Categories"


class EnhancedCourse(TimeStampedModel):
    """
    Model for store course releted extra details
    """

    course = models.OneToOneField(
        CourseOverview,
        db_constraint=False,
        db_index=True,
        on_delete=models.CASCADE,
    )
    partner = models.ForeignKey(Partner, null=True, blank=True, db_index=True, on_delete=models.SET(""))
    center = models.ForeignKey(Center, null=True, blank=True, db_index=True, on_delete=models.SET(""))
    category = models.ForeignKey(Category, null=True, blank=True, db_index=True, on_delete=models.SET(""))
    display_order = models.PositiveIntegerField(
        _("School page order"),
        default=0,
        help_text=_("Lower numbers are shown first on the school/partner page."),
    )

    class Meta:
        app_label = "course_partnerships"

    def __str__(self):
        return f"{self.course_id}"

    @classmethod
    def create_or_update(cls, course_id):
        """
        Create or update course detailss.
        """
        course, created = cls.objects.get_or_create(course_id=course_id)
        course.save()


class HeroCourse(TimeStampedModel):
    """
    A course featured in the homepage hero's pair of floating cards.

    Signed-in visitors see their own most recently enrolled courses there, so
    these curated picks are what a signed-out visitor sees instead — and what
    fills a leftover slot for a signed-in visitor who has not enrolled in two
    courses yet.
    """

    course = models.OneToOneField(
        CourseOverview,
        db_constraint=False,
        db_index=True,
        on_delete=models.CASCADE,
        help_text=_("Course to feature in the homepage hero."),
    )
    order = models.PositiveIntegerField(
        default=0,
        help_text=_("Lower numbers are shown first. Only the first two active picks are used."),
    )
    is_active = models.BooleanField(
        default=True,
        help_text=_("Uncheck to retire a pick without deleting it."),
    )
    # A date, not a checkbox, so the badge retires itself. Independent of
    # is_active: that controls curation, this controls the badge.
    new_until = models.DateField(
        null=True,
        blank=True,
        help_text=_(
            "Show a 'New course' badge until this date. Leave blank for none. "
            "Independent of 'is active'."
        ),
    )

    def __str__(self):
        return f"{self.course_id}"

    class Meta:
        app_label = "course_partnerships"
        # id breaks ties between picks sharing the same order.
        ordering = ("order", "id")
        verbose_name = "Homepage Hero Course"
        verbose_name_plural = "Homepage Hero Courses"


class PartnerOrganizationMapping(TimeStampedModel):
    """
    Mapping model between Partners and Organizations.

    Each record represents a unique relationship between a Partner and an Organization.
    This mapping is useful for defining which organizations a partner is associated with,
    and whether that relationship should be displayed in the mobile app.

    Fields:
        partner (ForeignKey): Reference to the Partner.
        organization (ForeignKey): Reference to the Organization.
        show_in_mobile_app (Boolean): If True, this mapping will be shown in the mobile app.
        display_name (CharField): Optional override for the partner's name in the UI.
    """

    partner = models.ForeignKey(
        Partner,
        on_delete=models.CASCADE,
        related_name="organization_mappings",
        help_text="The partner associated with the organization.",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="partner_mappings",
        help_text="The organization linked to the partner.",
    )
    show_in_mobile_app = models.BooleanField(
        default=False,
        help_text="Controls whether this partner-organization mapping is shown in the mobile app.",
    )
    display_name = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Custom name to display for the partner in this specific mapping.",
    )

    def __str__(self):
        return f"{self.partner.name} ↔ {self.organization.short_name}"

    class Meta:
        unique_together = ("partner", "organization")
        verbose_name = "Partner-Organization Mapping"
        verbose_name_plural = "Partner-Organization Mappings"


class CourseCreator(TimeStampedModel):
    """
    Model for storing course creator information.

    This model captures details about course creators/instructors including
    their profile picture, name, title, experience, and biography.
    """

    partner = models.ForeignKey(
        Partner,
        on_delete=models.CASCADE,
        db_index=True,
        related_name="course_creators",
        help_text=_("School/Partner this course creator is associated with"),
    )
    name = models.CharField(
        max_length=255,
        db_index=True,
        help_text=_("Full name of the course creator"),
    )
    title = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Professional title or designation (e.g., Professor, Senior Instructor)"),
    )
    experience = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text=_("Years of experience (e.g., 5)"),
    )
    bio = models.TextField(
        "Biography",
        max_length=2000,
        blank=True,
        null=True,
        help_text=_("Detailed biography of the course creator (max 2000 characters)"),
    )
    profile_picture = models.ImageField(
        "Profile Picture",
        upload_to="profile_pictures/",
        storage=CourseCreatorStorage(),
        blank=True,
        null=True,
        help_text=_("Upload only image file with .png, .jpeg, .jpg extension."),
        validators=[validate_bannner_extension],
    )

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Course Creator"
        verbose_name_plural = "Course Creators"


HOMEPAGE_ANNOUNCEMENT_TONE_INFO = "info"
HOMEPAGE_ANNOUNCEMENT_TONE_SUCCESS = "success"
HOMEPAGE_ANNOUNCEMENT_TONE_WARNING = "warning"
HOMEPAGE_ANNOUNCEMENT_TONE_ALERT = "alert"

HOMEPAGE_ANNOUNCEMENT_TONE_CHOICES = [
    (HOMEPAGE_ANNOUNCEMENT_TONE_INFO, _("Info")),
    (HOMEPAGE_ANNOUNCEMENT_TONE_SUCCESS, _("Success")),
    (HOMEPAGE_ANNOUNCEMENT_TONE_WARNING, _("Warning")),
    (HOMEPAGE_ANNOUNCEMENT_TONE_ALERT, _("Alert")),
]


# Stored inside the Django login session. It is not the session key.
HOMEPAGE_ANNOUNCEMENT_DISMISSAL_SESSION_KEY = "homepage_announcement_dismissal_id"


def homepage_announcement_dismissal_session_id(request):
    """
    Return this login's banner id, creating it the first time.

    Signed-out callers get None. The value is random and is not the Django
    session key. Logout clears the session, so the next login gets a new id
    and a closed banner shows again.
    """
    user = getattr(request, "user", None)
    session = getattr(request, "session", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    if session is None or not getattr(session, "session_key", None):
        return None
    if not session.get("_auth_user_id"):
        return None

    dismissal_id = session.get(HOMEPAGE_ANNOUNCEMENT_DISMISSAL_SESSION_KEY)
    if not dismissal_id:
        dismissal_id = uuid.uuid4().hex
        session[HOMEPAGE_ANNOUNCEMENT_DISMISSAL_SESSION_KEY] = dismissal_id
    return dismissal_id


def homepage_announcement_is_active(enabled, start_at, end_at, now):
    """
    Return whether a homepage announcement should be shown at `now`.

    A blank start shows as soon as the row is enabled. A blank end never
    expires on its own. The end instant itself is hidden, so a banner set
    to end at noon is gone at noon.
    """
    if not enabled:
        return False
    if start_at is not None and start_at > now:
        return False
    if end_at is not None and end_at <= now:
        return False
    return True


class HomepageAnnouncement(models.Model):
    """
    One admin-written notice shown above the Catalog homepage hero.

    Several rows can exist. The public API shows the newest row that
    homepage_announcement_is_active accepts, and nothing when none do.
    """

    message = models.TextField(
        help_text=_("Plain text shown in the banner. A URL is shown as text."),
    )
    tone = models.CharField(
        max_length=16,
        choices=HOMEPAGE_ANNOUNCEMENT_TONE_CHOICES,
        default=HOMEPAGE_ANNOUNCEMENT_TONE_INFO,
        help_text=_(
            "Info is a calm reminder, Success is positive, Warning is caution, and Alert is urgent."
        ),
    )
    enabled = models.BooleanField(
        default=False,
        help_text=_("Uncheck to hide the banner immediately, even inside the time window."),
    )
    start_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Leave blank to show as soon as the banner is enabled."),
    )
    end_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Leave blank to keep showing until the banner is disabled. Hidden once this time is reached."),
    )

    class Meta:
        app_label = "course_partnerships"
        verbose_name = "Homepage Announcement"
        verbose_name_plural = "Homepage Announcements"
        ordering = ["-id"]

    def __str__(self):
        text = self.message.strip()
        return text if len(text) <= 80 else f"{text[:77]}..."

    def clean(self):
        if self.start_at and self.end_at and self.end_at <= self.start_at:
            raise ValidationError({"end_at": _("End must be after start.")})
