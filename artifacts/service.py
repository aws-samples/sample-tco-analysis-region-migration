import numpy as np
import pandas as pd
from itertools import product
from datetime import datetime
from dataclasses import dataclass
from misc.sku_normalization import REGION_CODES
from misc.instance_replacement import InstanceReplacer


@dataclass
class ServiceRateCodeMapping:
    """
    Report containing information regarding the difference in features
    for a particular service
    """
    service: str
    reference_service: 'ServicePricing'
    target_service: 'ServicePricing'
    mapped: pd.DataFrame
    unmapped: pd.DataFrame


@dataclass
class ServicePricing:
    """
    Report containing feature availability info for a particular service
    """
    code: str
    skus: pd.DataFrame
    region_code: str
    publication_date: datetime

    def __post_init__(self):
        self.skus['operation'] = self.skus['operation'].replace({np.nan: None})

    def map(self, other: 'ServicePricing', instance_replacer: 'InstanceReplacer | None') -> ServiceRateCodeMapping:
        """
        Return a report with the features from this ServiceReport that map directly to those in other

        Parameters
        ----------
        other : Service report for another region or another point in time
        instance_replacer : Instance replacement profile, if available

        Returns
        -------
        A report with the differences between two Services
        """
        if self.code != other.code:
            raise ValueError(f'Cannot compare {self.code} pricing in {self.region_code} with '
                             f'{other.code} in {other.region_code}')

        # Service-specific tuning
        if self.code == 'AWSDataTransfer':
            # Remove inter-region traffic between source and destination regions
            inter_region_mask = (self.skus['usageType'].str.contains('AWS-In') |
                                 self.skus['usageType'].str.contains('AWS-Out'))
            mask = pd.Series([False] * self.skus.shape[0], index=self.skus.index)
            for combo in product(REGION_CODES[self.region_code], REGION_CODES[other.region_code]):
                prefix = f'{combo[0]}{combo[1]}AWS-'
                mask |= self.skus.loc[inter_region_mask, 'usageType'].str.startswith(prefix)
            for combo in product(REGION_CODES[other.region_code], REGION_CODES[self.region_code]):
                prefix = f'{combo[0]}{combo[1]}AWS-'
                mask |= self.skus.loc[inter_region_mask, 'usageType'].str.startswith(prefix)
            self.skus.drop(self.skus[mask].index, inplace=True)

        # Prepare source DataFrame (with instance replacements if needed)
        if isinstance(instance_replacer, InstanceReplacer):
            src_df = self.skus.copy()
            src_df = instance_replacer.apply_replacements(src_df,
                                                          service_code=self.code,
                                                          instance_type_col='normalizedUsageType')
        else:
            src_df = self.skus

        # Region-specific matching: normalizedRateCode for commercial regions,
        # Column-based for EUSC (cross-partition)
        if 'eusc' in self.region_code or 'eusc' in other.region_code:
            merge_keys = ['serviceCode', 'normalizedUsageType', 'operation',
                          'TermType', 'Unit', 'StartingRange', 'EndingRange']
            merge_keys += [col for col in ('LeaseContractLength', 'PurchaseOption', 'Instance Type')
                           if col in src_df.columns]
        else:
            merge_keys = ['serviceCode', 'normalizedUsageType', 'operation', 'normalizedRateCode']

        output_cols = ['SKU - origin', 'regionCode - origin', 'SKU - target',
                       'regionCode - target', 'serviceCode', 'usageType - origin', 'usageType - target',
                       'normalizedUsageType', 'operation', 'normalizedRateCode',
                       'rateCode - origin', 'rateCode - target', 'priceDescription', 'TermType', 'Unit',
                       'StartingRange', 'EndingRange', 'currency', 'pricePerUnit - origin', 'pricePerUnit - target',]

        def _normalize_merge_result(merged):
            """Consolidate suffixed columns from a merge into canonical names."""
            consolidate = {'currency', 'priceDescription', 'normalizedRateCode',
                           'TermType', 'Unit', 'StartingRange', 'EndingRange'}
            for col in consolidate:
                origin_col = f'{col} - origin'
                target_col = f'{col} - target'
                if col not in merge_keys and origin_col in merged.columns:
                    merged[col] = merged[origin_col]
                    merged.drop(columns=[c for c in (origin_col, target_col)
                                         if c in merged.columns],
                                inplace=True, errors='ignore')
                elif col not in merge_keys and target_col in merged.columns:
                    merged.drop(columns=[target_col], inplace=True, errors='ignore')
            return merged

        sku_mapping = src_df.merge(other.skus, how='left', on=merge_keys,
                                   suffixes=(' - origin', ' - target'))
        sku_mapping = _normalize_merge_result(sku_mapping)
        sku_mapping = sku_mapping[output_cols]

        # Compute price ratio: target / origin, handling zero-price cases
        sku_mapping['priceRatio'] = np.where(
            sku_mapping['pricePerUnit - origin'] == 0,
            np.where(sku_mapping['pricePerUnit - target'] == 0, 1.0, np.inf),
            sku_mapping['pricePerUnit - target'] / sku_mapping['pricePerUnit - origin']
        )

        mask = ~sku_mapping['SKU - target'].isna()
        unmapped = (sku_mapping[~mask].
                    drop(columns=[col for col in sku_mapping.columns if col.endswith(' - target')]).
                    rename(columns={col: col[:-9] for col in sku_mapping.columns if col.endswith(' - origin')}))

        return ServiceRateCodeMapping(service=self.code,
                                      reference_service=self,
                                      target_service=other,
                                      mapped=sku_mapping[mask],
                                      unmapped=unmapped)

    def to_dict(self):
        """
        Return a dictionary with the information from the object
        """
        return {'code': self.code,
                'skus': self.skus.to_dict(),
                'region_code': self.region_code,
                'publication_date': self.publication_date.isoformat()}

    @classmethod
    def from_dict(cls, data: dict):
        """
        Create a ServicePricing object from a dictionary
        """
        return cls(code=data['code'],
                   skus=pd.DataFrame.from_dict(data['skus']),
                   region_code=data['region_code'],
                   publication_date=datetime.fromisoformat(data['publication_date']))
