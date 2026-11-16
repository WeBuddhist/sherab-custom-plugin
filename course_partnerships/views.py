import logging

from common.djangoapps.edxmako.shortcuts import render_to_response
from common.djangoapps.student.models import CourseEnrollment
from django.db.models import Count, Exists, OuterRef, Prefetch
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.vary import vary_on_headers
from django.views.generic import View
from edx_rest_framework_extensions.auth.jwt.authentication import JwtAuthentication
from lms.djangoapps.courseware.access import has_access
from lms.djangoapps.courseware.courses import get_permission_for_course_about
from openedx.core.djangoapps.content.course_overviews.models import CourseOverview
from openedx.core.djangolib.markup import clean_dangerous_html
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from organizations.api import get_organizations
from xmodule.course_block import CATALOG_VISIBILITY_CATALOG_AND_ABOUT

from .helpers import get_course_key_or_error
from .models import *
from .serializers import (
    HeroCourseCardSerializer,
    HomepageAnnouncementSerializer,
    HomepageCategorySerializer,
    PartnerOrganizationMappingSerializer,
    PartnerSerializer,
)

log = logging.getLogger(__name__)

# The homepage hero shows a fixed pair of floating course cards.
HERO_COURSE_COUNT = 2

# Look further than 2 enrollments in case some don't have an EnhancedCourse row
# yet (courses added before this plugin was installed).
ENROLLMENT_SCAN_LIMIT = 10


class PartnerDetailView(View):
    """
    View for Partner Details
    """

    def get(self, request, slug):
        try:
            partner = Partner.objects.get(slug=slug)
        except Exception as e:
            raise Http404

        centers = Center.objects.filter(partner=partner)
        category_ids = CourseOverview.objects.filter(enhancedcourse__partner=partner).values_list(
            "enhancedcourse__category", flat=True
        )
        categories = (
            Category.objects.filter(id__in=category_ids)
            .annotate(num_courses=Count("enhancedcourse"))
            .filter(num_courses__gt=0)
        )
        partner_courses = CourseOverview.objects.filter(enhancedcourse__partner=partner).order_by(
            "enhancedcourse__display_order", "display_name", "id"
        )
        course_creators = CourseCreator.objects.filter(partner=partner)
        context = {
            "partner": partner,
            "centers": centers,
            "categories": categories,
            "partner_courses": partner_courses,
            "course_creators": course_creators,
        }
        return render_to_response("course_partnerships/partner-details.html", context)


class CenterDetailView(View):
    """
    View for Center Details
    """

    def get(self, request, partner_slug, center_slug):
        try:
            partner = Partner.objects.get(slug=partner_slug)
        except Exception as e:
            raise Http404

        try:
            center = Center.objects.get(partner=partner, slug=center_slug)
        except Exception as e:
            raise Http404

        category_ids = CourseOverview.objects.filter(enhancedcourse__partner=partner).values_list(
            "enhancedcourse__category", flat=True
        )
        categories = (
            Category.objects.filter(id__in=category_ids)
            .annotate(num_courses=Count("enhancedcourse"))
            .filter(num_courses__gt=0)
        )
        context = {"partner": partner, "center": center, "categories": categories}
        return render_to_response("course_partnerships/center-details.html", context)


class PublicAPIViewMixin:
    """
    Shared access policy for every public (anonymous-accessible) endpoint in
    this app, regardless of whether it's list-shaped or not:

    - authentication is skipped entirely rather than attempted and failed;
    - anonymous access is granted explicitly, so a future change to the
      platform-wide permission default can't silently lock these down.
    """

    authentication_classes = []
    permission_classes = [AllowAny]


class PublicListAPIView(PublicAPIViewMixin, ListAPIView):
    """
    Base class for the public listing endpoints in this app.

    These endpoints are read by anonymous clients (the homepage and the mobile
    app) and each is expected to return its whole list as a bare JSON array, so
    on top of PublicAPIViewMixin's access policy:

    - the platform-wide pagination default is disabled, which would otherwise
      cap responses at PAGE_SIZE and wrap them in a
      {count, next, previous, results} envelope that no client here expects;
    - filtering is disabled, since a subclass may return an already-evaluated
      list that a filter backend could not handle.
    """

    pagination_class = None
    filter_backends = []


