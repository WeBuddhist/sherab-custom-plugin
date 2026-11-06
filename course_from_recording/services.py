"""
Everything course_from_recording sends outside Open edX: S3 links and the Redis queue.

Kept in one module so the views stay plain and the tests can mock ``s3_client``
and ``redis_client``.
"""

import functools
import logging
import os
import re
import unicodedata

import boto3
import redis
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings

log = logging.getLogger(__name__)

TOOL = "course_from_recording"

# Long enough to upload a few hours of audio on a slow connection.
UPLOAD_URL_SECONDS = 6 * 3600
DOWNLOAD_URL_SECONDS = 3600

REQUIRED_SETTINGS = ("REDIS_URL", "QUEUE", "BUCKET", "TOKEN")


class NotConfigured(Exception):
    """STUDIO_TOOL_SERVICE is missing a value, so the tool can't run on this server."""


def tool_settings():
    config = getattr(settings, "STUDIO_TOOL_SERVICE", None) or {}
    missing = [name for name in REQUIRED_SETTINGS if not config.get(name)]
    if missing:
        raise NotConfigured(f"STUDIO_TOOL_SERVICE is missing {', '.join(missing)}")
    return config


def _slug(text, fallback):
    """'Day 1 - Morning.MP3' -> 'day-1-morning': ASCII letters, digits and dashes only, so S3 paths
    need no escaping. Text with nothing left (e.g. an all-Tibetan name) becomes ``fallback``."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:60].strip("-") or fallback


def new_job_folder(job):
    """
    Where a new job's files go, chosen once when the job is first saved (RecordingJob.save):
    '<prefix>/course-from-recording/<course>/<date>_<file name>_<short id>', e.g.
    'studio-tools/course-from-recording/Sherab-tspc12-2026/2026-11-03_audio-tibetan_a101e248'.
    Inside it, one numbered folder per step: 0-original/, 1-voices/, 2-clean/, 3-transcript/.
    """
    prefix = tool_settings().get("PREFIX", "studio-tools").strip("/")
    # 'course-v1:Sherab+tspc12+2026' -> 'Sherab-tspc12-2026'; a '+' in a path is often read as a space.
    course = re.sub(r"[^A-Za-z0-9._-]+", "-", str(job.course_id).split(":", 1)[-1]).strip("-")
    stem = _slug(os.path.splitext(job.original_filename)[0], "recording")
    return f"{prefix}/course-from-recording/{course}/{job.created:%Y-%m-%d}_{stem}_{job.id.hex[:8]}"


def job_folder(job):
    """Every file of the job lives under this folder."""
    if not job.folder:
        # Never guessed: a guessed folder would point the service and start-over at files that aren't there.
        raise ValueError(f"Recording job {job.id} has no folder.")
    return job.folder


def original_key(job):
    """The upload, never changed: '<folder>/0-original/<file name><extension>'."""
    stem = _slug(os.path.splitext(job.original_filename)[0], "recording")
    return f"{job_folder(job)}/0-original/{stem}{job.extension}"


@functools.lru_cache(maxsize=1)
def s3_client():
    """
    Studio's own S3 settings and key (from the tutor-contrib-s3 plugin). Made once per process: the
    job page is polled every few seconds, and a boto3 client is slow to make (and safe to share).
    """
    return boto3.client(
        "s3",
        region_name=getattr(settings, "AWS_S3_REGION_NAME", None) or None,
        aws_access_key_id=getattr(settings, "AWS_ACCESS_KEY_ID", None) or None,
        aws_secret_access_key=getattr(settings, "AWS_SECRET_ACCESS_KEY", None) or None,
        endpoint_url=getattr(settings, "AWS_S3_ENDPOINT_URL", None) or None,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": getattr(settings, "AWS_S3_ADDRESSING_STYLE", None) or "auto"},
        ),
    )


def redis_client():
    return redis.Redis.from_url(tool_settings()["REDIS_URL"])


def upload_url(job):
    """
    A link the browser can PUT the recording to, directly into S3.

    The content type and size are part of the signature, so S3 refuses any other
    file than the one the author declared.
    """
    return s3_client().generate_presigned_url(
        "put_object",
        Params={
            "Bucket": tool_settings()["BUCKET"],
            "Key": original_key(job),
            "ContentType": job.content_type,
            "ContentLength": job.file_size,
        },
        ExpiresIn=UPLOAD_URL_SECONDS,
    )


def uploaded_size(job):
    """Size in bytes of the uploaded recording, or None if it isn't in S3 (yet)."""
    try:
        head = s3_client().head_object(Bucket=tool_settings()["BUCKET"], Key=original_key(job))
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    return head["ContentLength"]


def _file_url(client, job, path):
    """A short-lived link to one file in the job's folder."""
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": tool_settings()["BUCKET"], "Key": f"{job_folder(job)}/{path}"},
        ExpiresIn=DOWNLOAD_URL_SECONDS,
    )


def download_urls(job):
    """Short-lived links to the service's outputs, e.g. {"transcript": "https://..."}."""
    if not job.results:
        return {}
    client = s3_client()
    return {name: _file_url(client, job, path) for name, path in job.results.items()}


def speaker_groups_with_urls(job):
    """The voices the service found, with links to their sample clips for the author to listen to."""
    if not job.speaker_groups:
        return []
    client = s3_client()
    return [
        {**group, "samples": [_file_url(client, job, path) for path in group["samples"]]}
        for group in job.speaker_groups
    ]


def delete_files(job, keep_recording=False):
    """
    Delete the job's files from S3: everything in its folder, or everything but the recording.
    Returns how many files were deleted.
    """
    client, bucket = s3_client(), tool_settings()["BUCKET"]
    keep = original_key(job) if keep_recording else None
    keys = [
        item["Key"]
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{job_folder(job)}/")
        for item in page.get("Contents", [])
        if item["Key"] != keep
    ]
    for start in range(0, len(keys), 1000):  # S3 deletes at most 1000 files per request
        client.delete_objects(
            Bucket=bucket, Delete={"Objects": [{"Key": key} for key in keys[start : start + 1000]], "Quiet": True}
        )
    return len(keys)


def move_recording(from_job, to_job):
    """
    Move a kept recording into a new job's folder, inside S3 (nothing is uploaded again).
    ``copy`` copies a large file in parts, so recordings over 5 GB work too.
    """
    client, bucket = s3_client(), tool_settings()["BUCKET"]
    source, target = original_key(from_job), original_key(to_job)
    client.copy({"Bucket": bucket, "Key": source}, bucket, target)
    try:
        client.delete_object(Bucket=bucket, Key=source)
    except (BotoCoreError, ClientError):
        # Undo the copy, so the recording is in one folder only and the author can simply try again.
        try:
            client.delete_object(Bucket=bucket, Key=target)
        except (BotoCoreError, ClientError):
            log.exception("Could not remove the copy %s of a recording that failed to move", target)
        raise


def enqueue(job):
    """Put the job on the studio tool service's queue. Raises redis.RedisError if Redis is down."""
    redis_client().lpush(tool_settings()["QUEUE"], f"{TOOL}:{job.id}")
