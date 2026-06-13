# MikeCast newsletter-signup Lambda

A single Python 3.12 Lambda behind a **Function URL** that powers double-opt-in
email signup for the MikeCast newsletter. It sits between the static signup form
(on `mikecast.io`) and Resend so the Resend API key never reaches the browser.

```
browser form ──POST /signup──▶ Lambda ──Resend.Emails.send──▶ confirmation email
                                                                      │
user clicks confirm link ──GET /confirm?t=…──▶ Lambda ──Resend.Contacts.create──▶ audience
                                                       └──302──▶ mikecast.io/confirmed.html
```

## Routes

| Method | Path        | Behaviour |
|--------|-------------|-----------|
| POST   | `/signup`   | Body `{"email": "..."}`. Validates format, HMAC-signs a 24h token, sends the confirmation email. Always returns `{"status":"pending"}` for a valid address (no enumeration). |
| GET    | `/confirm`  | `?t=<token>`. Verifies the HMAC + expiry, adds the contact to the Resend audience, 302→`/confirmed.html`. Bad/expired token → 302→`/confirmed.html?error=expired`. |

## Configuration

Secrets are resolved once at cold start: an **env var wins if set**, otherwise
the value is read from **SSM Parameter Store** under `SSM_PREFIX` (default
`/mikecast`).

| Name | Source | Purpose |
|------|--------|---------|
| `RESEND_API_KEY` | SSM `/mikecast/RESEND_API_KEY` (SecureString) | Resend full-access key |
| `RESEND_AUDIENCE_ID` | SSM `/mikecast/RESEND_AUDIENCE_ID` (String) | "MikeCast Daily" audience id |
| `SIGNUP_HMAC_SECRET` | SSM `/mikecast/SIGNUP_HMAC_SECRET` (SecureString) | token signing key |
| `RESEND_FROM` | env | sender, e.g. `MikeCast <mike@mikecast.io>` |
| `RESEND_REPLY_TO` | env | reply-to, e.g. `michael.schwimmer@gmail.com` |
| `SITE_BASE_URL` | env | `https://mikecast.io` (redirect target) |
| `CONFIRM_BASE_URL` | env | public base URL of THIS function (Function URL or custom domain). The confirm link is `${CONFIRM_BASE_URL}/confirm?t=…`. |
| `ALLOW_ORIGIN` | env | CORS origin (defaults to `SITE_BASE_URL`) |
| `SSM_PREFIX` | env | parameter prefix (default `/mikecast`) |

## First-time setup

```bash
# 1. Create the three SSM parameters (one-time)
HMAC=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
aws ssm put-parameter --name /mikecast/RESEND_API_KEY     --type SecureString --value "re_..."       --region us-east-1
aws ssm put-parameter --name /mikecast/RESEND_AUDIENCE_ID --type String       --value "<audience-id>" --region us-east-1
aws ssm put-parameter --name /mikecast/SIGNUP_HMAC_SECRET --type SecureString --value "$HMAC"          --region us-east-1

# 2. Create the IAM role (mikecast-newsletter-signup-role) with:
#    - AWSLambdaBasicExecutionRole (logs)
#    - ssm:GetParameters on arn:aws:ssm:us-east-1:<acct>:parameter/mikecast/*

# 3. Create the function (Python 3.12, handler = handler.handler), then env vars:
aws lambda update-function-configuration \
  --function-name mikecast-newsletter-signup \
  --handler handler.handler --runtime python3.12 --timeout 15 \
  --environment "Variables={RESEND_FROM=MikeCast <mike@mikecast.io>,RESEND_REPLY_TO=michael.schwimmer@gmail.com,SITE_BASE_URL=https://mikecast.io}" \
  --region us-east-1

# 4. Create a Function URL with CORS for https://mikecast.io
aws lambda create-function-url-config \
  --function-name mikecast-newsletter-signup --auth-type NONE \
  --cors 'AllowOrigins=["https://mikecast.io"],AllowMethods=["POST","GET"],AllowHeaders=["content-type"]' \
  --region us-east-1
#  -> copy the FunctionUrl. Set it as CONFIRM_BASE_URL (step 3 env) AND paste it
#     into signup.js (MIKECAST_SIGNUP_ENDPOINT) on the site.

# 5. Ship the code
./deploy.sh
```

## Local smoke test

```bash
RESEND_API_KEY=re_... RESEND_AUDIENCE_ID=... SIGNUP_HMAC_SECRET=dev \
CONFIRM_BASE_URL=http://localhost python3 - <<'PY'
import handler, json
print(handler.handler({"requestContext":{"http":{"method":"POST","path":"/signup"}},
                       "rawPath":"/signup","body":json.dumps({"email":"you+test@gmail.com"})}, None))
PY
```
