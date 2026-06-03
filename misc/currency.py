"""
Currency conversion utilities for cross-partition pricing comparisons.

Handles detection of currency mismatches between AWS regions, exchange rate
validation, and conversion of pricing data from one currency to another.
"""

import copy
import math
import sys
import httpx
import logging
from dataclasses import dataclass
import defusedxml.ElementTree as ET

from artifacts.region import RegionPricing
from artifacts.service import ServicePricing

logger = logging.getLogger(__name__)


@dataclass
class ConversionContext:
    """Metadata about currency conversion applied during comparison."""
    converted: bool
    exchange_rate: float | None = None
    src_currency: str | None = None
    dest_currency: str | None = None


def detect_currency_mismatch(
    src_region: RegionPricing,
    dest_region: RegionPricing,
) -> tuple[str, str] | None:
    """
    Compare currencies of two regions by inspecting the 'currency' column
    in their service features DataFrames.

    Returns (src_currency, dest_currency) if they differ, None if same.
    """
    src_currencies: set[str] = set()
    dest_currencies: set[str] = set()

    for service in src_region.services.values():
        src_currencies.update(service.skus['currency'].dropna().unique())

    for service in dest_region.services.values():
        dest_currencies.update(service.skus['currency'].dropna().unique())

    if not src_currencies or not dest_currencies:
        raise KeyError("Missing 'currency' column data in service features")

    # Each region should have a single consistent currency
    if len(src_currencies) > 1:
        logger.warning(
            'Source region %s has multiple currencies: %s',
            src_region.code, src_currencies,
        )
    if len(dest_currencies) > 1:
        logger.warning(
            'Target region %s has multiple currencies: %s',
            dest_region.code, dest_currencies,
        )

    # Use the most common currency (first element) for comparison
    src_currency = sorted(src_currencies)[0]
    dest_currency = sorted(dest_currencies)[0]

    if src_currency == dest_currency:
        return None

    return (src_currency, dest_currency)


def validate_rate(rate: float) -> float:
    """
    Validate that the exchange rate is a positive finite number.

    Raises ValueError if the rate is zero, negative, NaN, or infinity.
    Returns the rate unchanged for positive finite floats.
    """
    if not isinstance(rate, (int, float)):
        raise ValueError(f'Exchange rate must be a number, got {type(rate).__name__}')

    if math.isnan(rate):
        raise ValueError('Exchange rate must not be NaN')

    if math.isinf(rate):
        raise ValueError('Exchange rate must not be infinite')

    if rate <= 0:
        raise ValueError(f'Exchange rate must be positive, got {rate}')

    return float(rate)

ECB_NAMESPACE = {'gesmes': 'http://www.gesmes.org/xml/2002-08-01',
                 'eurofxref': 'http://www.ecb.int/vocabulary/2002-08-01/eurofxref'}


def fetch_ecb_rate(
    src_currency: str,
    dest_currency: str,
) -> tuple[float, str, str]:
    """
    Fetch the current EUR/USD exchange rate from the ECB daily XML feed.

    Returns (rate, source_description, publication_date).
    The rate converts from dest_currency to src_currency.

    Calls sys.exit() with a clear error message on any failure.
    """
    currencies = {src_currency, dest_currency}
    if currencies != {'EUR', 'USD'}:
        sys.exit(
            f'ECB feed only supports EUR/USD conversion, '
            f'got {src_currency}/{dest_currency}. '
            f'Please provide a rate manually via --exchange-rate.'
        )

    try:
        response = httpx.get('https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml',
                            timeout=30)
        response.raise_for_status()
        xml_data = response.content
    except Exception as exc:
        sys.exit(
            f'Failed to fetch exchange rate from ECB: {exc}\n'
            f'Please provide the rate manually via --exchange-rate.'
        )

    try:
        root = ET.fromstring(xml_data)

        # Extract publication date from the outer Cube element
        cube_time = root.find('.//eurofxref:Cube[@time]', ECB_NAMESPACE)
        if cube_time is None:
            raise ValueError('Could not find publication date in ECB XML')
        publication_date = cube_time.attrib['time']

        # Find the USD rate entry
        usd_cube = root.find(
            ".//eurofxref:Cube[@currency='USD']", ECB_NAMESPACE,
        )
        if usd_cube is None:
            raise ValueError('Could not find USD rate in ECB XML feed')

        eur_to_usd = float(usd_cube.attrib['rate'])
    except Exception as exc:
        sys.exit(
            f'Failed to parse ECB exchange rate XML: {exc}\n'
            f'Please provide the rate manually via --exchange-rate.'
        )

    # Determine the conversion direction
    if src_currency == 'USD' and dest_currency == 'EUR':
        # Converting EUR -> USD: multiply EUR prices by the EUR/USD rate
        rate = eur_to_usd
    else:
        # Converting USD -> EUR: multiply USD prices by 1/EUR_USD rate
        rate = 1.0 / eur_to_usd

    rate = validate_rate(rate)

    source_description = 'European Central Bank daily reference rate'
    return (rate, source_description, publication_date)


def resolve_exchange_rate(
    user_rate: float | None,
    src_currency: str,
    dest_currency: str,
) -> float:
    """
    Return the exchange rate to use for converting dest_currency to src_currency.

    If user_rate is provided, use it directly (skipping ECB fetch).
    Otherwise fetch from the ECB. Logs the rate, source, and publication date.
    """
    if user_rate is not None:
        rate = validate_rate(user_rate)
        logger.info(
            'Using user-provided exchange rate: %s %s/%s',
            rate, src_currency, dest_currency,
        )
        return rate

    rate, source, pub_date = fetch_ecb_rate(src_currency, dest_currency)
    logger.info(
        'Fetched exchange rate: %s %s/%s (source: %s, date: %s)',
        rate, src_currency, dest_currency, source, pub_date,
    )
    return rate


def convert_service_pricing(
    service: ServicePricing,
    exchange_rate: float,
    target_currency: str,
) -> ServicePricing:
    """
    Return a new ServicePricing with pricePerUnit multiplied by exchange_rate
    and currency column updated to target_currency. Does not mutate the original.
    """
    converted_features = service.skus.copy()
    converted_features['pricePerUnit'] = converted_features['pricePerUnit'] * exchange_rate
    converted_features['currency'] = target_currency

    return ServicePricing(
        code=service.code,
        skus=converted_features,
        region_code=service.region_code,
        publication_date=service.publication_date,
    )


def convert_region_pricing(
    region_pricing: RegionPricing,
    exchange_rate: float,
    target_currency: str,
) -> RegionPricing:
    """
    Return a new RegionPricing where all services have converted pricing.
    Deep copies the original to avoid mutation.
    """
    converted = copy.deepcopy(region_pricing)
    converted.services = {
        code: convert_service_pricing(service, exchange_rate, target_currency)
        for code, service in converted.services.items()
    }
    return converted
