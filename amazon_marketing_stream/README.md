# Amazon Marketing Stream Resources

📢 **Amazon Marketing Stream V2 is here.** Hourly Sponsored Ads and DSP performance in a single dataset — around 400 metrics across 59 dimensions — pushed straight to your own S3 bucket as Parquet, with 70 days of history delivered automatically when you subscribe. It shares attribution logic with the Ads Console and the Unified Reporting API, so your numbers reconcile. Stream V2 is the preferred reporting surface for Amazon Ads.

👉 [Deploy the Stream V2 S3 CloudFormation template](https://github.com/amzn/ads-advanced-tools-docs/blob/main/amazon_marketing_stream/StreamV2_S3_CF_Template.yaml) to get started.

---

⚠️ **The resources below are for Stream V1 and are now outdated.** Please migrate to Stream V2 using the link above.

This folder contains developer resources related to Amazon Marketing Stream V1, including:

- CloudFormation template to create SQS queues, roles, and policies — [Instructions](https://advertising.amazon.com/API/docs/en-us/amazon-marketing-stream/cloud-formation)
- CloudFormation template to create Firehose streams, roles, and policies — [Instructions](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-stream/onboarding/firehose/get-started)
- CloudFormation template to create CloudWatch alarms and dashboards for SQS destinations — [Instructions](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-stream/best-practices/sqs-cloudwatch)
