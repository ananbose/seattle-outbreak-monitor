#!/usr/bin/env bash
# Deploy the CDK stack (Lambda, schedule, table, alerts, dashboard) and run the monitor once.
#
# Settings come from environment variables or a local, git-ignored .env file
# (copy .env.example to .env):
#   EMAIL       (required) alert recipient(s), comma-separated
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
export JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1
STACK="${STACK:-seattle-outbreak-monitor}"

cd infrastructure
[[ -d node_modules ]] || npm ci
npx cdk deploy --require-approval never --no-notices

out() { aws cloudformation describe-stacks --stack-name "$STACK" --region "$AWS_REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

echo "Running monitor once..."
aws lambda invoke --function-name "$(out FunctionName)" --region "$AWS_REGION" /dev/stdout
echo
echo "Dashboard: $(out DashboardUrl)"
