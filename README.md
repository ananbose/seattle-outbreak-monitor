# Seattle Outbreak Watch

A scheduled AWS Lambda that checks public-health sources for disease outbreaks around Seattle, emails new findings, and publishes a dashboard. It's tuned for a household with an infant: measles, whooping cough, RSV, meningitis and infant-formula contamination are flagged as **baby-relevant**. Those items are matched anywhere in Washington and listed first.

## Sources

| Source | How |
|---|---|
| King County measles page (exposure sites) | page change detection, structured exposure table |
| WA DOH measles case page (cases by county) | page change detection |
| Public Health – Seattle & King County foodborne outbreaks | table scrape |
| Public Health Insider (PHSKC blog), Snohomish County Health, WA DOH | RSS |
| CDC US outbreak investigations | RSS + syndication API (kept only if Washington is affected) |
| FDA food recalls (infant products, contamination only) | RSS, falling back to openFDA (fda.gov blocks AWS IPs) |
| Google News | targeted queries, local outlets |

## Architecture

EventBridge Scheduler (every 2h, 6am–10pm Pacific) → Lambda (`src/app.py`, stdlib + boto3)
- DynamoDB: already-seen items and page snapshots
- SNS: email alerts (subject says `MEASLES` when relevant)
- S3 + CloudFront: dashboard (`web/index.html` reads `data.json`, which the Lambda writes)

## Deploy

```bash
cp .env.example .env    # set EMAIL (and optionally AWS_PROFILE, DOMAIN, CERT_ARN)
./deploy.sh
```

`.env` is git-ignored, so your email, domain and account details stay out of the repo.

`deploy.sh` builds and deploys the SAM stack, uploads the dashboard and runs the monitor once. New email subscribers must click the SNS confirmation link.

## Local dry run

```bash
python3 src/app.py --dry-run --max-age-days 30
# On python.org builds of Python for macOS, add SSL_CERT_FILE=/etc/ssl/cert.pem if you hit certificate errors.
```

Tune matching in the regexes at the top of `src/app.py` (`INFANT_CRITICAL`, `STRONG_DISEASE`, `GEO_NAMES`, ...).