class PartnerListAPIView(PublicListAPIView):
    """
    API endpoint to retrieve partner-organization mappings.

    Each entry in the response corresponds to a unique pair of:
        - Partner (name + logo)
        - Organization (short_name)

    Only mappings marked with `show_in_mobile_app=True` are returned.

    Method:
        GET

    Example Response (200 OK):
        [
            {
                "partner_name": "Partner Name",
                "logo": "https://yourdomain.com/../partner_logo.png",
                "organization": "org1"
            },
            ...
        ]
    """

    serializer_class = PartnerOrganizationMappingSerializer
    queryset = PartnerOrganizationMapping.objects.filter(show_in_mobile_app=True)


class PartnerHomepageListAPIView(PublicListAPIView):
    """
    API endpoint to retrieve all partners, for display on the homepage
    schools-and-partners carousel. Unlike PartnerListAPIView, this is not
    filtered by `show_in_mobile_app` — it mirrors the old homepage template's
    `Partner.objects.all()` behavior, since the homepage carousel and the
    mobile app's partner list serve different purposes and audiences.

    Method:
        GET

    Example Response (200 OK):
        [
            {
                "partner_name": "Partner Name",
                "logo": "https://yourdomain.com/../partner_logo.png",
                "slug": "partner-slug"
            },
            ...
        ]
    """

    serializer_class = PartnerSerializer

    # Explicit ordering keeps the carousel stable: an unordered queryset lets
    # the database return rows in any order, which can reshuffle the logos
    # between requests.
    queryset = Partner.objects.order_by("name")


# Personalized for a signed-in caller: the dismissal id lives in that login's
# session. A shared cache must not reuse one visitor's id for another.
@method_decorator(never_cache, name="dispatch")
@method_decorator(vary_on_headers("Authorization", "Cookie"), name="dispatch")
class HomepageAnnouncementAPIView(APIView):
    """
    The notice shown above the Catalog homepage hero.

    Returns the newest saved row that is enabled and inside its time window.
    A blank start shows immediately. A blank end does not expire. When no row
    qualifies, the response is 204 so the homepage renders no banner.

    A signed-in caller also receives dismissal_session_id. That id stays the
    same until logout, and the next login receives a different one. A
    signed-out caller receives null. Login is recognized from the Catalog
    token or the session cookie, and anonymous visitors still get the banner.

    Method:
        GET

    Example Response (200 OK):
        {
            "message": "Registration for the new term is now open.",
            "tone": "info",
            "dismissal_session_id": null
        }
    """

    authentication_classes = (JwtAuthentication, SessionAuthentication)
    permission_classes = [AllowAny]

    def get(self, request):
        now = timezone.now()
        announcement = None
        for row in HomepageAnnouncement.objects.filter(enabled=True).order_by("-id"):
            if homepage_announcement_is_active(row.enabled, row.start_at, row.end_at, now) and row.message.strip():
                announcement = row
                break

        if announcement is None:
            return Response(status=status.HTTP_204_NO_CONTENT)

        return Response(
            HomepageAnnouncementSerializer(announcement, context={"request": request}).data
        )


