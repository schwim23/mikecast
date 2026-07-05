# MikeCast — Social Distribution Setup (X + Instagram)

This runbook covers the **manual, one-time** setup (SOC-8) needed to turn on the
daily auto-posts to X (Twitter) and Instagram. All the code (`mc_social.py`,
`crew/distribution_crew.py`, `mc_dist_state.py`, pipeline Step 11) ships and runs
already — each channel simply **skips gracefully** with a log line until its
credentials exist. Once the SSM params below are in place and the task def is
updated, posting turns on automatically at the next 6:30 AM ET run.

Everything degrades safely: a missing/expired token is a logged warning, never a
pipeline failure.

---

## 1. X (Twitter) — API v2, OAuth 1.0a user context

1. Create/sign in to an X developer account: https://developer.x.com → **Projects & Apps**.
2. Create a **Project** and an **App** inside it (Free tier is enough to *post* tweets).
3. In the App's **Settings → User authentication settings**, enable OAuth 1.0a with
   **App permissions = Read and write**. (This is the common trip-up: read+write must be
   set *before* you generate the access token.)
4. In **Keys and tokens**, generate:
   - **API Key** and **API Secret** (a.k.a. consumer key/secret)
   - **Access Token** and **Access Token Secret** — generate these *after* setting
     Read and write, so they carry write scope.

You now have four values: `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`.

---

## 2. Instagram — Graph API (Business account)

1. Convert the MikeCast Instagram account to a **Business** (or Creator) account.
2. Link it to a **Facebook Page** you control.
3. At https://developers.facebook.com create a **Meta app** (Business type) and add the
   **Instagram Graph API** product.
4. Get an access token. **Preferred: a Business-Portfolio system-user token** (does not
   expire) with scopes `instagram_basic`, `instagram_content_publish`, `pages_read_engagement`.
   *Fallback:* a long-lived user token (60-day expiry) — see the refresh runbook in §5.
5. Find the Instagram Business account id:
   ```
   GET https://graph.facebook.com/v21.0/me/accounts?access_token=TOKEN
   # → note the page id, then:
   GET https://graph.facebook.com/v21.0/{PAGE_ID}?fields=instagram_business_account&access_token=TOKEN
   # → instagram_business_account.id  ==  IG_USER_ID
   ```

You now have `META_ACCESS_TOKEN` and `IG_USER_ID`.

> First live test: while the Meta app is still in **Development mode**, publishing works
> to accounts that hold a role on the app (admin/dev/tester). You can validate end-to-end
> before submitting for App Review.

---

## 3. Store the credentials in SSM (us-east-1)

```bash
aws ssm put-parameter --region us-east-1 --type SecureString --name /mikecast/X_API_KEY              --value 'xxxx'
aws ssm put-parameter --region us-east-1 --type SecureString --name /mikecast/X_API_SECRET           --value 'xxxx'
aws ssm put-parameter --region us-east-1 --type SecureString --name /mikecast/X_ACCESS_TOKEN         --value 'xxxx'
aws ssm put-parameter --region us-east-1 --type SecureString --name /mikecast/X_ACCESS_TOKEN_SECRET  --value 'xxxx'
aws ssm put-parameter --region us-east-1 --type SecureString --name /mikecast/META_ACCESS_TOKEN      --value 'xxxx'
aws ssm put-parameter --region us-east-1 --type String       --name /mikecast/IG_USER_ID             --value '1784xxxxxxxxxxx'
```

Also add the same variables to `~/.profile` for local runs:

```bash
export X_API_KEY=... X_API_SECRET=... X_ACCESS_TOKEN=... X_ACCESS_TOKEN_SECRET=...
export META_ACCESS_TOKEN=... IG_USER_ID=...
```

---

## 4. Wire the params into the ECS task definition

Register a new task-def revision that adds these to `containerDefinitions[0].secrets`
(same procedure used for the Resend params in revision 28). The exec role
`mikecast-ecs-execution-role` already has SSM read = `*`, so no IAM change is needed.
The GitHub Actions deploy fetches the latest task-def at deploy time, so the secrets
persist across future image deploys.

Each entry maps an env var name to its SSM parameter ARN, e.g.:

```json
{ "name": "X_API_KEY", "valueFrom": "arn:aws:ssm:us-east-1:602039469166:parameter/mikecast/X_API_KEY" }
```

Add one such entry for each of the six params above, then point the EventBridge
scheduler `mikecast-daily` at the new revision.

---

## 5. Meta token refresh runbook (only if you used a 60-day user token)

A system-user token does not expire; skip this. For a long-lived **user** token, refresh
before day 60:

```bash
curl -s "https://graph.facebook.com/v21.0/oauth/access_token?grant_type=fb_exchange_token&client_id=APP_ID&client_secret=APP_SECRET&fb_exchange_token=CURRENT_LONG_LIVED_TOKEN"
# → take the new access_token from the response, then:
aws ssm put-parameter --region us-east-1 --type SecureString --overwrite \
  --name /mikecast/META_ACCESS_TOKEN --value 'NEW_TOKEN'
```

An expired token simply degrades to a logged warning — the daily pipeline keeps running.

---

## 6. Verify

Once the SSM params exist and your shell has the env vars:

```bash
# Offline: generate copy + card, post nothing (writes data/MikeCast_card_<date>.png)
source ~/.profile && .venv/bin/python3 mc_social.py --date <old-date> --dry-run

# X live: post + verify the deep link, then delete
.venv/bin/python3 mc_social.py --date <old-date> --only x --force
.venv/bin/python3 mc_social.py --delete-tweet <tweet-id>

# IG live (app in Development mode is fine): post to a role-holding account
.venv/bin/python3 mc_social.py --date <old-date> --only ig --force
# delete manually in the Instagram app if unwanted (no IG delete API)
```

Editing / reposting a past day is handled by `mc_edit.py` (see its `--help`).
