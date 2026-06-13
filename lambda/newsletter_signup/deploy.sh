#!/usr/bin/env bash
# Package and deploy the MikeCast newsletter-signup Lambda.
#
#   ./deploy.sh            # update function code (function must already exist)
#
# Prereqs: AWS CLI configured; the function FUNCTION_NAME exists with an IAM
# role that allows ssm:GetParameters on /mikecast/RESEND_* + SIGNUP_HMAC_SECRET
# and logs:CreateLogStream / logs:PutLogEvents. See README.md for first-time setup.
set -euo pipefail

FUNCTION_NAME="${FUNCTION_NAME:-mikecast-newsletter-signup}"
REGION="${AWS_REGION:-us-east-1}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="$HERE/build"
ZIP="$HERE/function.zip"

echo "Building $FUNCTION_NAME …"
rm -rf "$BUILD" "$ZIP"
mkdir -p "$BUILD"
pip install --quiet --target "$BUILD" -r "$HERE/requirements.txt"
cp "$HERE/handler.py" "$BUILD/"

( cd "$BUILD" && zip -qr "$ZIP" . )

echo "Deploying to $FUNCTION_NAME ($REGION) …"
aws lambda update-function-code \
  --function-name "$FUNCTION_NAME" \
  --zip-file "fileb://$ZIP" \
  --region "$REGION" \
  --no-cli-pager

echo "Done. Remember to (re)configure env vars / Function URL if they changed (see README.md)."
