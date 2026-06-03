import os
import time
import json
import httpx
import boto3
import logging
import asyncio
import pandas as pd
from io import StringIO
import concurrent.futures
from datetime import date, datetime
from artifacts.region import RegionPricing
from artifacts.service import ServicePricing
from data_io.storage_wrapper import StorageWrapper
from misc.sku_normalization import normalize_usage_types, normalize_unit

# Create a client to the pricing API
pricing = boto3.client('pricing', region_name='eu-central-1')
regional_pricing = {}
if os.environ.get('EUSC_PROFILE_NAME') is not None:
    session = boto3.Session(region_name='eusc-de-east-1',
                            profile_name=os.environ.get('EUSC_PROFILE_NAME'))
    regional_pricing['eusc-de-east-1'] = session.client('pricing')
services_paginator = pricing.get_paginator('describe_services')
paginator = pricing.get_paginator('get_products')
cache = StorageWrapper(os.environ.get('CACHE_DIR', 'sku_cache'))


def get_service_codes() -> list[str]:
    """
    Get the list of AWS services for the default AWS region from the bulk API

    Uses the AWS Pricing API to retrieve all available service codes. The API results
    are paginated, so this function iterates through all pages to build a complete list.

    Returns:
        list[str]: A sorted list of AWS service codes (e.g. 'AmazonEC2', 'AmazonS3', etc.)

    Example:
        >>> get_service_codes()
        ['AmazonEC2', 'AmazonRDS', 'AmazonS3', ...]

    Note:
        Requires a configured boto3 pricing client with appropriate permissions to
        call the describe_services API
    """
    paginator = pricing.get_paginator('describe_services')
    services = set()
    for page in paginator.paginate():
        for service in page.get('Services', []):
            services |= {service['ServiceCode']}

    return sorted(services)


def get_products(service: str, region: str,
                 effective_date: date = date.today(), use_cache: bool = True) -> ServicePricing | None:
    """
    Retrieves pricing information for a specific AWS service in a given region.

    Args:
        service (str): AWS service code (e.g. 'AmazonEC2', 'AmazonS3')
        region (str): AWS region code (e.g. 'us-east-1')
        effective_date (date, optional): Date for which to get pricing. Defaults to current date.
        use_cache (bool, optional): Whether to use cached data if available. Defaults to True.

    Returns:
        pd.DataFrame: DataFrame containing pricing information with columns:
            - SKU: Unique identifier for the product
            - regionCode: AWS region code
            - serviceCode: AWS service code
            - usageType: Type of usage for the product
            - normalizedUsageType: Normalized version of usage type
            - operation: API operation name
            - priceDescription: Description of the price
            - pricePerUnit: Cost per unit in USD
            - currency: Currency code (USD)

    Raises:
        ServiceUnavailableError: If the service is not available in the specified region
        PriceListUnavailableError: If unable to retrieve the price list
        httpx.HTTPError: If there are network/HTTP errors when fetching the price list

    Notes:
        - Uses local file caching to store pricing data for 24 hours
        - Cache files are stored in CACHE_DIR with SHA256 hashed filenames
        - Normalizes usage types using normalize_usage_types() function
    """
    cache_file = f'{region}/{effective_date}/{service}.csv'

    # Read the data from the cache if available and if it is current enough
    try:
        if not use_cache:
            raise FileNotFoundError(f'Cache usage disabled for {region}')
        data = cache.read_bytes(cache_file, compressed=True).decode()
    except FileNotFoundError:
        logging.info(f'\t{service} in {region}')

        pricing_client = regional_pricing.get(region, pricing)
        # First, get the price list ARN
        try:
            response = pricing_client.list_price_lists(ServiceCode=service,
                                                       RegionCode=region,
                                                       EffectiveDate=datetime(year=effective_date.year,
                                                                              month=effective_date.month,
                                                                              day=effective_date.day),
                                                       CurrencyCode='EUR' if region in ('eusc-de-east-1',) else 'USD')
            if len(response.get('PriceLists', [])) < 1:
                raise pricing_client.exceptions.ResourceNotFoundException(operation_name='ListPriceLists',
                                                                          error_response={'Error': {
                                                                              'Code': 'ResourceNotFoundException',
                                                                              'Message': 'The specified service does not exist.'}})
        except pricing_client.exceptions.ResourceNotFoundException as e:
            logging.debug(f'Got ResourceNotFoundException when trying to fetch {service} price in {region}:\n{e}')
            return None
        price_list_arn = response['PriceLists'][0]['PriceListArn']
        # Get the URL for the price list
        response = pricing_client.get_price_list_file_url(PriceListArn=price_list_arn,
                                                          FileFormat='csv')
        price_list_uri = response.get('Url', '')
        if price_list_uri == '':
            return None
        for attempt in range(3):
            try:
                with httpx.Client() as client:
                    response = client.get(price_list_uri)
                    response.raise_for_status()
                break
            except httpx.HTTPError as e:
                logging.exception(e)
                if attempt == 2:  # Last attempt
                    raise
                time.sleep(2 ** attempt)
        cache.write_bytes(cache_file, response.text.encode(), compressed=True)
        data = response.text

    # Read the data from the pricing file
    df = pd.read_csv(StringIO(data), skiprows=5)
    publication_date = datetime(year=effective_date.year, month=effective_date.month, day=effective_date.day)
    line = data.splitlines()[2]
    if line.startswith('"Publication Date"'):
        publication_date = datetime.fromisoformat(line.split(',')[1].strip('"'))

    # Normalize the CSV contents
    df.rename(columns={'Region Code': 'regionCode',
                       'PriceDescription': 'priceDescription',
                       'PricePerUnit': 'pricePerUnit',
                       'RateCode': 'rateCode',
                       'Currency': 'currency'},
              inplace=True)
    df['normalizedUsageType'] = df['usageType'].apply(lambda x: normalize_usage_types(x, region))
    df['normalizedRateCode'] = df['rateCode'].apply(lambda x: '.'.join(x.split('.')[1:]))
    if 'TermType' not in df.columns:
        df['TermType'] = ''
    if 'Unit' not in df.columns:
        df['Unit'] = ''
    df['TermType'] = df['TermType'].fillna('')
    df['Unit'] = df['Unit'].apply(lambda x: normalize_unit(x) if isinstance(x, str) else '')
    for col in ('StartingRange', 'EndingRange'):
        if col not in df.columns:
            df[col] = ''
        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)
    if 'regionCode' not in df.columns:
        df.insert(df.shape[1], 'regionCode', '')
    if 'serviceCode' not in df.columns:
        df['serviceCode'] = ''
        df.loc[df['priceDescription'].str.lower().str.contains('Marketplace'), 'serviceCode'] = 'awsmarketplace'
    for col in ('PurchaseOption', 'LeaseContractLength'):
        if col in df.columns:
            df[col] = df[col].fillna('')
            df[col] = df[col].str.lower().str.replace(' ', '')
    kept_cols = ['SKU', 'regionCode', 'serviceCode', 'usageType', 'normalizedUsageType',
                 'operation', 'priceDescription', 'pricePerUnit', 'rateCode', 'normalizedRateCode',
                 'TermType', 'Unit', 'StartingRange', 'EndingRange', 'currency']
    # Extra optional columns to keep if present
    extra_cols = [col for col in ('LeaseContractLength', 'PurchaseOption', 'Instance Type')
                  if col in df.columns]
    df = df[kept_cols + extra_cols]

    return ServicePricing(code=service, region_code=region, publication_date=publication_date, skus=df)


