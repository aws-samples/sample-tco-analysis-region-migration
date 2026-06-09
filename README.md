# Introduction

This repo contains a tool that can be used for analyzing a destination region as a potential candidate for hosting
existing workloads by matching current usage with the 
[AWS Pricing API](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/price-changes.html) information in the destination region.

This tool provides the following features:

* Matches [SKUs](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/price-changes.html#price-list-overview)
  in use to those available in the target region, with a per-service level of detail.
* Provides a list of SKUs that cannot be mapped to the target region (maybe due to inter-region feature mismatch). 
  You can annotate these SKUs to link them to components in your own applications to understand where you need to act.
* For the SKUs that can be mapped directly to the target region, the tool provider a cost ratio between regions. 
  That way you can get an accurate idea of how much would it cost to run the infrastructure in the destination 
  region easily.
* Support for customer-configurable instance replacements, allowing you to specify how instance types should be
  replaced with newer, more efficient alternatives when migrating to a new region.
* Spot instance support for Amazon EC2.
* Support for the AWS commercial Regions and the AWS European Sovereign Cloud (EUSC).

The target is to give you enough information to do an initial target region analysis on both feature mismatch
and cost differences in minutes.

**Please, note that this is a sample implementation, review the output of this tool in detail and critically 
as the code in this repo is not production ready.**

- [Introduction](#introduction)
- [Requirements](#requirements)
- [Limitations](#limitations)
- [Usage](#usage)
  - [Input file](#input-file)
  - [Output Report](#output-report)
  - [Instance replacement profile](#instance-replacement-profile)
  - [SKU annotations](#sku-annotations)
    - [How it works](#how-it-works)
  - [AWS European Sovereign Cloud Support](#aws-european-sovereign-cloud-support)
- [Interpreting the results](#interpreting-the-results)
- [Appendix](#appendix)
  - [Extracting the CUR data with Athena](#extracting-the-cur-data-with-athena)
    - [Step 1: Configure AWS Data Export (first time only)](#step-1-configure-aws-data-export-first-time-only)
    - [Step 2: Verify the data is ready](#step-2-verify-the-data-is-ready)
    - [Step 3: Run the query](#step-3-run-the-query)
    - [Step 4: Export results](#step-4-export-results)

# Requirements

This code has been tested with Python 3.14 in macOS 26.4, but should work fine in other OSes, too, provided that
the following dependencies are met:

* Python 3.14
* The requirements in [`requirements.txt`](requirements.txt) (`pip install -r requirements.txt`)
* SDK access to the AWS Pricing API.
* For EUSC (`eusc-de-east-1`) regions: a separate AWS profile with access to the EUSC Pricing API, specified via the
  `EUSC_PROFILE_NAME` environment variable (see [AWS European Sovereign Cloud Support](#aws-european-sovereign-cloud-support) below).

# Limitations

The code aims to be useful for assessing the services and features available in a target region for a particular 
workload, but it has limitations and you should exercise your own judgment when reviewing its results:

* It only considers features that have a SKU (typically billed features). There are many AWS features & services 
  which are not billed and are therefor not visible with this tool. Some examples include AWS Identity and 
  Access Management (IAM), parts of Amazon Bedrock or Amazon ECS...
* The tool does not yet consider Savings Plans.
* Some user-facing features are billed separately from core AWS features (specific third party models in 
  Amazon Bedrock are billed through the marketplace, so specific FM availability is not detected).
* SKUs are only considered if the originating region is the one specified when running the script. In some cases,
  this might not be enough. For example, consider the case where your stack requires data that must be copied cross-AZ
  from a third region to the destination region but that particular feature is not available in the destination region.
* SKUs are unique in AWS, meaning that the same concept for the same service will map to different SKUs in
  different AWS regions, thus the need to match them based on other fields.
* The tool maps data transfer costs to the AWS Data Transfer service, although in CUR data they will be assigned to
  the service which originated them; this might cause discrepancies between your source CUR data and the mapped data
  for this concept.

# Usage

The code can be run as follows:

```bash
# Basic usage between a source & dest regions
python main.py analyze --cur-data=${INPUT_FILE_PATH} ${SRC_REGION} ${DEST_REGION} ${OUTPUT_REPORT_PATH}
# Apply an instance replacement profile
python main.py analyze --cur-data=${INPUT_FILE_PATH} ${SRC_REGION} ${DEST_REGION} ${OUTPUT_REPORT_PATH} --instance-replacements=${INSTANCE_REPLACEMENT_PROFILE_PATH}
```

## Input file

One of the easiest ways to obtain the input file in the required format is with 
[AWS Cost Usage Reports](https://docs.aws.amazon.com/cur/latest/userguide/what-is-cur.html) (CUR). CUR provides detailed
billing information at the SKU level and can be easily queried with Amazon Athena to obtain the kind of input required
by this tool, for example using a query like [the one below](#extracting-the-cur-data-with-athena).

## Output Report

Once the code has run, it will create an Excel report that will contain the following sheets:

* A copy of the input data.
* A list of the instance replacements
* The list of services in use in the source region.
* The list of services that are not available in the destination region.
* The list of SKUs that directly map to existing SKUs in the target region. This is, effectively, the list of items
  available in the target region.
* One sheet per service that has SKUs in use in the source region that cannot be directly mapped to SKUs in the
  target region.

Once you have this report, you can analyze the data to confront it with your knowledge of the workload
to determine what changes need to be done (if any) in order to migrate the workload to the target region.

## Instance replacement profile

An instance replacement profile can optionally be provided. Using this functionality you can provide per-service
instance replacements. This functionality serves two main purposes:
* Simplifying the feature gap/TCO analysis by increasing the directly mappable features in the report (ie: replacing 
  `m5.xlarge` → `m6i.xlarge` if the former is not available in the target region).
* Estimate TCO for modernization scenarios (ie: moving to Graviton).

The code contains a sample instance replacement profile
[`instance_replacements_graviton.json`](data/instance_replacements_graviton.json) that you can use as a base.
The file should contain a dictionary where the keys are the service codes, as defined in the pricing API
and the values are literal instance replacements, per instance:

```json
{
  "AmazonEC2": {
    "a1.medium": "t3.micro",
    "c3.4xlarge": "c5.4xlarge",
    "c5a.12xlarge": "c5.12xlarge"
  },
  "AmazonRDS": {
    "db.r4.large": "db.r5.large"
  }
```

Please note that no efficiency factor is applied automatically when doing this as this is use-case (as moving 
to more modern compute architectures will typically yield a performance improvement).

## SKU annotations

The output report includes sheets for each service with unavailable SKUs. These SKUs are raw pricing API entries
that can be interpreted better when linked to your services and features that make use of the SKU 
(e.g. "logging component in App-X", "payment gateway") so that future reports for the same region
automatically include those annotations in an `Annotation` column.

### How it works

1. Run the tool to generate an Excel report for a target region.
2. Open the `<ServiceCode> unav SKUs` sheets and add an `Annotation` column describing what
   each unavailable SKU is used for in your environment.
3. Run the `update-annotations` command to extract those annotations:

```bash
python main.py update-annotations ${REPORT_PATH} ${REGION}
```

This reads all `*unav SKUs` sheets from the Excel report, extracts rows that have an `Annotation` value, and saves
them as CSV files under `data/sku_annotations/${REGION}/${ServiceCode}.csv`. If a CSV already exists for a service,
new entries are merged and duplicates are resolved by keeping the most recent annotation.

On subsequent runs of the `analyze` command for the same target region, the tool will automatically pick up these
CSVs and populate the `Annotation` column in the unavailable SKU sheets, saving you from having to re-annotate
the same SKUs.

## AWS European Sovereign Cloud Support

The tool supports the AWS European Sovereign Cloud (`eusc-de-east-1`) region. Since it has its own isolated Pricing API
endpoint, you need to provide a separate AWS profile with access to it via the `EUSC_PROFILE_NAME` environment variable:

```bash
export EUSC_PROFILE_NAME=my-eusc-profile
python main.py analyze eu-west-1 eusc-de-east-1 output_report.xlsx --cur-data=cur_data.csv
```

Note that EUSC pricing is retrieved in EUR rather than USD. The tool will retrieve the 
exchange rate automatically if needed, but please beware that the tool's conversion rate is only
considered for comparison purposes.

# Interpreting the results

Once processed, you might find one or more of the following situations:

* The Excel report will not contain inter-region traffic SKUs between the source and destination regions. The
  `Services` tab will correctly show this traffic as unmappable cost but you will not see an entry in the
  `AWSDataTransfer` sheet for inter-region traffic.
* Instance types are not available for a particular service in the destination region. For example, AWS regions launched 
  recently will typically not support 5th gen `x86_64` instances. This is expected as AWS adopts new instance types and
  typically requires you to add the legacy instance replacements to an
  [instance replacement profile](#instance-replacement-profile).
* SKUs that are somehow affected by physical location. Services like DirectConnect have PoP-specific SKUs. That kind
  of locality does not map between regions; you can usually treat that as a false positive.
* Unavailable services/features/pricing methods:
  - You should not expect services in Maintenance, Sunset or Full Shutdown in the destination regions.
    See [AWS Lifecycle Changes](https://docs.aws.amazon.com/general/latest/gr/service-lifecycle.html) for details.
  - [AWS Capabilities by Region](https://builder.aws.com/build/capabilities) provides visibility into feature and 
    service availability and roadmap per service and region.
    + In some cases you might want to rearchitect around the missing services/features.
  - Sometimes features are presented with different purchase options in different regions.
  - Some services are not available in target region as they are global services. Services like 
    Amazon CloudFront or Amazon Route53 are global services with no (or little) regional service.
    This is usually fine and requires no/little action on your side.
* False positives. Some SKUs are not homogeneously mapped across regions and this might not be properly noticed by this
  tool. You will most likely also get entries for cross-region traffic from the source region to the destination region.
  This is expected, as it would translate into intra-region traffic, which is not charged in the same way.
  (for example, `EU-EUS2-AWS-In-ABytes` in `AWSDataTransfer` is the inter-region traffic between the Europe (Ireland) 
  and Europe (Spain) AWS regions, which won't have a corresponding pricing entry in Europe (Spain) as there is no
  `EUS2-EUS2-AWS-In-ABytes` concept).

# Appendix

## Extracting the CUR data with Athena

### Step 1: Configure AWS Data Export (first time only)

See the 
[AWS Data Export documentation](https://docs.aws.amazon.com/cur/latest/userguide/dataexports-create-standard.html) 
for more details.

1. Sign in to the AWS Console with the management account.
2. Go to **Billing → Cost and Usage Analysis → Data Exports**.
3. Click **Create export** and configure:
   - Export type: Standard data export
   - Table name: CUR 2.0
   - Time granularity: Hourly or Daily
   - Format: Parquet (recommended for Athena)
4. Configure the destination S3 bucket.
5. Click **Create export**.
6. Wait up to 24 hours for the first data to be generated.

### Step 2: Verify the data is ready

1. In **Data Exports**, verify that "Data last refreshed" shows a date.
2. Go to **Amazon Athena** in the console.
3. Run `SHOW DATABASES;` and look for a database matching your export name (e.g. `cur_data_export_xxxxx`).

### Step 3: Run the query

1. In Athena, select the CUR database from the dropdown.
2. Run `SHOW TABLES;` to see the available tables.
3. Replace `YOUR_TABLE_NAME` in the query below with the actual table name and adjust `year`/`month` as needed:

```sql
SELECT line_item_product_code          AS "Service",
       line_item_line_item_type        AS "Item Type",
       line_item_usage_type            AS "Usage Type",
       line_item_operation             AS "Operation",
       line_item_line_item_description AS "Description",
       line_item_unblended_rate        AS "Unblended Rate",
       pricing_unit                    AS "Pricing Unit",
       pricing_rate_code               AS "Rate Code",
       product_sku                     AS "SKU",
       product_equivalentondemandsku   AS "Equivalent On-Demand SKU",
       SUM(
               CASE
                 WHEN (line_item_line_item_type = 'SavingsPlanCoveredUsage')
                   THEN savings_plan_savings_plan_effective_cost
                 WHEN (line_item_line_item_type = 'DiscountedUsage')
                   THEN reservation_effective_cost
                 ELSE line_item_unblended_cost
                 END
       )                               AS "Effective Cost"

FROM YOUR_TABLE_NAME

WHERE year = '2026'
  AND month = '03'
  AND line_item_line_item_type IN ('Usage' , 'ReservedUsage' , 'SavingsPlanCoveredUsage')

GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10
```

### Step 4: Export results

Once the query completes, click **Download results** (CSV) in Athena. The resulting CSV file can be used 
directly as the `--cur-data` input for this tool.