class HomepageCategoryListAPIView(PublicListAPIView):
    """
    API endpoint to retrieve the homepage course categories with their courses.

    Returns every category flagged `show_on_homepage`, each with the full list
    of courses filed under it, so the homepage can render its category tabs and
    switch between them without further requests.

    Categories left with no visible courses are omitted, so a tab never opens
    onto an empty grid.

    Method:
        GET

    Example Response (200 OK):
        [
            {
                "id": 1,
                "name": "Category Name",
                "courses": [
                    {
                        "course_id": "course-v1:Org+Course+Run",
                        "title": "Course Title",
                        "image_url": "https://yourdomain.com/../course_image.jpg",
                        "provider_name": "Provider Name",
                        "provider_logo": "https://yourdomain.com/../provider_logo.png"
                    },
                    ...
                ]
            },
            ...
        ]
    """

    serializer_class = HomepageCategorySerializer

    def get_queryset(self):
        """
        Return the homepage categories, each carrying its visible courses.

        Returns:
            list[Category]: Categories that have at least one visible course,
                each with a `visible_courses` attribute the serializer reads.
        """
        visible_courses = (
            EnhancedCourse.objects
            # select_related is a correctness guard as much as a performance
            # one: the link to CourseOverview has no database constraint, so
            # the join also discards rows pointing at courses that no longer
            # exist, which would otherwise raise when serialized.
            .select_related("course", "partner", "center")
            .filter(
                # This endpoint is public and unauthenticated, so restrict it
                # to courses that are meant to be publicly listed. The old
                # homepage template applied neither filter and would happily
                # advertise a staff-only course to anonymous visitors.
                course__visible_to_staff_only=False,
                course__catalog_visibility=CATALOG_VISIBILITY_CATALOG_AND_ABOUT,
            )
            .order_by("-course__start", "course__id")
        )

        categories = (
            Category.objects.filter(show_on_homepage=True)
            # Categories have no ordering field of their own, and an unordered
            # queryset lets the database reshuffle the tabs between requests.
            .order_by("name", "id")
            .prefetch_related(
                Prefetch("enhancedcourse_set", queryset=visible_courses, to_attr="visible_courses")
            )
        )

        # Drop empty categories here rather than with a Count annotation: the
        # count would be taken before the visibility filter above, so a
        # category holding only hidden courses would survive it and then render
        # an empty tab. The prefetched lists are already loaded, so filtering
        # in Python costs no extra queries.
        return [category for category in categories if category.visible_courses]


class InviteInstructionsAPIView(PublicAPIViewMixin, APIView):
    """
    API endpoint to retrieve a course's invite-only instructions.

    Public and unauthenticated: a visitor to an invite-only course's about
    page may well be signed out, and the instructions themselves carry no
    sensitive information.

    Resolves course_id -> EnhancedCourse -> partner -> invite_instructions,
    restricted to courses the caller may see an about page for. That rule is
    the platform's, not ours: has_access() with the permission from
    get_permission_for_course_about(), which honours the configurable
    COURSE_ABOUT_VISIBILITY_PERMISSION (and any per-site override) rather
    than hardcoding a catalog_visibility value here that would drift from
    upstream — and, unlike a hardcoded "both", it admits the invite-only
    courses actually set to "about", which are the ones this endpoint exists
    to serve. visible_to_staff_only is then checked separately, since the
    about-page permissions don't consider it (see get() below).

    A course the caller may not see gets the same 404 as one that doesn't
    exist at all, so this endpoint never reveals that a hidden course
    exists — a visible course with no EnhancedCourse row, no partner, or a
    partner with no instructions set all resolve to a 200 response with a
    null value instead, since the caller decides how to fall back on
    missing content.

    invite_instructions is authored in a rich-text field, so its HTML is run
    through clean_dangerous_html() on the way out: the caller renders it
    unescaped and can't sanitize it itself. This is the same filter the
    theme applies to the sibling Partner.content field when rendering it
    from a Mako template (see partner-details.html).

    Method:
        GET

    Example Response (200 OK):
        {"invite_instructions": "<p>Contact admissions@example.com</p>"}
        {"invite_instructions": null}

    Example Response (404 Not Found):
        {"error": "Course not found: ..."}
    """

    def get(self, request, course_id):
        course_key, error = get_course_key_or_error(course_id)
        if error:
            return error

        # One response for "no such course" and for "you may not see this
        # course", so the endpoint can't be probed to discover hidden courses.
        not_found = Response(
            {"error": _("Course not found: {course_id}").format(course_id=course_id)},
            status=status.HTTP_404_NOT_FOUND,
        )

        try:
            course_overview = CourseOverview.get_from_id(course_key)
        except (CourseOverview.DoesNotExist, IOError):
            # IOError: a stale or broken CourseOverview row the modulestore
            # can no longer load — from a caller's perspective that is
            # indistinguishable from the course not existing.
            return not_found

        if not has_access(request.user, get_permission_for_course_about(), course_overview):
            return not_found

        # Deliberately on top of the permission above, not folded into it. The
        # about-page permissions ('see_about_page', and 'see_in_catalog') only
        # look at catalog_visibility — visible_to_staff_only is consulted by
        # 'see_exists' alone, via can_load(). So with this site's configured
        # COURSE_ABOUT_VISIBILITY_PERMISSION a staff-only course would hand its
        # instructions to an anonymous caller. Keep that flag honoured whatever
        # the setting happens to be.
        if course_overview.visible_to_staff_only and not has_access(request.user, "staff", course_overview):
            return not_found

        enhanced_course = EnhancedCourse.objects.select_related("partner").filter(course_id=course_key).first()
        partner = enhanced_course.partner if enhanced_course else None
        invite_instructions = partner.invite_instructions if partner else None

        return Response({"invite_instructions": clean_dangerous_html(invite_instructions)})


