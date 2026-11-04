#!/usr/bin/env bash
# Build + deploy the stack, upload the dashboard page, and run the monitor once.
#
# Settings come from environment variables or a local, git-ignored .env file
# (copy .env.example to .env):
#   EMAIL       (required) address that receives alerts
#   DOMAIN      (optional) custom dashboard hostname, e.g. outbreak.example.com
#   CERT_ARN    (required with DOMAIN) ACM certificate in us-east-1 for DOMAIN
#   AWS_PROFILE / AWS_REGION (optional)
set -euo pipefail
cd "$(dirname "$0")"

if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

export AWS_REGION="${AWS_REGION:-us-west-2}"
[[ -n "${AWS_PROFILE:-}" ]] && export AWS_PROFILE
STACK="${STACK:-seattle-outbreak-monitor}"
EMAIL="${EMAIL:?Set EMAIL (alert recipient) in .env or the environment}"

PARAMS=("NotificationEmail=$EMAIL")
if [[ -n "${DOMAIN:-}" ]]; then
  PARAMS+=("DomainName=$DOMAIN" "CertificateArn=${CERT_ARN:?CERT_ARN required with DOMAIN}")
fi

sam build
sam deploy --stack-name "$STACK" --region "$AWS_REGION" --resolve-s3 --capabilities CAPABILITY_IAM \
  --no-confirm-changeset --no-fail-on-empty-changeset --parameter-overrides "${PARAMS[@]}"

out() { aws cloudformation describe-stacks --stack-name "$STACK" --region "$AWS_REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

aws s3 cp web/index.html "s3://$(out DashboardBucket)/index.html" \
  --content-type "text/html; charset=utf-8" --cache-control "max-age=300"

echo "Running monitor once..."
aws lambda invoke --function-name "$(out FunctionName)" --region "$AWS_REGION" /dev/stdout
echo
echo "Dashboard: $(out DashboardUrl)"
