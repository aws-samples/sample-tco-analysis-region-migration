REGION_CODES = {'af-south-1': ('AFS1-', 'AFS1:', 'af-south-1-'),
                'ap-east-1': ('APE1-', 'APE1:', 'ap-east-1-'),
                'ap-northeast-1': ('APN1-', 'APN1:', 'ap-northeast-1-'),
                'ap-northeast-2': ('APN2-', 'APN2:', 'ap-northeast-2-'),
                'ap-northeast-3': ('APN3-', 'APN3:', 'ap-northeast-3-'),
                'ap-south-1': ('APS3-', 'APS3:', 'ap-south-1-'),
                'ap-south-2': ('APS5-', 'APS5:', 'ap-south-2-'),
                'ap-southeast-1': ('APS1-', 'APS1:', 'ap-southeast-1-'),
                'ap-southeast-2': ('APS2-', 'APS2:', 'ap-southeast-2-'),
                'ap-southeast-3': ('APS4-', 'APS4:', 'ap-southeast-3-'),
                'ap-southeast-4': ('APS6-', 'APS6:', 'ap-southeast-4-'),
                'ca-central-1': ('CAN1-', 'CAN1:', 'ca-central-1-'),
                'ca-west-1': ('CAN2-', 'CAN2:', 'ca-west-1-'),
                'cn-north-1': ('CNN1-', 'CNN1:', 'cn-north-1-'),
                'eu-central-1': ('EUC1-', 'EUC1:', 'eu-central-1-'),
                'eu-central-2': ('EUC2-', 'EUC2:', 'eu-central-2-'),
                'eu-north-1': ('EUN1-', 'EUN1:', 'eu-north-1-'),
                'cn-northwest-1': ('CNW1-', 'CNW1:', 'cn-northwest-1-'),
                'eusc-de-east-1': ('ESC1-', 'eusc-de-east-1-'),
                'eu-south-1': ('EUS1-', 'EUS1:', 'eu-south-1-'),
                'eu-south-2': ('EUS2-', 'EUS2:', 'eu-south-2-'),
                'eu-west-1': ('EU-', 'EU:', 'EUW1-', 'eu-west-1-'),
                'eu-west-2': ('EUW2-', 'EUW2:', 'eu-west-2-'),
                'eu-west-3': ('EUW3-', 'EUW3:', 'eu-west-3-'),
                'il-central-1': ('ILC1-', 'ILC1:', 'il-central-1-'),
                'me-central-1': ('MEC1-', 'MEC1:', 'me-central-1-'),
                'me-south-1': ('MES1-', 'MES1:', 'me-south-1-'),
                'mx-central-1': ('MXC1-', 'MXC1:', 'mx-central-1-'),
                'sa-east-1': ('SAE1-', 'SAE1:', 'sa-east-1-'),
                'us-gov-west-1': ('UGW1-', 'UGW1:', 'us-gov-west-1-'),
                'us-gov-east-1': ('UGE1-', 'UGE1:', 'us-gov-east-1-'),
                'us-east-1': ('USE1-', 'USE1:', 'us-east-1-'),
                'us-east-2': ('USE2-', 'USE2:', 'us-east-2-'),
                'us-west-1': ('USW1-', 'USW1:', 'us-west-1-'),
                'us-west-2': ('USW2-', 'USW2:', 'us-west-2-')}
# Well known normalizations. These take priority over standard normalization
WELL_KNOW_NORMALIZATIONS = {'EU-AWSSecretsManager-APIRequests': 'REGION-AWSSecretsManagerAPIRequest'}


def normalize_usage_types(code: str, region: str, ignore_errors: bool = False) -> str | None:
    """
    Normalizes AWS usage type codes by removing region-specific prefixes.

    The purpose of this function is to make usage types region agnostic, so that if a usageType is present
    in two separate regions both will return the same normalized string.

    Parameters
    ----------
    code : str
        The usage type code to normalize
    region : str
        The AWS region code (e.g. 'us-east-1')
    ignore_errors : bool, optional
        If True, return None instead of raising ValueError for unsupported regions.
        Default is False.

    Returns
    -------
    str or None
        The normalized usage type code with region prefix removed if present.
        If code exists in WELL_KNOW_NORMALIZATIONS, returns the mapped value.
        If no region prefix is found, returns the original code unchanged.
        Returns None if ignore_errors=True and region is not supported.

    Raises
    ------
    ValueError
        If the provided region is not supported (not found in REGION_CODES) and ignore_errors=False
    """
    if code in WELL_KNOW_NORMALIZATIONS:
        return WELL_KNOW_NORMALIZATIONS[code]

    if region not in REGION_CODES:
        if ignore_errors:
            return None

        raise ValueError(f'Region {region} not supported')

    for region_code in REGION_CODES[region]:
        if code.startswith(region_code):
            return f'REGION-{code[len(region_code):]}'

    # That didn't work... AWSBackup has usageTypes like `AFS1-EU-CrossRegion-WarmBytes-Aurora`
    # where the region code we're interested in normalizing is the second one (`EU` in this
    # example) let's see if we get a match there.
    # We should not get into this code for SKUs going the other way `EU-AFS1-CrossRegion-WarmBytes-Aurora`
    # since those would've been caught by the code above.
    # Also, handle AWSDataTransfer-specific usageTypes like `EU-EUS2-AWS-Out-ABytes-T1`
    if (('-CrossRegion-WarmBytes-' in code) or
            code.endswith('-AWS-In-Bytes') or code.endswith('-AWS-Out-Bytes') or
            ('-AWS-In-ABytes' in code) or ('-AWS-Out-ABytes' in code) or
            code.endswith('-S3RTC-In-Bytes') or code.endswith('-S3RTC-Out-Bytes')):
        for region_code in REGION_CODES[region]:
            parts = code.split('-')
            if parts[1] != region_code.strip('-'):
                continue

            parts[1] = 'REGION'
            return '-'.join(parts)

    return code

# Normalization map for Unit values that differ across regions/partitions
# (e.g., "Months" vs "Month", "GigaBytes" vs "GB")
UNIT_NORMALIZATIONS = {
    'months': 'Month',
    'gigabytes': 'GB',
    'gigabytesmonth': 'GB-Month',
    'gigabyte': 'GB',
    'gigabyte month': 'GB-Month',
    'hours': 'Hrs',
}


def normalize_unit(unit: str) -> str:
    """
    Normalize a pricing Unit value to handle inconsistencies across regions.

    The AWS Pricing API uses different unit names for the same concept in
    different regions (e.g., "Months" vs "Month", "GigaBytes" vs "GB").
    This function maps known variants to a canonical form.

    Parameters
    ----------
    unit : str
        The unit value from the pricing CSV

    Returns
    -------
    str
        The normalized unit value
    """
    if not isinstance(unit, str):
        return unit
    return UNIT_NORMALIZATIONS.get(unit.lower(), unit)