# Personalized per caller, so it must never be cached — a shared cache could
# serve one learner's courses to another.
@method_decorator(never_cache, name="dispatch")
@method_decorator(vary_on_headers("Authorization", "Cookie"), name="dispatch")
class HeroCourseListAPIView(ListAPIView):
    """
    API endpoint for the courses shown in the homepage hero's floating cards.

    A signed-in visitor gets their most recently enrolled courses, newest first.
    Any slot left over — one enrollment, or a new account with none — is filled
    from the staff-curated `HeroCourse` picks, which are also all a signed-out
    visitor sees. So the hero has cards to render for everyone, and a learner's
    own courses always take precedence over the curated ones.

    Fewer than `HERO_COURSE_COUNT` courses is a valid response, not an error:
    nothing is curated on a fresh install.

    A card reports `is_new` when staff gave the course a `HeroCourse.new_until`
    date that has not passed, whichever route the card took into the hero — a
    curated pick or the caller's own enrollment.

    A card also reports `is_enrolled`: true for a card sourced from the
    caller's own enrollments, false for a curated pick they have not joined.

    Method:
        GET

    Example Response (200 OK):
        [
            {
                "course_id": "course-v1:Org+Course+Run",
                "title": "Course Title",
                "image_url": "https://yourdomain.com/../course_image.jpg",
                "provider_name": "Provider Name",
                "provider_logo": "https://yourdomain.com/../provider_logo.png",
                "is_new": false,
                "is_enrolled": false
            },
            ...
        ]
    """

    # Response depends on who's asking, so auth is attempted (unlike
    # PublicListAPIView) but stays optional, so signed-out visitors get 200s.
    authentication_classes = (JwtAuthentication, SessionAuthentication)
    permission_classes = [AllowAny]
    # Fixed-length list rendered as-is — pagination and filtering don't apply.
    pagination_class = None
    filter_backends = []
    serializer_class = HeroCourseCardSerializer

    def get_queryset(self):
        """
        Return the courses for the hero cards, in the order to render them.

        Returns:
            list[EnhancedCourse]: At most HERO_COURSE_COUNT courses, the
                caller's own enrollments first and curated picks after.
        """
        courses = self._recently_enrolled(self.request.user)

        if len(courses) < HERO_COURSE_COUNT:
            courses = self._top_up(courses, self._curated())

        return courses

    def _recently_enrolled(self, user):
        """
        Return the user's most recently enrolled courses, newest first.

        Args:
            user (User or AnonymousUser): The requesting user.

        Returns:
            list[EnhancedCourse]: The user's newest enrollments, empty for an
                anonymous caller.
        """
        if not user.is_authenticated:
            return []

        # Ordering isn't the model default, so it has to be requested explicitly.
        course_ids = list(
            CourseEnrollment.objects.filter(user=user, is_active=True)
            .order_by("-created", "-id")
            .values_list("course_id", flat=True)[:ENROLLMENT_SCAN_LIMIT]
        )

        # No visibility filter here: the user is already enrolled, so their own
        # course should show even if it's unlisted from the public catalog.
        courses = self._in_key_order(self._enhanced_courses(), course_ids)[:HERO_COURSE_COUNT]

        for course in courses:
            course.is_enrolled = True

        return courses

    def _curated(self):
        """
        Return the staff-curated hero picks, in the order set in the admin.

        Returns:
            list[EnhancedCourse]: The active picks that are safe to show
                publicly.
        """
        course_ids = list(
            HeroCourse.objects.filter(is_active=True)
            .order_by("order", "id")
            .values_list("course_id", flat=True)
        )

        # Shown to anonymous visitors, so held to the same visibility rules as
        # the homepage categories endpoint above.
        queryset = self._enhanced_courses().filter(
            course__visible_to_staff_only=False,
            course__catalog_visibility=CATALOG_VISIBILITY_CATALOG_AND_ABOUT,
        )
        courses = self._in_key_order(queryset, course_ids)

        for course in courses:
            course.is_enrolled = False

        return courses

    @staticmethod
    def _enhanced_courses():
        """
        Return the base queryset every hero card is serialized from.

        Returns:
            QuerySet: EnhancedCourse rows with their branding relations loaded
                and the "new course" flag annotated on.
        """
        # select_related also drops courses removed from the modulestore (no DB
        # constraint links them). is_new is annotated here so both enrollment
        # and curated cards get it automatically; is_active is unrelated — it
        # controls curation, not the badge.
        return EnhancedCourse.objects.select_related("course", "partner", "center").annotate(
            is_new=Exists(
                HeroCourse.objects.filter(
                    course_id=OuterRef("course_id"),
                    new_until__gte=timezone.localdate(),
                )
            ),
        )

    @staticmethod
    def _in_key_order(queryset, course_ids):
        """
        Resolve course keys to EnhancedCourse rows, keeping the given order.

        Args:
            queryset (QuerySet): EnhancedCourse rows to draw from.
            course_ids (list): Course keys, in the order to return them.

        Returns:
            list[EnhancedCourse]: The rows found, in `course_ids` order.
        """
        if not course_ids:
            return []

        # course_id__in doesn't preserve order, so it's reapplied here. Courses
        # with no EnhancedCourse row just drop out.
        rows = {row.course_id: row for row in queryset.filter(course_id__in=course_ids)}
        return [rows[course_id] for course_id in course_ids if course_id in rows]

    @staticmethod
    def _top_up(courses, extras):
        """
        Fill the remaining hero slots from `extras`, skipping duplicates.

        Args:
            courses (list[EnhancedCourse]): Courses already claimed.
            extras (list[EnhancedCourse]): Courses to fill the rest with.

        Returns:
            list[EnhancedCourse]: At most HERO_COURSE_COUNT courses.
        """
        # Skip a curated pick if the learner is already enrolled in it, so the
        # same course never fills two card slots.
        seen = {course.course_id for course in courses}
        for extra in extras:
            if len(courses) >= HERO_COURSE_COUNT:
                break
            if extra.course_id in seen:
                continue
            courses.append(extra)
            seen.add(extra.course_id)

        return courses


class OrganizationDisplayNamesView(View):
    """Return full organization names keyed by their short names."""

    @method_decorator(login_required)
    def get(self, request, *args, **kwargs):  # pylint: disable=unused-argument
        """Return the labels used when displaying organization identifiers in Studio."""
        return JsonResponse({
            organization["short_name"]: organization["name"]
            for organization in get_organizations()
            if organization.get("name")
        })
