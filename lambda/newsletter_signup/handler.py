"""
MikeCast newsletter signup Lambda.

One handler, two routes (AWS Lambda Function URL, payload format 2.0):

  POST /signup    body {"email": "..."} — validate, HMAC-sign a 24h token,
                  send a double-opt-in confirmation email via Resend.
  GET  /confirm?t=…  — verify the token, add the contact to the Resend
                  audience, 302-redirect to the thank-you page.

Secrets are resolved once at cold start (cached in module scope): an env var
wins if set (handy for local testing), otherwise the value is read from SSM
Parameter Store under SSM_PREFIX (default /mikecast).

The handler never reveals whether an address already exists (enumeration
defence): /signup always answers {"status": "pending"} for a well-formed
address regardless of prior state.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TOKEN_TTL_SECONDS = 24 * 3600

# Plain (non-secret) config from env.
RESEND_FROM = os.environ.get("RESEND_FROM", "MikeCast <mike@mikecast.io>")
RESEND_REPLY_TO = os.environ.get("RESEND_REPLY_TO", "michael.schwimmer@gmail.com")
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "https://mikecast.io").rstrip("/")
ALLOW_ORIGIN = os.environ.get("ALLOW_ORIGIN", SITE_BASE_URL)
# Public base URL of THIS function (Function URL or custom domain), used to
# build the confirmation link. Falls back to SITE_BASE_URL only as a guess.
CONFIRM_BASE_URL = os.environ.get("CONFIRM_BASE_URL", "").rstrip("/")


def _load_secrets() -> dict:
    """Resolve secrets at cold start: env override first, then SSM."""
    prefix = os.environ.get("SSM_PREFIX", "/mikecast")
    wanted = {
        "RESEND_API_KEY": f"{prefix}/RESEND_API_KEY",
        "RESEND_AUDIENCE_ID": f"{prefix}/RESEND_AUDIENCE_ID",
        "SIGNUP_HMAC_SECRET": f"{prefix}/SIGNUP_HMAC_SECRET",
    }
    resolved: dict = {}
    to_fetch: dict = {}
    for key, param in wanted.items():
        if os.environ.get(key):
            resolved[key] = os.environ[key]
        else:
            to_fetch[param] = key
    if to_fetch:
        try:
            ssm = boto3.client("ssm")
            resp = ssm.get_parameters(Names=list(to_fetch), WithDecryption=True)
            for p in resp.get("Parameters", []):
                resolved[to_fetch[p["Name"]]] = p["Value"]
            missing = [n for n in to_fetch if to_fetch[n] not in resolved]
            if missing:
                logger.error("Missing SSM parameters: %s", missing)
        except Exception as exc:
            logger.error("Failed to read secrets from SSM: %s", exc)
    return resolved


_SECRETS = _load_secrets()
RESEND_API_KEY = _SECRETS.get("RESEND_API_KEY", "")
RESEND_AUDIENCE_ID = _SECRETS.get("RESEND_AUDIENCE_ID", "")
_HMAC_SECRET = _SECRETS.get("SIGNUP_HMAC_SECRET", "").encode()


# ---------------------------------------------------------------------------
# Token signing — payload(JSON bytes) || sig(32 bytes), urlsafe-b64, unpadded
# ---------------------------------------------------------------------------

def _sign(email: str, exp: int) -> str:
    payload = json.dumps({"email": email, "exp": exp}, separators=(",", ":")).encode()
    sig = hmac.new(_HMAC_SECRET, payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + sig).decode().rstrip("=")


def _verify(token: str) -> str | None:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except Exception:
        return None
    if len(raw) <= 32:
        return None
    payload, sig = raw[:-32], raw[-32:]
    expected = hmac.new(_HMAC_SECRET, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(payload)
    except Exception:
        return None
    if int(data.get("exp", 0)) < int(time.time()):
        return None
    return data.get("email")


# ---------------------------------------------------------------------------
# HTTP response helpers
# ---------------------------------------------------------------------------

def _cors_headers() -> dict:
    return {
        "Access-Control-Allow-Origin": ALLOW_ORIGIN,
        "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


def _json(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", **_cors_headers()},
        "body": json.dumps(body),
    }


def _redirect(location: str) -> dict:
    return {"statusCode": 302, "headers": {"Location": location, **_cors_headers()}, "body": ""}


def _confirm_email_html(confirm_url: str) -> str:
    return f"""\
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;max-width:520px;margin:0 auto;color:#1f2328;">
  <h2 style="margin:0 0 12px;">Confirm your MikeCast subscription</h2>
  <p style="line-height:1.6;color:#444;">Tap the button below to confirm and start receiving the daily MikeCast briefing each morning.</p>
  <p style="margin:24px 0;">
    <a href="{confirm_url}" style="display:inline-block;padding:12px 22px;border-radius:8px;background:#f97316;color:#fff;text-decoration:none;font-weight:700;">Confirm subscription</a>
  </p>
  <p style="line-height:1.6;color:#8b949e;font-size:13px;">This link expires in 24 hours. If you didn't request this, you can safely ignore this email.</p>
</div>"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _do_signup(email: str) -> dict:
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        return _json(400, {"status": "invalid"})
    if not (RESEND_API_KEY and _HMAC_SECRET):
        logger.error("Signup attempted but Resend/HMAC secret not configured.")
        return _json(500, {"status": "error"})

    confirm_base = CONFIRM_BASE_URL or SITE_BASE_URL
    token = _sign(email, int(time.time()) + TOKEN_TTL_SECONDS)
    confirm_url = f"{confirm_base}/confirm?t={token}"

    try:
        import resend
        resend.api_key = RESEND_API_KEY
        resend.Emails.send({
            "from": RESEND_FROM,
            "to": [email],
            "reply_to": RESEND_REPLY_TO,
            "subject": "Confirm your MikeCast subscription",
            "html": _confirm_email_html(confirm_url),
        })
    except Exception as exc:
        logger.error("Confirmation email send failed: %s", exc)
        return _json(502, {"status": "error"})

    # Never reveal whether the address already existed.
    return _json(200, {"status": "pending"})


def _do_confirm(token: str) -> dict:
    email = _verify(token)
    if not email:
        return _redirect(f"{SITE_BASE_URL}/confirmed.html?error=expired")
    try:
        import resend
        resend.api_key = RESEND_API_KEY
        resend.Contacts.create({
            "email": email,
            "audience_id": RESEND_AUDIENCE_ID,
            "unsubscribed": False,
        })
    except Exception as exc:
        # A duplicate (already-subscribed) lands here too — treat as success so
        # re-clicking a link doesn't show a scary error. Log for visibility.
        logger.warning("Contact create returned an error (continuing): %s", exc)
    return _redirect(f"{SITE_BASE_URL}/confirmed.html")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def handler(event, context):
    http = (event.get("requestContext") or {}).get("http") or {}
    method = (http.get("method") or "GET").upper()
    path = event.get("rawPath") or http.get("path") or "/"

    if method == "OPTIONS":
        return {"statusCode": 204, "headers": _cors_headers(), "body": ""}

    if method == "POST" and path.endswith("/signup"):
        raw = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            try:
                raw = base64.b64decode(raw).decode()
            except Exception:
                return _json(400, {"status": "invalid"})
        try:
            email = (json.loads(raw) or {}).get("email", "")
        except Exception:
            return _json(400, {"status": "invalid"})
        return _do_signup(email)

    if method == "GET" and path.endswith("/confirm"):
        token = (event.get("queryStringParameters") or {}).get("t", "")
        return _do_confirm(token)

    return _json(404, {"status": "not_found"})
