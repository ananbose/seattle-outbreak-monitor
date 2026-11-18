import * as fs from 'fs';
import * as path from 'path';
import { Construct } from 'constructs';
import * as cdk from 'aws-cdk-lib';
import * as budgets from 'aws-cdk-lib/aws-budgets';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cwActions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import * as sns from 'aws-cdk-lib/aws-sns';

export interface OutbreakMonitorStackProps extends cdk.StackProps {
  /** Addresses that receive alert emails (each must confirm its SNS subscription). */
  notificationEmails: string[];
  /** Optional custom dashboard hostname, e.g. outbreak.example.com. */
  domainName?: string;
  /** ACM certificate in us-east-1 covering domainName. */
  certificateArn?: string;
  /** EventBridge Scheduler expression, evaluated in America/Los_Angeles. */
  scheduleExpression: string;
  /** Ignore items published more than this many days ago. */
  maxAgeDays: number;
  /** Secret header value CloudFront sends to the sign-up API; direct calls without it are refused. */
  originSecret?: string;
  /** WAF web ACL (e.g. the one the CloudFront flat-rate plan creates); omitted = none. */
  webAclArn?: string;
  /** Account-wide monthly cost alert (USD), emailed to the first notification address. */
  budgetUsd?: number;
}

/**
 * Pin a construct's CloudFormation logical ID. This stack was first deployed with
 * AWS SAM; keeping the same IDs lets CloudFormation update resources in place
 * instead of replacing them (same CloudFront domain, table, topic subscription).
 */
function pin(c: Construct, logicalId: string) {
  const res = (c instanceof cdk.CfnResource ? c : c.node.defaultChild) as cdk.CfnResource;
  res.overrideLogicalId(logicalId);
}

export class OutbreakMonitorStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: OutbreakMonitorStackProps) {
    super(scope, id, props);

    // ---------------- Alerts ----------------
    // L1 topic with an inline subscription (matches the original stack, so the
    // already-confirmed subscription is kept).
    const topic = new sns.CfnTopic(this, 'AlertTopic', {
      displayName: 'Seattle Outbreak Alerts',
      subscription: props.notificationEmails.map(endpoint => ({ protocol: 'email', endpoint })),
    });
    pin(topic, 'AlertTopic');