async def get_all_products_in_region(region: str, effective_date: date = date.today(), use_cache=True) -> RegionPricing:
    """
    Retrieves pricing information for all available AWS services in the specified regions.

    Args:
        region: AWS region code (e.g. ['us-east-1', 'eu-west-1'])
        effective_date (date, optional): The effective date for this pricing
        use_cache (bool, optional): Whether to use cached data if available. Defaults to True.

    Returns:
        tuple: Contains:
            - pd.DataFrame: Combined pricing data for all available services across specified regions
            - dict[str, set[str]]: Dictionary mapping region codes to sets of unavailable service codes

    The function iterates through all AWS services for each specified region, attempts to get pricing data
    for each service, and combines the results into a single DataFrame. Services that are not available
    in each region are tracked separately in the unavailable_services set.

    Cache files are stored in {CACHE_DIR}/{region}/all_products.json and are considered valid for 24 hours.
    """
    cache_file = f'{region}/{effective_date}/all_products.json'
    try:
        if not use_cache:
            raise FileNotFoundError(f'Cache usage disabled for {region}')
        cache_data = json.loads(cache.read_bytes(cache_file, compressed=True).decode())
        logging.info(f'{region} data read from cache at "{cache_file}"')
        return RegionPricing.from_dict(cache_data)
    except FileNotFoundError:
        # No valid cache found, query the API for the data
        logging.info(f'Getting features available in {region} for {effective_date}')
        tasks = {}
        loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            for service in get_service_codes():
                tasks[service] = await loop.run_in_executor(pool, lambda: get_products(service, region,
                                                                                       effective_date=effective_date))

        # Construct the RegionPricing object, cache & return it
        unavailable_services = {service for service, result in tasks.items() if result is None}
        services = {service: result for service, result in tasks.items() if isinstance(result, ServicePricing)}
        rp = RegionPricing(code=region, effective_date=effective_date, services=services,
                           unavailable_services=unavailable_services)
        cache.write_bytes(cache_file, json.dumps(rp.to_dict()).encode(), compressed=True)
        return rp
