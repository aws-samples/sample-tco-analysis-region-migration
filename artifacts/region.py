from __future__ import annotations

import logging
import pandas as pd
from dataclasses import dataclass
from misc.spot import get_spot_pricing
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING
from misc.sku_normalization import REGION_CODES
from misc.instance_replacement import InstanceReplacer
from artifacts.service import ServicePricing, ServiceRateCodeMapping

if TYPE_CHECKING:
    from misc.currency import ConversionContext


@dataclass
class RegionRateCodeMapping:
    """
    An object with data regarding how consumption maps from a source to a destination region
    """
    services: dict[str, ServiceRateCodeMapping]
    region_mapping: RegionMapping


@dataclass
class RegionMapping:
    """
    Class holding the difference in services for two given regions
    """
    source_region: RegionPricing
    target_region: RegionPricing
    unavailable_services: set[str]
    services: dict[str, ServiceRateCodeMapping]
    instance_replacer: InstanceReplacer | None
    conversion_context: ConversionContext | None = None

    def _map_spot_consumption(self, consumption: pd.DataFrame) -> (pd.DataFrame | None, pd.DataFrame | None):
        # Filter the consumption to only consider spot
        spot_mask = (consumption['Service'] == 'AmazonEC2') & consumption['Usage Type'].str.contains('SpotUsage')
        df = consumption[spot_mask]
        if df.shape[0] == 0:
            return None, None

        # Get the Spot pricing comparison for the instances where we have consumption
        logging.info('Comparing spot pricing...')
        mapped_dfs = []
        unmapped_dfs = []
        volatile_instances = []
        now = datetime.today()
        then = now - timedelta(days=30)
        product_descriptions = (df['Description'].str.
                                split('Spot Instance-hour', expand=True)[0].str.
                                split(n=1, expand=True)[1].str.strip().drop_duplicates())
        for product in product_descriptions:
            logging.info(f'Getting spot pricing for "{product}" in {self.source_region.code}')
            platform_mask = consumption['Description'].str.contains(product, regex=False)
            platform_usage = consumption[spot_mask & platform_mask].copy()
            if isinstance(self.instance_replacer, InstanceReplacer):
                instances = platform_usage['Usage Type'].str.split('SpotUsage:', expand=True)
                sorted_instances = sorted(instances[1].drop_duplicates())
                src_spot_price = get_spot_pricing(sorted_instances, then, now,
                                                  region=self.source_region.code,
                                                  product_description=product)
                platform_usage = self.instance_replacer.apply_replacements(platform_usage,
                                                                           service_code='AmazonEC2',
                                                                           instance_type_col='Usage Type')
                _instances = platform_usage['Usage Type'].str.split('SpotUsage:', expand=True)
                sorted_instances = sorted(_instances[1].drop_duplicates())
                target_spot_price = get_spot_pricing(sorted_instances, then, now,
                                                     region=self.target_region.code,
                                                     product_description=product)
            else:
                # Here we just have to compute the pricing difference between regions for the same instance types
                instances = platform_usage['Usage Type'].str.split('SpotUsage:', expand=True)
                sorted_instances = sorted(instances[1].drop_duplicates())
                target_spot_price = get_spot_pricing(sorted_instances, then, now,
                                                     region=self.target_region.code,
                                                     product_description=product)
                src_spot_price = get_spot_pricing(sorted_instances, then, now,
                                                  region=self.source_region.code,
                                                  product_description=product)
            # Warn about spot instances whose price has been very volatile in the last 30 days
            _volatile_instances = target_spot_price[(target_spot_price['max'] - target_spot_price['min']).abs() >
                                                    target_spot_price['median'] * 0.25]
            for i in _volatile_instances.index:
                volatile_instances.append(f'{i} - {product}')

            # Map pricing data from source to target regions based on median values
            if isinstance(self.instance_replacer, InstanceReplacer):
                src_spot_price['replacement'] = src_spot_price.index
                src_spot_price = self.instance_replacer.apply_replacements(src_spot_price,
                                                                           service_code='AmazonEC2',
                                                                           instance_type_col='replacement',
                                                                           with_colon=False)
                df = src_spot_price.merge(target_spot_price, how='right',
                                          left_on='replacement', right_index=True,
                                          suffixes=(' - source', ' - target')).drop(columns=['Replaced'])
            else:
                df = src_spot_price.merge(target_spot_price, how='right',
                                          left_index=True, right_index=True,
                                          suffixes=(' - source', ' - target'))
                df['replacement'] = df.index
            # Apply currency conversion to target spot prices if needed
            if self.conversion_context is not None and self.conversion_context.converted:
                df['median - target'] = df['median - target'] * self.conversion_context.exchange_rate
            df['priceRatio'] = df['median - target'] / df['median - source']
            # Finally combine the instance information with the actual instances
            merged = instances.merge(df[['replacement', 'priceRatio', 'median - source', 'median - target']],
                                     how='right', left_on=1, right_index=True)

            # Convert the format of the consumption dataframe to that of the mapped dataframe
            df = consumption.loc[merged.index]
            df['pricePerUnit - origin'] = merged['median - source']
            df['pricePerUnit - target'] = merged['median - target']
            df['priceRatio'] = merged['priceRatio']
            df['cost - target'] = df['Effective Cost'] * df['priceRatio']
            # Skip this product if we could not find any spot pricing
            if df.shape[0] == 0:
                continue
            # Create the column with the target usageType, removing the region prefix and replacing the
            # instance type, if an instance replacer is available
            if isinstance(self.instance_replacer, InstanceReplacer):
                df['usageType - target'] = df['Usage Type'].str.split('SpotUsage:', n=1, expand=True)[1].apply(
                    lambda x: 'SpotUsage:' + self.instance_replacer.get_replacement('AmazonEC2', x))
            else:
                df['usageType - target'] = df['Usage Type'].str.split('SpotUsage:', n=1, expand=True)[1].apply(
                    lambda x: f'SpotUsage: {x}')
            df = df[['SKU', 'Service', 'Usage Type', 'usageType - target', 'Operation', 'Rate Code',
                     'Description', 'pricePerUnit - origin', 'pricePerUnit - target', 'Effective Cost',
                     'priceRatio', 'cost - target']]
            df = df.rename(columns={'SKU': 'SKU - origin',
                                    'Service': 'serviceCode',
                                    'Usage Type': 'usageType - origin',
                                    'Operation': 'operation',
                                    'Rate Code': 'rateCode - origin',
                                    'Description': 'priceDescription',
                                    'Effective Cost': 'cost - origin'})
            df.insert(loc=1, column='regionCode - origin', value=self.source_region.code)
            df.insert(loc=2, column='SKU - target', value=None)
            mapped_dfs.append(df)

            # Now, create the unmapped spot usage report
            df = consumption[spot_mask & platform_mask]
            spot_pricing = instances.merge(src_spot_price, how='left', left_on=1, right_index=True)
            unmapped_indices = [i for i in df.index if i not in merged.index]
            df = df.loc[unmapped_indices]
            df['pricePerUnit'] = spot_pricing.loc[unmapped_indices, 'median']
            df = df[['SKU', 'Service', 'Usage Type', 'Operation', 'Rate Code',
                     'Description', 'pricePerUnit', 'Effective Cost']]
            df = df.rename(columns={'Service': 'serviceCode',
                                    'Usage Type': 'usageType',
                                    'Operation': 'operation',
                                    'Rate Code': 'rateCode',
                                    'Description': 'priceDescription',
                                    'Effective Cost': 'cost - origin'})
            df.insert(loc=1, column='regionCode', value=self.source_region.code)
            df.insert(loc=4, column='normalizedUsageType', value='')
            df.insert(loc=6, column='normalizedRateCode', value='')
            unmapped_dfs.append(df)

        # Warn about volatile instances, this only goes to the logger and is not returned to
        # the caller... I might want to change the behaviour to report it
        if len(volatile_instances) > 0:
            logging.warning(' 💸💸 Spot pricing for the following instance types has changed more '
                            'than 25% of its median value in the last 30 days:')
            for i in volatile_instances:
                logging.warning(f'\t{i}')

        return pd.concat(mapped_dfs, ignore_index=True), pd.concat(unmapped_dfs, ignore_index=True)

    def map_consumption(self, consumption: pd.DataFrame) -> RegionRateCodeMapping:
        """
        Map the consumption in the source region to the destination region

        Parameters
        ----------
        consumption : DataFrame with the consumption data

        Returns
        -------
        A mapping of the cost of running the given consumption in a target region
        """
        # Only keep services that are present in the CUR data
        services = {}
        for service_code in {code for code in self.services.keys() if code in consumption['Service'].unique()}:
            df = self.services[service_code].mapped.merge(consumption[['Rate Code', 'Effective Cost']],
                                                          how='left',
                                                          left_on='rateCode - origin', right_on='Rate Code')
            df.dropna(inplace=True, subset='Effective Cost')
            df['priceRatio'] = (df[f'pricePerUnit - target'] / (df[f'pricePerUnit - origin']))
            df.loc[df['priceRatio'].isna(), 'priceRatio'] = 1.0
            mask = ~df['SKU - target'].isna()
            df = (df.drop(columns=['Rate Code', 'normalizedUsageType', 'normalizedRateCode',
                                   'TermType', 'Unit', 'currency']).
                  rename(columns={'Effective Cost': 'cost - origin'}))
            mapped = df.loc[mask].copy()
            mapped['cost - target'] = (mapped['cost - origin'] * mapped['priceRatio'])

            # Create the list of unmapped rate code. We'll do that by looking at what SKUs from
            mask = self.services[service_code].unmapped['rateCode'].isin(consumption['Rate Code'])
            unmapped = self.services[service_code].unmapped[mask]
            unmapped = unmapped.merge(consumption[['Rate Code', 'Effective Cost']],
                                      how='left',
                                      left_on='rateCode',
                                      right_on='Rate Code').drop(
                columns=['Rate Code', 'TermType', 'Unit', 'currency']).rename(
                columns={'Effective Cost': 'cost - origin'})

            # Perform service-specific tuning in the consumption
            if service_code == 'AmazonEC2':
                # Create entries for Spot consumption. Those will not be included above since
                # Spot pricing is not provided by the Pricing API
                spot_mapped, spot_unmapped = self._map_spot_consumption(consumption)
                if spot_mapped is not None and spot_unmapped is not None:
                    mapped = pd.concat([mapped, spot_mapped], ignore_index=True)
                    unmapped = pd.concat([unmapped, spot_unmapped], ignore_index=True)

            services[service_code] = ServiceRateCodeMapping(service=service_code,
                                                            mapped=mapped,
                                                            unmapped=unmapped,
                                                            reference_service=self.source_region.services[service_code],
                                                            target_service=self.target_region.services[service_code])

        return RegionRateCodeMapping(services=services,
                                     region_mapping=self)


