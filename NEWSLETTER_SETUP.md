# MikeCast Newsletter — full setup runbook

Step-by-step to take the Resend email newsletter (code on branch `newsletter-resend` / PR #18) from "code complete" to live. The code is done; this file is the **manual + AWS** setup.

**What gets created:** a Resend account + verified `mikecast.io` sending domain, an audience, an API key, 3 SSM secrets, a new ECS task-def revision (carries the secrets), a signup Lambda + IAM role + public Function URL, and the live signup form on mikecast.io.

**Division of labor:** Phase 1 only you can do (your Resend login). Phases 2–6 can be run with the AWS admin creds once you hand over 3 values (API key, Audience ID, DNS records). Phases 7–9 are partly yours (postal address, merging the PR).

## Real values (baked into the commands below)

- Account `602039469166` · region `us-east-1`
- Route53 zone `Z0221219351UZNUE93AY5` (`mikecast.io`)
- ECS family `mikecast` (rev 26 at time of writing) · execution role `mikecast-ecs-execution-role` — **already allows `ssm:GetParameters` on `*`, so no IAM change is needed for the ECS secrets**
- Site bucket `mikecast-io-data` (static files served from the **root**) · CloudFront distribution `EFNQM31KQHY56`
- ECS network: subnet `subnet-086fe88cca1a9de84`, security group `sg-0b075c1eea308976b`
- The GH Actions deploy workflow **downloads the latest task-def revision, swaps the image, and re-registers** — so any secrets/env added to the latest revision are inherited by every future deploy automatically.

---

## Phase 1 — Resend (you, in the browser) 🔑

1. Sign up at **https://resend.com** (use `michael.schwimmer@gmail.com`).
2. **Domains → Add Domain →** `mikecast.io`. Resend shows **3–4 DNS records** (an SPF `TXT`, two DKIM `CNAME`s, optionally a DMARC `TXT`). Keep this page open.
3. **Audiences → Create Audience →** name it `MikeCast Daily`. Copy its **Audience ID** (e.g. `78261eca-...`).
4. **API Keys → Create API Key →** Permission **Full access**, name `mikecast-prod`. Copy the key (`re_...`) — shown once.

**Collect three things for the next phases:** the **API key** (`re_…`), the **Audience ID**, and the **DNS records** (name / type / value for each).

---

## Phase 2 — DNS verification in Route53

For each record Resend gave you, UPSERT it into the hosted zone. Example shape (fill in the real values Resend listed):

```bash
aws route53 change-resource-record-sets --hosted-zone-id Z0221219351UZNUE93AY5 --region us-east-1 \
  --change-batch '{"Changes":[
    {"Action":"UPSERT","ResourceRecordSet":{"Name":"send.mikecast.io","Type":"TXT","TTL":300,
      "ResourceRecords":[{"Value":"\"v=spf1 include:amazonses.com ~all\""}]}},
    {"Action":"UPSERT","ResourceRecordSet":{"Name":"resend._domainkey.mikecast.io","Type":"CNAME","TTL":300,
      "ResourceRecords":[{"Value":"<dkim-value-from-resend>"}]}}
  ]}'
```

Then click **Verify** in the Resend dashboard (propagation usually 1–10 min). Confirm:

```bash
dig TXT send.mikecast.io +short
dig CNAME resend._domainkey.mikecast.io +short
```

---

## Phase 3 — SSM secrets

These **must exist before Phase 4** (a task referencing a missing SSM param fails to start).

```bash
# Token-signing secret (generate fresh)
HMAC=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")

aws ssm put-parameter --region us-east-1 --name /mikecast/RESEND_API_KEY \
  --type SecureString --value "re_XXXXXXXX"
aws ssm put-parameter --region us-east-1 --name /mikecast/RESEND_AUDIENCE_ID \
  --type String       --value "<audience-id>"
aws ssm put-parameter --region us-east-1 --name /mikecast/SIGNUP_HMAC_SECRET \
  --type SecureString --value "$HMAC"
```

---

## Phase 4 — Add the secrets to the ECS task definition

Registering one new revision makes every future deploy inherit `RESEND_*` automatically (the workflow re-reads the latest revision). Pulls rev 26, injects the two secrets + `RESEND_FROM` env, registers the next revision:

```bash
aws ecs describe-task-definition --task-definition mikecast --region us-east-1 \
  --query taskDefinition > /tmp/td.json

python3 - <<'PY'
import json
td=json.load(open('/tmp/td.json'))
c=td['containerDefinitions'][0]
acct="602039469166"; ssm=f"arn:aws:ssm:us-east-1:{acct}:parameter/mikecast"
have={s['name'] for s in c.get('secrets',[])}
for n in ("RESEND_API_KEY","RESEND_AUDIENCE_ID"):
    if n not in have: c.setdefault('secrets',[]).append({"name":n,"valueFrom":f"{ssm}/{n}"})
env={e['name'] for e in c.get('environment',[])}
if "RESEND_FROM" not in env:
    c.setdefault('environment',[]).append({"name":"RESEND_FROM","value":"MikeCast <mike@mikecast.io>"})
for k in ("taskDefinitionArn","revision","status","requiresAttributes","compatibilities","registeredAt","registeredBy"):
    td.pop(k,None)
json.dump(td, open('/tmp/td-new.json','w'))
PY

aws ecs register-task-definition --region us-east-1 --cli-input-json file:///tmp/td-new.json \
  --query 'taskDefinition.revision'
```

(The EventBridge scheduler is re-pointed at the newest revision on every GH Actions deploy, so no manual scheduler update is required — it'll pick this up on the next deploy. To use it immediately for a manual `run-task`, just reference `mikecast` and it uses the latest active revision.)

---

## Phase 5 — Create the Lambda + IAM role + Function URL

```bash
ACCT=602039469166

# 5a. IAM role (trust Lambda; logs + SSM read for the 3 params)
aws iam create-role --role-name mikecast-newsletter-signup-role \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
aws iam attach-role-policy --role-name mikecast-newsletter-signup-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam put-role-policy --role-name mikecast-newsletter-signup-role --policy-name ssm-read \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"ssm:GetParameters\",\"kms:Decrypt\"],\"Resource\":[\"arn:aws:ssm:us-east-1:$ACCT:parameter/mikecast/*\",\"arn:aws:kms:us-east-1:$ACCT:key/*\"]}]}"

# 5b. Package + create the function (from the repo)
cd ~/mikecast/lambda/newsletter_signup && ./deploy.sh   # builds function.zip; warns the fn doesn't exist yet — fine
aws lambda create-function --region us-east-1 \
  --function-name mikecast-newsletter-signup \
  --runtime python3.12 --handler handler.handler --timeout 15 \
  --role arn:aws:iam::$ACCT:role/mikecast-newsletter-signup-role \
  --zip-file fileb://function.zip \
  --environment "Variables={RESEND_FROM=MikeCast <mike@mikecast.io>,RESEND_REPLY_TO=michael.schwimmer@gmail.com,SITE_BASE_URL=https://mikecast.io}"

# 5c. Public Function URL with CORS for the site
aws lambda create-function-url-config --region us-east-1 \
  --function-name mikecast-newsletter-signup --auth-type NONE \
  --cors 'AllowOrigins=["https://mikecast.io"],AllowMethods=["POST","GET"],AllowHeaders=["content-type"]'
aws lambda add-permission --region us-east-1 \
  --function-name mikecast-newsletter-signup --statement-id public-url \
  --action lambda:InvokeFunctionUrl --principal "*" --function-url-auth-type NONE
# -> copy the returned FunctionUrl, e.g. https://abc123.lambda-url.us-east-1.on.aws/
```

> The Lambda is **not** in a VPC, so it has default internet egress to reach the Resend API — don't attach it to the VPC.

---

## Phase 6 — Tell the Lambda its own URL

The confirm link is built from `CONFIRM_BASE_URL`; set it to the Function URL (no trailing slash):

```bash
aws lambda update-function-configuration --region us-east-1 \
  --function-name mikecast-newsletter-signup \
  --environment "Variables={RESEND_FROM=MikeCast <mike@mikecast.io>,RESEND_REPLY_TO=michael.schwimmer@gmail.com,SITE_BASE_URL=https://mikecast.io,CONFIRM_BASE_URL=https://abc123.lambda-url.us-east-1.on.aws}"
```

---

## Phase 7 — Wire the frontend

1. **Postal address (CAN-SPAM):** edit `_NEWSLETTER_FOOTER` in `mc_deliver.py` (branch `newsletter-resend`), replacing the `[POSTAL ADDRESS REQUIRED …]` placeholder with a real mailing address (a PO box is fine).
2. **Signup endpoint:** set `MIKECAST_SIGNUP_ENDPOINT` in `signup.js` to the Function URL (no trailing slash).
3. **Publish static files** to the site bucket root + invalidate CloudFront:

```bash
cd ~/mikecast
aws s3 cp index.html     s3://mikecast-io-data/index.html     --content-type text/html
aws s3 cp subscribe.html s3://mikecast-io-data/subscribe.html --content-type text/html
aws s3 cp confirmed.html s3://mikecast-io-data/confirmed.html --content-type text/html
aws s3 cp signup.js      s3://mikecast-io-data/signup.js      --content-type application/javascript
aws s3 cp style.css      s3://mikecast-io-data/style.css      --content-type text/css
aws cloudfront create-invalidation --distribution-id EFNQM31KQHY56 \
  --paths /index.html /subscribe.html /confirmed.html /signup.js /style.css
```

---

## Phase 8 — Ship the backend

Merge **PR #18** (`newsletter-resend`) once the SSM params exist (Phase 3). GH Actions builds the image and registers a task-def revision that already carries the `RESEND_*` secrets, so the next daily run calls `send_newsletter_broadcast`.

---

## Phase 9 — End-to-end test

```bash
FUNCTION_URL=https://abc123.lambda-url.us-east-1.on.aws   # your real URL

# Signup -> expect {"status":"pending"} and a confirmation email; clicking it lands on confirmed.html
curl -s -X POST "$FUNCTION_URL/signup" -H 'Content-Type: application/json' \
  -d '{"email":"michael.schwimmer+nl1@gmail.com"}'
# After confirming, your address appears in the Resend audience.

# One-shot broadcast (same flags as the daily run):
aws ecs run-task --cluster mikecast --task-definition mikecast --launch-type FARGATE \
  --overrides '{"containerOverrides":[{"name":"mikecast","command":["python","mikecast_briefing.py","--crew","--force"]}]}' \
  --network-configuration 'awsvpcConfiguration={subnets=[subnet-086fe88cca1a9de84],securityGroups=[sg-0b075c1eea308976b],assignPublicIp=ENABLED}' \
  --region us-east-1
# CloudWatch (/ecs/mikecast) should show "Newsletter broadcast sent (id=...)".
```

---

## Quick checklist

- [ ] Phase 1: Resend account, domain added, audience created, API key created
- [ ] Phase 2: DNS records in Route53; Resend shows domain **Verified**
- [ ] Phase 3: 3 SSM params created (`RESEND_API_KEY`, `RESEND_AUDIENCE_ID`, `SIGNUP_HMAC_SECRET`)
- [ ] Phase 4: new ECS task-def revision with the 2 secrets + `RESEND_FROM`
- [ ] Phase 5: IAM role + Lambda + Function URL created
- [ ] Phase 6: `CONFIRM_BASE_URL` set to the Function URL
- [ ] Phase 7: postal address filled, `MIKECAST_SIGNUP_ENDPOINT` set, static files published + invalidated
- [ ] Phase 8: PR #18 merged → deployed
- [ ] Phase 9: signup → confirm → broadcast all verified
