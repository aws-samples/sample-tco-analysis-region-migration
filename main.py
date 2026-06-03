#!/usr/bin/env python3

import sys
import json
import asyncio
import logging
import pandas as pd
from pathlib import Path
from json import JSONDecodeError
from misc.sku_normalization import REGION_CODES
from misc.skus import get_all_products_in_region
from misc.exceptions import InvalidCURDataException
from misc.instance_replacement import InstanceReplacer
from data_io.feature_extractor import extract_sku_annotations
from data_io.report import write_excel_report
from misc.currency import (
    ConversionContext, detect_currency_mismatch, resolve_exchange_rate, convert_region_pricing
)


async def main(src_region: str, dest_region: str,
               cur_data: pd.DataFrame,
               output_path: str,
               use_cache: bool = True,
               detailed_report: bool = False,
               instance_replacer: InstanceReplacer | None = None,
               exchange_rate: float | None = None):
    """
    Determine region compatibility for the given CUR SKUs and write the report to Excel

    Parameters
    ----------
    detailed_report : Flag indicating whether detailed SKU mapping should be included in the Excel report
    src_region : The code for the AWS region where the CUR data was gathered
    dest_region : The code for the AWS region where you want to evaluate SKU availability
    cur_data : CUR data
    output_path : Path where the output report will be written to
    use_cache : Flag indicating whether the cache should be used for fetching product pricing info
    instance_replacer : Instance replacement profile (optional)
    exchange_rate : Exchange rate for cross-currency pricing conversion (optional)
    """
    # Validate CUR data columns and handle spot pricing before downloading the pricing data
    if not all([c in cur_data.columns for c in ['Service', 'Rate Code', 'Effective Cost']]):
        raise InvalidCURDataException('CUR data does not include the required columns')

    # Retrieve the products from the source region(s)
    async with asyncio.TaskGroup() as tg:
        src_pricing_task = tg.create_task(get_all_products_in_region(src_region,
                                                                     use_cache=use_cache))
        dest_pricing_task = tg.create_task(get_all_products_in_region(dest_region,
                                                                      use_cache=use_cache))
    src_pricing = src_pricing_task.result()
    dest_pricing = dest_pricing_task.result()

    if 'Equivalent On-Demand SKU' not in cur_data.columns:
        cur_data.insert(cur_data.shape[1], 'Equivalent On-Demand SKU', pd.NA)

    # Map all data transfer cost to the AWSDataTransfer service so that it can be compared
    mask = cur_data['Rate Code'].isin(src_pricing.services['AWSDataTransfer'].skus['rateCode'])
    cur_data.loc[mask, 'Service'] = 'AWSDataTransfer'

    # Detect currency mismatch and convert if needed
    mismatch = detect_currency_mismatch(src_pricing, dest_pricing)
    if mismatch is not None:
        src_currency, dest_currency = mismatch
        rate = resolve_exchange_rate(exchange_rate, src_currency, dest_currency)
        dest_pricing = convert_region_pricing(dest_pricing, rate, src_currency)
        conversion_context = ConversionContext(
            converted=True,
            exchange_rate=rate,
            src_currency=src_currency,
            dest_currency=dest_currency,
        )
    elif exchange_rate is not None:
        logging.info(
            'Exchange rate %.4f ignored: source and destination regions use the same currency',
            exchange_rate,
        )
        conversion_context = ConversionContext(converted=False)
    else:
        conversion_context = ConversionContext(converted=False)

    # Write the Excel report with the service & SKU availability data
    logging.info(f'Writing compatibility report to {output_path}...')

    region_pricing_map = src_pricing.compare(dest_pricing, instance_replacer=instance_replacer,
                                             conversion_context=conversion_context)

    # Preserve original (unconverted) pricing columns when conversion was applied
    if conversion_context.converted:
        original_col = f'pricePerUnit - target ({conversion_context.dest_currency})'
        for service_mapping in region_pricing_map.services.values():
            df = service_mapping.mapped
            if 'pricePerUnit - target' in df.columns:
                # Recover original unconverted values by reversing the conversion
                df[original_col] = df['pricePerUnit - target'] / conversion_context.exchange_rate

    write_excel_report(region_pricing_map, cur_data, output_dir=output_path,
                       detailed_feature_mapping=detailed_report,
                       conversion_context=conversion_context)


if __name__ == '__main__':
    import argparse

    sorted_region_codes = sorted(REGION_CODES.keys())

    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(help='Action to execute', dest='subparser', required=True)
    analyzer = subparsers.add_parser('analyze', help='Analyze a workload across regions')
    analyzer.add_argument('source_region',
                          choices=sorted_region_codes,
                          help='Code for the region where the CUR data was gathered')
    analyzer.add_argument('dest_region',
                          choices=sorted_region_codes,
                          help='Code for the destination AWS region')
    analyzer.add_argument('--cur-data', type=Path, required=True,
                          help='Path to the CSV/XLSX file with CUR data')
    analyzer.add_argument('--exchange-rate', type=float, required=False, default=None,
                          help='Exchange rate for cross-currency pricing conversion (e.g., EUR to USD)')
    analyzer.add_argument('output_path', type=str,
                          help='Directory where the output report will be written to')
    analyzer.add_argument('--no-cache', help='Do not use the pricing data cache', action='store_true')
    analyzer.add_argument('--detailed-report', help='Generate detailed SKU mapping in Excel report',
                          action='store_true', default=False)
    analyzer.add_argument('--instance-replacements', type=Path,
                          help='Path to the JSON file with instance replacement configurations')
    annotation_updater = subparsers.add_parser('update-annotations',
                                               help='Extract SKU annotations from Excel report')
    annotation_updater.add_argument('report_path', type=Path,
                                    help='Path to the Excel report file')
    annotation_updater.add_argument('region', choices=sorted_region_codes,
                                    help='AWS region code')

    args = parser.parse_args()

    if args.subparser == 'update-annotations':
        extract_sku_annotations(args.report_path, args.region)
        sys.exit(0)

    instance_replacer = None
    try:
        df = pd.read_csv(args.cur_data)
    except UnicodeDecodeError:
        df = pd.read_excel(args.cur_data)

    if args.instance_replacements is not None:
        try:
            instance_replacer = InstanceReplacer.from_config(
                json.loads(args.instance_replacements.read_text()))
        except (JSONDecodeError, RuntimeError) as e:
            print('Could not load instance replacement config '
                  f'from {args.instance_replacements} ({e})')
            sys.exit(1)

    exchange_rate = getattr(args, 'exchange_rate', None)
    try:
        asyncio.run(main(src_region=args.source_region,
                         dest_region=args.dest_region,
                         cur_data=df,
                         output_path=args.output_path,
                         use_cache=not args.no_cache,
                         detailed_report=args.detailed_report,
                         instance_replacer=instance_replacer,
                         exchange_rate=exchange_rate))
    except InvalidCURDataException as e:
        print('The CUR data file does contain the required columns, refer to '
              'README.md to learn how to generate it')
        sys.exit(1)
