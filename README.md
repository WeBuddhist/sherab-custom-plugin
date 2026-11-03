# sherab-custom-plugin

Custom Open edX plugins for the **Sherab / WeBuddhist Academy** platform, packaged
together as a single installable distribution (`custom-extensions`). The package
bundles six independent Django apps that plug into the LMS and/or CMS via the
[Open edX plugin framework](https://edx.readthedocs.io/projects/edx-django-utils/en/latest/edx_django_utils.plugins.html)
(`edx_django_utils.plugins`) — no edx-platform core code is modified.

| App | Runs in | Purpose |
| --- | --- | --- |
| [`ai_course_creator`](#ai_course_creator) | CMS (Studio) | Conversational AI ("Sherab") that designs and generates whole courses, and edits existing sections, using Google Gemini. |
| [`course_from_recording`](#course_from_recording) | CMS (Studio) | Jobs for the "From a teaching recording" tool: upload links, the studio tool service's queue, and progress. |
| [`course_schedule`](#course_schedule) | LMS + CMS | Per-section schedule (dates, times, instructor, tools, notes) set in the Studio outline and shown on the learner Dates tab. |
| [`course_partnerships`](#course_partnerships) | LMS | Partner organizations, centers, categories, and course-creator profiles, with branding assets and a mobile API. |
| [`user_extension`](#user_extension) | LMS | Extends user profiles with partner relationships; auto-approves ID verification on registration. |
| [`wishlist`](#wishlist) | LMS | Personal per-user course wishlist. |

---

## Installation

The plugin is installed into the edx-platform virtualenv. Under Tutor it is mounted
and listed as an extra pip requirement so it is picked up on image build / dev mount.

`config.yml`:

```yaml
OPENEDX_EXTRA_PIP_REQUIREMENTS:
  - google-api-python-client
  - google-genai
MOUNTS:
  - /path/to/sherab-custom-plugin   # dev: live-mounted into LMS & CMS
```

Python dependencies are listed in [`requirements/common.in`](requirements/common.in)
and installed automatically by `setup.py`.

The apps register through the entry points in [`setup.py`](setup.py):

```
cms.djangoapp:  ai_course_creator, course_from_recording, course_partnerships, course_schedule, user_extension
lms.djangoapp:  course_partnerships, course_schedule, user_extension, wishlist
```

After installing or changing models, run migrations for the relevant process, e.g.:

```bash
tutor dev exec cms ./manage.py cms migrate ai_course_creator
tutor dev exec lms ./manage.py lms migrate course_partnerships
```

---

## `ai_course_creator`

A **Studio-only (CMS)** conversational assistant — "Sherab" — that helps a course
creator design a course through chat, then generates the real Open edX course
structure for them. It also powers a per-section "Edit with SherabAI" sidebar for
improving an existing section. All LLM calls are **server-side only**; the Gemini
API key is never sent to the browser.

### Two flows

**1. Whole-course creator.** From an empty course outline the creator chats with
Sherab through a guided, four-phase flow (Learner → Transformation → Materials &
Assessment → Generate). When the conversation is ready, Sherab generates the full
outline and writes it into the course as **draft** sections, subsections, units,
and components.

**2. Per-section editor.** From a populated outline the creator opens a section in
the "Edit with SherabAI" sidebar. The conversation is anchored on that section's
**zero-to-hero transformation** (what a learner can do after the section that they
couldn't before). Sherab proposes concrete edits; nothing is written until the
creator clicks **Apply changes**, and edits are strictly scoped to that one section.

### Components

- **`models.py`**
  - `ChatSession` — one conversation, unique per `(user, course_id, section_locator)`.
    `section_locator` is empty for the creator flow and the chapter usage key for the
    per-section editor, so each section keeps its own thread. Caches the generated
    `course_json`, the display `current_phase`, the `generation_status` lifecycle, and
    the `created_section_locators` (for rollback / detecting existing Sherab content).
  - `ChatMessage` — a user/assistant turn within a session.
  - `UploadedMaterial` — course material shared by the creator (file/link/text),
    stored as extracted plain text (the binary is not retained).

- **`services/llm_client.py`** — thin wrapper over the `google-genai` SDK. Streams
  replies (`stream_reply`), makes JSON-mode generation calls (`complete_json`), loads
  the bundled skill as the system instruction, and parses/strips the embedded
  `COURSE_JSON` and `SECTION_EDITS` marker blocks. Handles rate-limit (429), 503
  "high demand", and oversized-request (413) errors with backoff/retry and
  user-friendly messages.

- **`services/generator.py`** — orchestrates two-stage, resumable course generation:
  a small **skeleton** call (titles + objectives + component plan), then one
  **content** call per section (lesson HTML + assessments). Yields progress events
  consumed by `GenerateCourseView`.

- **`services/course_builder.py`** — turns the generated `COURSE_JSON` into real
  Open edX structure by calling `contentstore`/modulestore directly (chapter →
  sequential → vertical → component). Builds CAPA problem OLX for several question
  types. Also exposes `user_can_author` (the author-permission check) and
  `delete_sections` (rollback).

- **`services/section_editor.py`** — `read_section` serializes one chapter subtree to
  a JSON tree (usage keys + content) for the LLM; `apply_section_edits` applies an
  operation list (`rename` / `editContent` / `add` / `delete` / `reorder`) strictly
  scoped to that chapter. Every usage key the model supplies is validated against the
  chapter's own descendant set before anything is mutated.

- **`services/materials.py`** — extracts plain text from PDF / DOCX / PPTX / plain
  text uploads, pasted text, and public URLs; truncates to the per-request context
  budget.

- **`skill/`** and **`skill_section_editor/`** — the `SKILL.md` prompt files (plus
  `skill/references/*.md`) that define Sherab's persona and conversation rules for the
  creator flow and the section editor respectively. Loaded as the model's system
  instruction.

- **`helpers.py`** — view helpers: SSE framing (`sse`), session lookup
  (`get_or_create_session`), edit-and-resend truncation (`truncate_for_edit`), the
  shared course-key + author-permission guard (`require_course_author`), and feature/
  limit settings accessors.

### API endpoints

All require an authenticated user; course-mutating endpoints additionally check
author access. Mounted at the site root, so paths are absolute on the Studio host.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/ai-course-creator/chat/` | Stream the assistant's next reply (SSE). |
| POST | `/api/ai-course-creator/upload/` | Add a material (file / link / text). |
| DELETE | `/api/ai-course-creator/material/<pk>/` | Delete an uploaded material. |
| POST | `/api/ai-course-creator/generate/` | Generate + write the course (SSE). |
| GET | `/api/ai-course-creator/config/` | Feature-flag state for the Studio UI. |
| GET / DELETE | `/api/ai-course-creator/session/` | Fetch (resume) or reset a conversation. |
| GET | `/api/ai-course-creator/section-content/` | Read one section's content tree. |
| POST | `/api/ai-course-creator/section-chat/` | Stream a per-section editor reply (SSE). |
| POST | `/api/ai-course-creator/apply-section/` | Apply the latest proposed section edits. |

### Settings

Defaults are set in `settings/common.py` / `settings/production.py`; the Gemini
credentials are normally injected into CMS settings from Tutor `config.yml` via the
`configuration_plugin` patch.

| Setting | Default | Purpose |
| --- | --- | --- |
| `GEMINI_API_KEY` | `""` | Google Gemini API key (server-side only). |
| `GEMINI_MODEL` | `gemini-1.5-pro` | Model id (e.g. `gemini-2.5-flash-lite`). |
| `AI_COURSE_CREATOR_ENABLED` | `True` | Master on/off switch for the feature. |
| `AI_COURSE_CREATOR_MAX_UPLOAD_BYTES` | `25 MB` | Max uploaded-material size. |
| `AI_COURSE_CREATOR_MAX_CONTEXT_CHARS` | `16000` | Max material text per request. |

Tutor wiring — set the values in `config.yml`:

```yaml
GEMINI_API_KEY: "AQ.xxxxxxxx"
GEMINI_MODEL: gemini-2.5-flash-lite
```

and inject them into CMS settings in `configuration_plugins.yml`:

```yaml
patches:
  openedx-cms-production-settings: |
    GEMINI_API_KEY = "{{ GEMINI_API_KEY }}"
    GEMINI_MODEL = "{{ GEMINI_MODEL }}"
```

then `tutor config save` and restart CMS.

> **Note:** the bundled skill is read once and cached (`lru_cache`) for the process
> lifetime, so restart the CMS after editing any `SKILL.md` to pick up changes.

The matching frontend lives in the **`frontend-app-authoring`** MFE under
`src/ai-course-creator/` (the creator modal and the section-editor sidebar).

---

## `course_from_recording`

**CMS-only.** The Studio side of the "From a teaching recording" tool. A course
author uploads a teaching recording, and the **studio tool service** (a separate
container from the `wb-academy-studio-tool-service` repo) cleans it, finds the
speakers and transcribes it in the background. The heavy audio work never runs
inside Open edX; this app only keeps track of the jobs.

- **Uploads go straight to S3.** The API returns a signed upload link for one file,
  with its content type and size in the signature, so the recording never passes
  through Studio and S3 refuses any other file.
- **Files** live in the private Open edX bucket under
  `studio-tools/course-from-recording/<job id>/`.
- **Queue:** starting a job pushes `course_from_recording:<job id>` onto the
  service's Redis list. The service trusts only the id and reads the rest from
  this app, so a queue entry this app didn't create leads nowhere.

### Models

- `RecordingJob`: one row per recording. Random UUID id (also its S3 folder),
  course, user, `status` (`uploading` → `queued` → `cleaning` → `finding_speakers` →
  `transcribing` → `transcript_ready`, or `failed`), `progress` (0-100 for the current
  step), `error`, the author's `metadata` (teacher, text, languages, place, year), the
  declared file name, type and size, `duration_seconds`, `transcribed_minutes` (Monlam
  cost tracking), and `results` (output name → path inside the job folder).

### Components

- `services.py`: everything that leaves Open edX: signed S3 links, checking the
  upload, and the Redis queue.
- `views.py`: the Studio API for the studio-extension MFE. Course authors only.
- `views_service.py`: the service's API. No user login: every request needs the
  shared secret in the `X-Service-Token` header, compared in constant time. The
  service may only report its own steps, only while a job is queued or running,
  and only output paths inside the job's folder.

### API endpoints

| Who | Method | Path | Purpose |
| --- | --- | --- | --- |
| Author | POST | `/api/course-from-recording/v1/jobs/` | Create a job: `{course_id, filename, content_type, size, consent, details: {teacher, text?, languages?, place?, year?}}`. Returns the job and `upload: {method, url, headers}`. |
| Author | GET | `/api/course-from-recording/v1/jobs/?course_id=...` | The course's latest job, or `null`, so the tool page can resume. |
| Author | GET | `/api/course-from-recording/v1/jobs/<id>/` | One job, with short-lived `downloads` links to its outputs. |
| Author | POST | `/api/course-from-recording/v1/jobs/<id>/start/` | Check the upload is in S3 with the declared size, then queue the job. |
| Author | POST | `/api/course-from-recording/v1/jobs/<id>/retry/` | Queue a failed job again; the service continues from the step that failed. |
| Service | GET | `/api/course-from-recording/v1/service/jobs/<id>/` | Job details, its S3 folder and the recording's key. |
| Service | PATCH | `/api/course-from-recording/v1/service/jobs/<id>/` | Progress report: any of `status`, `progress`, `error`, `results` (merged), `duration_seconds`, `transcribed_minutes`. |

### Settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `STUDIO_TOOL_SERVICE` | `{}` | `REDIS_URL`, `QUEUE`, `BUCKET`, `PREFIX`, `TOKEN`, `MAX_RECORDING_MINUTES`. Set by the `studio_tool_service` Tutor plugin; while it is incomplete the author API answers 503. |
| `COURSE_FROM_RECORDING_MAX_UPLOAD_MB` | `2048` | Largest recording an author can upload. |

S3 access uses Studio's own settings and key from `tutor-contrib-s3`
(`AWS_S3_REGION_NAME`, `AWS_ACCESS_KEY_ID`, ...). The bucket's CORS rule must allow
`PUT` from the MFE host.

### Frontend

- Entry point: the "Create with a tool" menu in the empty course outline
  (`frontend-app-authoring` `src/plugin-slots/CourseOutlineCreateToolsSlot/` and
  `src/plugins/RecordingCourseTool/`).
- Tool page: `frontend-app-studio-extension`, route `/course-from-recording/:courseId`.

### Tests

```bash
tutor dev exec cms python -m pytest -c cms/pytest.ini --ds=cms.envs.test --rootdir=/openedx/edx-platform -p conftest \
    /mnt/sherab-custom-plugin/course_from_recording/tests/
```

---

## `course_schedule`

**LMS + CMS.** Lets course creators schedule each section (chapter) of an
**instructor-paced** course from the Studio course outline, and shows that schedule
to learners on the Dates tab, next to the platform's own dates.

- **Informational only.** It never changes release dates, due dates or access, and
  never writes to the modulestore or edx-when. It lives in its own tables, so dates
  can be moved freely without republishing.
- **One timezone per course.** Dates and times are wall-clock values in the course
  timezone, and learners see them in that timezone too.
- **Title** is the section's display name, read live, so it follows renames.

### Models

- `CourseScheduleSettings`: the course timezone (default `COURSE_SCHEDULE_DEFAULT_TIMEZONE`).
- `SectionSchedule`: one row per scheduled section, holding `start_date`, `num_days`
  (a section can span several days), optional `start_time`/`end_time`,
  `instructor`, `tools_required` and `notes`.

### Components

- `services.py`: pure logic, unit-tested without edx-platform. It computes warnings
  (overlap, before course start, after course end, before content release, subsection
  due before the session), the "content available from" date, and session ordering.
  Warnings never block saving. Auto-fill and shifting later sections happen in the
  authoring MFE, so the creator sees a preview before saving.
- `views_cms.py`: the Studio API. Course authors only (except the timezone list),
  and it refuses changes for self-paced courses.
- `views_lms.py`: the learner API. Access matches the platform's Dates tab: enrolled
  learners and staff, with masquerade. A session is listed only if its section is in
  the learner's own course outline (`learning_sequences`), so staff-only, hidden and
  restricted sections never leak.
- `signals.py`: drops a section's schedule when the section is deleted
  (`XBLOCK_DELETED`), and all of a course's rows when the course is deleted.

### API endpoints

| Host | Method | Path | Purpose |
| --- | --- | --- | --- |
| Studio | GET | `/api/course-schedule/v1/studio/<course_id>/` | Every section in outline order, with its schedule and warnings, and the course's current timezone. |
| Studio | GET | `/api/course-schedule/v1/studio/timezones/` | The timezone names the Studio API accepts. Any logged-in user. |
| Studio | PUT | `/api/course-schedule/v1/studio/<course_id>/` | Save a batch: `{"timezone"?, "upsert": [...], "remove": [section_id, ...]}`, applied in one transaction. Returns the same payload as GET. |
| LMS | GET | `/api/course-schedule/v1/<course_id>/` | The sessions this learner can see, sorted by date and time. `enabled: false` for self-paced courses. |

### Settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `COURSE_SCHEDULE_ENABLED` | `True` | Master on/off switch (hides Studio controls, disables the learner API). |
| `COURSE_SCHEDULE_DEFAULT_TIMEZONE` | `Asia/Kolkata` | Timezone of a course until its creator picks another. |

### Frontend and styles

- Studio UI: `frontend-app-authoring` `src/course-schedule/`. It is mounted from
  `SectionCard.tsx` (badge next to "Section highlights") and `StatusBar.jsx`
  ("Course schedule" item).
- Dates tab: `frontend-app-learning` `src/course-home/dates-tab/section-schedule/`.
  It is mounted from `timeline/Timeline.jsx` and `timeline/Day.jsx`.
- All CSS: `brand-openedx` `paragon/_authoring.scss` (Studio; the course schedule
  styles are its section 3) and `paragon/_learning.scss` (Dates tab; its section 2).

### Tests

Run them inside the containers with the edx-platform test settings:

```bash
tutor dev exec cms python -m pytest -c cms/pytest.ini --ds=cms.envs.test --rootdir=/openedx/edx-platform -p conftest \
    /mnt/sherab-custom-plugin/course_schedule/tests/test_views_cms.py /mnt/sherab-custom-plugin/course_schedule/tests/test_services.py
tutor dev exec lms python -m pytest -c setup.cfg --ds=lms.envs.test --rootdir=/openedx/edx-platform -p conftest \
    /mnt/sherab-custom-plugin/course_schedule/tests/test_views_lms.py
```

---

## `course_partnerships`

**LMS-only.** Manages partner organizations, their sub-centers, course categories,
and instructor/course-creator profiles — with branding assets — and links them to
courses. Provides partner/center detail pages and a mobile-app JSON API.

### Models

- `Partner` — schools / partner organizations (logo, banner, rich-text description;
  can activate school-admin features).
- `Center` — sub-entities under a `Partner` (logo, banner, description).
- `Category` — course categories, optionally tied to a partner; can be shown on the homepage.
- `EnhancedCourse` — links a `CourseOverview` to its `Partner` / `Center` / `Category`.
- `PartnerOrganizationMapping` — maps a `Partner` to an `Organization`, with a mobile-app
  visibility toggle and optional display-name override.
- `CourseCreator` — instructor profiles (name, title, years of experience, bio, picture).
- `HeroCourse` — courses curated for the homepage hero cards (see `/api/courses/hero/`
  below). `order`: display position. `is_active`: whether it's used as a curated pick.
  `new_until`: shows a "New course" badge until this date, independent of `is_active`.

### API endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/schools/<slug>/` | Partner detail page (centers, categories, courses, creators). |
| GET | `/schools/<partner_slug>/<center_slug>/` | Center detail page. |
| GET | `/api/partners/` | Mobile-app JSON of partner-organization mappings. |
| GET | `/api/partners/homepage/` | All partners, for the homepage schools-and-partners carousel. |
| GET | `/api/categories/homepage/` | Homepage course categories, each with its visible courses. |
| GET | `/api/courses/hero/` | Courses for the homepage hero cards. Personalized: a signed-in caller gets their most recent enrollments, newest first, with curated `HeroCourse` picks filling any leftover slot; a signed-out caller gets the curated picks alone. Each card also reports `is_new` (true while the course's `HeroCourse.new_until` date has not passed) and `is_enrolled` (true for a card sourced from the caller's own enrollments, false for a curated pick they have not joined). |

### Management commands

| Command | Purpose |
| --- | --- |
| `assign_course_partners` | Auto-assign partners to courses lacking one, from org→partner mappings. |
| `check_partner_logos` | Show current storage locations / URLs for logos and creator profiles. |
| `check_storage_settings` | Validate the S3 storage backend config for partner/center/creator assets. |
| `fix_logo_paths` | Correct malformed logo/banner paths in the DB (`--dry-run` supported). |

### Behaviour & settings

- **Signals:** on course publish, creates/updates the `EnhancedCourse` row and
  auto-assigns a partner from the org mapping; on course delete, removes it.
- `settings/common.py` adds `ckeditor` to `INSTALLED_APPS` for the rich-text fields.
- Asset storage backends are read from Django config (`PARTNER_LOGO_BACKEND`,
  `CENTER_LOGO_BACKEND`, `COURSE_CREATOR_STORAGE_BACKEND`), wired via the
  `tutor-contrib-s3` plugin.

---

## `user_extension`

**LMS-only.** Extends user profiles with partner-organization relationships and
streamlines onboarding.

- **Model:** `ExtendedUserProfile` — OneToOne with `User`; associates users with a
  `Partner` and stores admin relationships with timestamps.
- **Signals:**
  - `sync_extended_profile` — create an `ExtendedUserProfile` whenever a `User` is created.
  - `auto_approve_id_verification_on_registration` — create an approved
    `ManualVerification` for every new user, bypassing the standard ID-verification flow.
- **Management command:** `approve_id_verification` — bulk-approve ID verification
  (`--username`, `--all`, `--batch-size`, `--dry-run`).
- No REST endpoints; no settings injected.

---

## `wishlist`

**LMS-only.** A personal per-user course wishlist.

- **Model:** `Wishlist` — one entry per `(user, course)` pair.
- **API endpoints:**

| Method | Path | Purpose |
| --- | --- | --- |
| GET / POST | `/api/wishlist/` | List or add wishlist entries. |
| DELETE | `/api/wishlist/<course_id>/` | Remove a wishlist entry. |
| GET | `/api/wishlist/status/` | Check wishlist status for course IDs. |

- No settings injected; no active signals.

---

## Development

- **Formatting:** Black, line length 120 (see [`pyproject.toml`](pyproject.toml)).
- **Making migrations** (from the edx-platform dir / inside the container):

  ```bash
  ./manage.py cms makemigrations ai_course_creator
  ./manage.py cms migrate ai_course_creator
  ```

- **Dev mount:** with the plugin in `MOUNTS`, code changes are live; the CMS still
  needs a restart to reload a changed `SKILL.md` (it is cached per process).

  ```bash
  tutor dev restart cms
  ```

## License

Proprietary.
