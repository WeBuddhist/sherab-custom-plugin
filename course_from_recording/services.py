"""
Everything course_from_recording sends outside Open edX: S3 links and the Redis queue.

Kept in one module so the views stay plain and the tests can mock ``s3_client``
and ``redis_client``.
"""

import boto3
import redis
from botocore.config import Config
from botocore.exceptions import ClientError
from django.conf import settings

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


def job_folder(job):
    """'studio-tools/course-from-recording/<job id>': every file of the job lives under it."""
    prefix = tool_settings().get("PREFIX", "studio-tools").strip("/")
    return f"{prefix}/course-from-recording/{job.id}"


def original_key(job):
    return f"{job_folder(job)}/original{job.extension}"


def s3_client():
    """Studio's own S3 settings and key (from the tutor-contrib-s3 plugin)."""
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


def download_urls(job):
    """Short-lived links to the service's outputs, e.g. {"transcript": "https://..."}."""
    if not job.results:
        return {}
    client = s3_client()
    bucket = tool_settings()["BUCKET"]
    folder = job_folder(job)
    return {
        name: client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": f"{folder}/{path}"}, ExpiresIn=DOWNLOAD_URL_SECONDS
        )
        for name, path in job.results.items()
    }


def enqueue(job):
    """Put the job on the studio tool service's queue. Raises redis.RedisError if Redis is down."""
    redis_client().lpush(tool_settings()["QUEUE"], f"{TOOL}:{job.id}")