@dataclass
class RegionPricing:
    """
    Report containing feature availability info for a particular service
    """
    code: str
    effective_date: date
    services: dict[str, ServicePricing]
    unavailable_services: set[str]

    def __post_init__(self):
        """
        Check data sanity
        """
        if self.code not in REGION_CODES.keys():
            raise ValueError(f'Region "{self.code}" is not supported')

    def compare(self, other: RegionPricing, instance_replacer: InstanceReplacer | None,
                conversion_context: ConversionContext | None = None) -> RegionMapping:
        """
        Return the mapping between services in regions
        """
        return RegionMapping(source_region=self,
                             target_region=other,
                             unavailable_services={s for s in self.services if s in other.unavailable_services},
                             services={code: self.services[code].map(other.services[code], instance_replacer)
                                       for code in self.services if code in other.services},
                             instance_replacer=instance_replacer,
                             conversion_context=conversion_context)

    def to_dict(self):
        """
        Return a dictionary with the information from the object
        """
        return {'region_code': self.code,
                'effective_date': self.effective_date.isoformat(),
                'services': {code: service.to_dict() for code, service in self.services.items()},
                'unavailable_services': list(self.unavailable_services)}

    @classmethod
    def from_dict(cls, data: dict):
        """
        Create a ServicePricing object from a dictionary
        """
        return cls(code=data['region_code'],
                   effective_date=date.fromisoformat(data['effective_date']),
                   services={code: ServicePricing.from_dict(service) for code, service in data['services'].items()},
                   unavailable_services=set(data['unavailable_services']))