    const table = new dynamodb.Table(this, 'SeenTable', {
      partitionKey: { name: 'id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'expires_at',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    pin(table, 'SeenTable');

    // ---------------- Email sign-up API (POST /api/subscribe via CloudFront) ----------------
    const signupFn = new lambda.Function(this, 'SignupFunction', {
      runtime: lambda.Runtime.PYTHON_3_13,
      architecture: lambda.Architecture.ARM_64,
      handler: 'signup.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'src'), {
        exclude: ['__pycache__', '*.pyc'],
      }),
      memorySize: 128,
      timeout: cdk.Duration.seconds(10),
      // Caps cost and load if someone floods the endpoint; extra requests get 429.
      reservedConcurrentExecutions: 5,
      environment: { SEEN_TABLE: table.tableName, TOPIC_ARN: topic.ref, ORIGIN_SECRET: props.originSecret ?? '' },
      logGroup: new logs.LogGroup(this, 'SignupLogs', {
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });
    table.grantReadWriteData(signupFn);
    signupFn.addToRolePolicy(new iam.PolicyStatement({ actions: ['sns:Subscribe'], resources: [topic.ref] }));
    const signupUrl = signupFn.addFunctionUrl({ authType: lambda.FunctionUrlAuthType.NONE });
    const signupHost = cdk.Fn.select(2, cdk.Fn.split('/', signupUrl.url));

    // ---------------- Dashboard: private S3 bucket behind CloudFront ----------------
    const bucket = new s3.Bucket(this, 'DashboardBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    pin(bucket, 'DashboardBucket');

    const oac = new cloudfront.CfnOriginAccessControl(this, 'DashboardOAC', {
      originAccessControlConfig: {
        name: `${this.stackName}-oac`,
        originAccessControlOriginType: 's3',
        signingBehavior: 'always',
        signingProtocol: 'sigv4',
      },
    });
    pin(oac, 'DashboardOAC');

    const useDomain = !!props.domainName;
    const distribution = new cloudfront.CfnDistribution(this, 'DashboardDistribution', {
      distributionConfig: {
        enabled: true,
        comment: 'Seattle Outbreak Watch dashboard',
        defaultRootObject: 'index.html',
        httpVersion: 'http2and3',
        // PriceClass_All + IPv6 match what the CloudFront flat-rate plan sets.
        priceClass: 'PriceClass_All',
        ipv6Enabled: true,
        webAclId: props.webAclArn,
        aliases: useDomain ? [props.domainName!] : undefined,
        viewerCertificate: useDomain
          ? {
              acmCertificateArn: props.certificateArn,
              sslSupportMethod: 'sni-only',
              minimumProtocolVersion: 'TLSv1.2_2021',
            }
          : { cloudFrontDefaultCertificate: true },
        origins: [
          {
            id: 's3',
            domainName: bucket.bucketRegionalDomainName,
            originAccessControlId: oac.attrId,
            s3OriginConfig: { originAccessIdentity: '' },
          },
          {
            id: 'signup',
            domainName: signupHost,
            customOriginConfig: { originProtocolPolicy: 'https-only', originSslProtocols: ['TLSv1.2'] },
            originCustomHeaders: props.originSecret
              ? [{ headerName: 'X-Origin-Verify', headerValue: props.originSecret }]
              : undefined,
          },
        ],
        cacheBehaviors: [
          {
            pathPattern: '/api/*',
            targetOriginId: 'signup',
            viewerProtocolPolicy: 'https-only',
            allowedMethods: ['GET', 'HEAD', 'OPTIONS', 'PUT', 'PATCH', 'POST', 'DELETE'],
            cachedMethods: ['GET', 'HEAD'],
            cachePolicyId: cloudfront.CachePolicy.CACHING_DISABLED.cachePolicyId,
            originRequestPolicyId: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER.originRequestPolicyId,
          },
        ],
        defaultCacheBehavior: {
          targetOriginId: 's3',
          viewerProtocolPolicy: 'redirect-to-https',
          compress: true,
          // Managed "CachingOptimized"; data.json is uploaded with a short max-age.
          cachePolicyId: cloudfront.CachePolicy.CACHING_OPTIMIZED.cachePolicyId,
        },
      },
    });
    pin(distribution, 'DashboardDistribution');

    const bucketPolicy = new s3.CfnBucketPolicy(this, 'DashboardBucketPolicy', {
      bucket: bucket.bucketName,
      policyDocument: {
        Statement: [
          {
            Effect: 'Allow',
            Principal: { Service: 'cloudfront.amazonaws.com' },
            Action: 's3:GetObject',
            Resource: `${bucket.bucketArn}/*`,
            Condition: {
              StringEquals: {
                'AWS:SourceArn': `arn:aws:cloudfront::${this.account}:distribution/${distribution.ref}`,
              },
            },
          },
        ],
      },
    });
    pin(bucketPolicy, 'DashboardBucketPolicy');

    const dashboardUrl = useDomain
      ? `https://${props.domainName}/`
      : `https://${distribution.attrDomainName}/`;

    // Upload the static page. prune=false so data.json / history.json written by
    // the Lambda are never deleted.
    const webDir = path.join(__dirname, '..', '..', 'web');
    new s3deploy.BucketDeployment(this, 'DashboardPage', {
      sources: [
        s3deploy.Source.asset(webDir, { exclude: ['index.html'] }),
        // index.html gets the real site URL (for link-preview tags) filled in at deploy time,
        // so the domain never has to be committed.
        s3deploy.Source.data(
          'index.html',
          fs.readFileSync(path.join(webDir, 'index.html'), 'utf8').split('__SITE_URL__').join(dashboardUrl),
        ),
      ],
      destinationBucket: bucket,
      prune: false,
      cacheControl: [s3deploy.CacheControl.fromString('max-age=300')],
      // Invalidate the CDN copy of the page + preview image on every deploy.
      distribution: cloudfront.Distribution.fromDistributionAttributes(this, 'DashboardDistributionRef', {
        distributionId: distribution.ref,
        domainName: distribution.attrDomainName,
      }),
      distributionPaths: ['/index.html', '/', '/og-image.png'],
    });

    // ---------------- Monitor Lambda ----------------
    const fn = new lambda.Function(this, 'MonitorFunction', {
      runtime: lambda.Runtime.PYTHON_3_13,
      architecture: lambda.Architecture.ARM_64,
      handler: 'app.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'src'), {
        exclude: ['__pycache__', '*.pyc'],
      }),
      memorySize: 256,
      timeout: cdk.Duration.seconds(120),
      environment: {
        SEEN_TABLE: table.tableName,
        TOPIC_ARN: topic.ref,
        MAX_AGE_DAYS: String(props.maxAgeDays),
        DASHBOARD_BUCKET: bucket.bucketName,
        DASHBOARD_URL: dashboardUrl,
      },
    });
    pin(fn, 'MonitorFunction');
    pin(fn.role!, 'MonitorFunctionRole');

    table.grantReadWriteData(fn);
    bucket.grantReadWrite(fn);
    fn.addToRolePolicy(new iam.PolicyStatement({ actions: ['sns:Publish'], resources: [topic.ref] }));

    const logGroup = new logs.LogGroup(this, 'MonitorLogs', {
      // Same Fn::Sub expression the original stack used; any other spelling of the
      // same value makes CloudFormation try to replace the log group.
      logGroupName: cdk.Fn.sub('/aws/lambda/${MonitorFunction}'),
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    pin(logGroup, 'MonitorLogs');

    // ---------------- Schedule ----------------
    const scheduleRole = new iam.Role(this, 'MonitorFunctionScheduleRole', {
      assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com'),
      inlinePolicies: {
        MonitorFunctionScheduleLambdaPolicy: new iam.PolicyDocument({
          statements: [new iam.PolicyStatement({ actions: ['lambda:InvokeFunction'], resources: [fn.functionArn] })],
        }),
      },
    });
    pin(scheduleRole, 'MonitorFunctionScheduleRole');

    const schedule = new scheduler.CfnSchedule(this, 'MonitorFunctionSchedule', {
      name: 'MonitorFunctionSchedule',
      scheduleExpression: props.scheduleExpression,
      scheduleExpressionTimezone: 'America/Los_Angeles',
      flexibleTimeWindow: { mode: 'OFF' },
      target: { arn: fn.functionArn, roleArn: scheduleRole.roleArn },
    });
    pin(schedule, 'MonitorFunctionSchedule');

    // ---------------- Alarm: email if the function itself fails ----------------
    const alarm = new cloudwatch.Alarm(this, 'ErrorAlarm', {
      alarmDescription: 'Outbreak monitor Lambda is erroring',
      metric: fn.metricErrors({ period: cdk.Duration.days(1), statistic: 'Sum' }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    alarm.addAlarmAction(new cwActions.SnsAction(sns.Topic.fromTopicArn(this, 'AlarmTopic', topic.ref)));
    pin(alarm, 'ErrorAlarm');

    // ---------------- Outputs ----------------
    // ---------------- Cost alert ----------------
    // Account-wide: emails if this month's AWS bill passes half the limit, the limit,
    // or is forecast to pass it -- an early warning if traffic or abuse runs up costs.
    if (props.budgetUsd && props.budgetUsd > 0) {
      const subscribers = [{ subscriptionType: 'EMAIL', address: props.notificationEmails[0] }];
      const notify = (notificationType: string, threshold: number) => ({
        notification: { notificationType, comparisonOperator: 'GREATER_THAN', threshold, thresholdType: 'PERCENTAGE' },
        subscribers,
      });
      new budgets.CfnBudget(this, 'MonthlyCostBudget', {
        budget: {
          budgetName: `${this.stackName}-monthly-cost`,
          budgetType: 'COST',
          timeUnit: 'MONTHLY',
          budgetLimit: { amount: props.budgetUsd, unit: 'USD' },
        },
        notificationsWithSubscribers: [notify('ACTUAL', 50), notify('ACTUAL', 100), notify('FORECASTED', 100)],
      });
    }

    new cdk.CfnOutput(this, 'FunctionName', { value: fn.functionName });
    new cdk.CfnOutput(this, 'TopicArn', { value: topic.ref });
    new cdk.CfnOutput(this, 'DashboardBucketName', { value: bucket.bucketName, exportName: undefined });
    new cdk.CfnOutput(this, 'DashboardUrl', { value: dashboardUrl });
    new cdk.CfnOutput(this, 'CloudFrontDomain', {
      value: distribution.attrDomainName,
      description: 'Point a CNAME for your custom domain at this.',
    });
  }
}
