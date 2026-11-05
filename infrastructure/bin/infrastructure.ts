#!/usr/bin/env node
import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { OutbreakMonitorStack } from '../lib/outbreak-monitor-stack';

// Personal settings live in ../.env (git-ignored); environment variables win.
const envFile = path.join(__dirname, '..', '..', '.env');
if (fs.existsSync(envFile)) {
  for (const line of fs.readFileSync(envFile, 'utf8').split('\n')) {
    const m = /^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$/.exec(line);
    if (m && process.env[m[1]] === undefined) process.env[m[1]] = m[2].replace(/^["']|["']$/g, '');
  }
}

// EMAIL may list several recipients, comma-separated.
const notificationEmails = (process.env.EMAIL || '').split(',').map(e => e.trim()).filter(Boolean);
if (notificationEmails.length === 0) {
  throw new Error('Set EMAIL (alert recipients, comma-separated) in .env or the environment. See .env.example.');
}
if (process.env.DOMAIN && !process.env.CERT_ARN) {
  throw new Error('CERT_ARN (ACM certificate in us-east-1) is required when DOMAIN is set.');
}

const app = new cdk.App();
new OutbreakMonitorStack(app, 'OutbreakMonitor', {
  stackName: process.env.STACK || 'seattle-outbreak-monitor',
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.AWS_REGION || process.env.CDK_DEFAULT_REGION || 'us-west-2',
  },
  notificationEmails,
  domainName: process.env.DOMAIN || undefined,
  certificateArn: process.env.CERT_ARN || undefined,
  scheduleExpression: process.env.SCHEDULE || 'cron(0 6-22/2 * * ? *)',
  maxAgeDays: Number(process.env.MAX_AGE_DAYS || 7),
});
