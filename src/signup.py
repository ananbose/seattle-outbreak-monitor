"""Email alert sign-up, served behind CloudFront at POST /api/subscribe.

Subscribes the address to the alert SNS topic. SNS emails a confirmation link and
only delivers alerts after it is clicked (double opt-in), and every alert carries an
unsubscribe link. Rate limits (kept in the SeenTable with a TTL) stop the form being
used to flood someone with confirmation emails:
  - one request per address per day
  - 5 requests per client IP per hour (best effort; IP from X-Forwarded-For)
  - 40 requests per hour in total
"""
import base64
import hashlib
import json
import os
import re
import time

import boto3

TOPIC_ARN = os.environ["TOPIC_ARN"]
TABLE = boto3.resource("dynamodb").Table(os.environ["SEEN_TABLE"])
SNS = boto3.client("sns")

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")
PER_IP_PER_HOUR = 5
TOTAL_PER_HOUR = 40


def resp(status, body):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body),
    }


def bump(key, limit, ttl_seconds):
    """Count one request under key; return False if the limit is already reached."""
    try:
        TABLE.update_item(
            Key={"id": key},
            UpdateExpression="ADD n :one SET expires_at = if_not_exists(expires_at, :exp)",
            ConditionExpression="attribute_not_exists(n) OR n < :limit",
            ExpressionAttributeValues={":one": 1, ":limit": limit, ":exp": int(time.time()) + ttl_seconds},
        )
        return True
    except TABLE.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def handler(event, context):
    http = event.get("requestContext", {}).get("http", {})
    if http.get("method") != "POST" or event.get("rawPath") != "/api/subscribe":
        return resp(404, {"error": "Not found"})

    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode(errors="replace")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return resp(400, {"error": "Send JSON like {\"email\": \"you@example.com\"}"})
    if not isinstance(body, dict):
        return resp(400, {"error": "Send JSON like {\"email\": \"you@example.com\"}"})

    # Honeypot: a hidden field people never fill in. Pretend success for bots.
    if body.get("website"):
        return resp(200, {"ok": True})

    email = str(body.get("email") or "").strip()
    if len(email) > 254 or not EMAIL_RE.match(email):
        return resp(400, {"error": "That doesn't look like an email address."})

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    ip = (headers.get("x-forwarded-for") or http.get("sourceIp") or "unknown").split(",")[0].strip()
    hour = time.strftime("%Y%m%d%H", time.gmtime())

    if not bump(f"signup:all:{hour}", TOTAL_PER_HOUR, 7200):
        return resp(429, {"error": "Lots of sign-ups right now. Please try again in an hour."})
    if not bump(f"signup:ip:{ip}:{hour}", PER_IP_PER_HOUR, 7200):
        return resp(429, {"error": "Too many sign-ups from your network. Please try again in an hour."})
    digest = hashlib.sha256(email.lower().encode()).hexdigest()
    if not bump(f"signup:email:{digest}", 1, 86400):
        return resp(200, {"ok": True, "already": True})

    SNS.subscribe(TopicArn=TOPIC_ARN, Protocol="email", Endpoint=email)
    return resp(200, {"ok": True})
