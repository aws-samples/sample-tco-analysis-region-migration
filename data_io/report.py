from __future__ import annotations

import numpy as np
import pandas as pd
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING
from artifacts.region import RegionMapping
from data_io.storage_wrapper import StorageWrapper
from misc.instance_replacement import InstanceReplacer

if TYPE_CHECKING:
    from misc.currency import ConversionContext

CURRENCY_SYMBOLS = {
    'USD': '$',
    'EUR': '€',
}


def add_sku_annotations(df: pd.DataFrame, svc: str, region_code: str,
                        annotations_dir: Path = Path('data/sku_annotations')):
    """
    Add an "Annotation" column to the given data with user-provided SKU annotations
    """
    annotations_file = (annotations_dir / region_code / f'{svc}.csv')
    if annotations_file.is_file():
        annotations = pd.read_csv(annotations_file)
        annotations['operation'] = annotations['operation'].replace({np.nan: None})
        if annotations.shape[0] > 0:
            df = df.merge(annotations,
                          how='left',
                          on=['normalizedUsageType', 'operation', 'normalizedRateCode'])
    if 'Annotation' not in df.columns:
        df.insert(df.shape[1], 'Annotation', '')

    return df


def _apply_money_formats(sheet, df: pd.DataFrame, money_format, original_money_format,
                         conversion_context: ConversionContext | None = None):
    """
    Apply currency-aware money formats to columns in a worksheet based on column names.

    Columns containing original (unconverted) prices get ``original_money_format``;
    all other monetary columns get ``money_format``.
    """
    # Columns that hold monetary values
    money_columns = {'pricePerUnit - origin', 'pricePerUnit - target',
                     'cost - origin', 'cost - target', 'Effective Cost'}

    # Build the set of original-currency column names when conversion is active
    original_currency_columns: set[str] = set()
    if conversion_context is not None and conversion_context.converted and conversion_context.dest_currency:
        original_currency_columns.add(f'pricePerUnit - target ({conversion_context.dest_currency})')

    for idx, col in enumerate(df.columns):
        if col in original_currency_columns:
            sheet.set_column(idx, idx, None, original_money_format)
        elif col in money_columns:
            sheet.set_column(idx, idx, None, money_format)


def write_excel_report(region_map: RegionMapping, cur_data: pd.DataFrame, output_dir: str,
                       detailed_feature_mapping: bool = False,
                       annotations_dir: Path = Path('data/sku_annotations'),
                       conversion_context: ConversionContext | None = None):
    """
    Write the Excel version of the workload compatiblity report
    """
    # Create the output directory, if needed
    Path(f'{output_dir}').mkdir(parents=True, exist_ok=True)

    # Write the detailed Excel report
    excel_buffer = BytesIO()
    with pd.ExcelWriter(excel_buffer, engine='xlsxwriter') as writer:
        workbook = writer.book

        # Determine currency-aware formats
        if conversion_context is not None and conversion_context.converted:
            src_symbol = CURRENCY_SYMBOLS.get(conversion_context.src_currency, '$')
            dest_symbol = CURRENCY_SYMBOLS.get(conversion_context.dest_currency, '$')
            money_format = workbook.add_format({'num_format': f'_-"{src_symbol}"* #,##0.00'})
            original_money_format = workbook.add_format({'num_format': f'_-"{dest_symbol}"* #,##0.00'})
        else:
            money_format = workbook.add_format({'num_format': '_-"$"* #,##0.00'})
            original_money_format = money_format
        if cur_data is not None:
            # Write a copy of the CUR data to the Excel file
            cur_data.to_excel(writer, sheet_name=f'CUR Data - origin', index=False)
            writer.sheets['CUR Data - origin'].set_column(10, 10, None, money_format)
            # Create a sheet with instance replacement information if applicable
            if region_map.instance_replacer is not None:
                replacement_summary = region_map.instance_replacer.to_dataframe()
                if replacement_summary is not None:
                    replacement_summary.to_excel(writer, sheet_name='Instance Replacements', index=False)
                    writer.sheets['Instance Replacements'].set_column(4, 5, None, money_format)
            # Map consumption to the target region, report that
            consumption = region_map.map_consumption(cur_data)
            # Write the sheet with the summary of mappable features
            data = {svc: [cur_data.loc[cur_data['Service'] == svc, 'Effective Cost'].sum(),
                          consumption.services[svc].mapped['cost - origin'].sum(),
                          consumption.services[svc].mapped['cost - target'].sum()]
                    for svc in sorted(consumption.services.keys())}
            pd.DataFrame.from_dict(data, orient='index',
                                   columns=['Total consumption',
                                            'Mappable cost',
                                            'Translated cost']).to_excel(writer, sheet_name='Services', index=True)
            writer.sheets['Services'].set_column(1, 3, None, money_format)
            # Write the sheet with the spend in unavailable services
            data = {svc: [cur_data.loc[cur_data['Service'] == svc, 'Effective Cost'].sum()]
                    for svc in sorted(cur_data['Service'].unique())
                    if svc in region_map.unavailable_services}
            pd.DataFrame.from_dict(data, orient='index',
                                   columns=['Total consumption']).to_excel(writer,
                                                                           sheet_name='Unavailable services',
                                                                           index=True)
            writer.sheets['Unavailable services'].set_column(1, 1, None, money_format)
            # Write the SKU mapping sheet
            mapped_data = pd.concat([consumption.services[svc].mapped
                                     for svc in sorted(consumption.services.keys())])

            # Write the full mapping data
            mapped_data.to_excel(writer, sheet_name='CUR data - mapping', index=False)
            _apply_money_formats(writer.sheets['CUR data - mapping'], mapped_data,
                                 money_format, original_money_format, conversion_context)
            # Write one sheet per used service with missing SKUs
            for svc in sorted(consumption.services.keys()):
                df = consumption.services[svc].unmapped
                if df.shape[0] == 0:
                    continue

                # Add SKU annotations if available
                df = add_sku_annotations(df, svc, region_map.target_region.code,
                                         annotations_dir=annotations_dir)
                sheet_name = f'{svc} unav SKUs'[:31]
                drop_df = df.drop(columns=['normalizedUsageType', 'normalizedRateCode', 'TermType', 'Unit'],
                                  errors='ignore')
                drop_df.to_excel(writer, sheet_name=sheet_name, index=False)
                _apply_money_formats(writer.sheets[sheet_name], drop_df,
                                     money_format, original_money_format, conversion_context)

        else:
            pd.Series(sorted(region_map.services.keys()), name='Services').to_excel(writer,
                                                                                    sheet_name='Services',
                                                                                    index=False)
            pd.Series(sorted(region_map.unavailable_services)).to_excel(writer,
                                                                        sheet_name='Unavailable services',
                                                                        index=False)
            if detailed_feature_mapping:
                # Write the SKU mapping sheet
                mapped_data = pd.concat([region_map.services[svc].mapped for svc in sorted(region_map.services.keys())])

                # Write the full mapping data
                display_data = mapped_data.drop(columns=['normalizedUsageType',
                                                         'normalizedRateCode',
                                                         'SKU - origin',
                                                         'SKU - target'])
                display_data.to_excel(writer,
                                      sheet_name='SKU mapping',
                                      index=False)
                _apply_money_formats(writer.sheets['SKU mapping'], display_data,
                                     money_format, original_money_format, conversion_context)
            for svc, mapping in region_map.services.items():
                # Skip empty services or DirectConnect unless the source & target regions are the same ones
                if ((mapping.unmapped.shape[0] == 0) or
                        ((svc == 'AWSDirectConnect') and
                         (region_map.source_region.code != region_map.target_region.code))):
                    continue

                # Add SKU annotations if available
                df = add_sku_annotations(mapping.unmapped, svc, region_map.target_region.code,
                                         annotations_dir=annotations_dir)
                df.drop(columns=['normalizedUsageType',
                                 'normalizedRateCode',
                                 'TermType', 'Unit'],
                        errors='ignore').to_excel(writer,
                                                  sheet_name=f'{svc} unav SKUs'[:31],
                                                  index=False)

        # Autofit the columns
        for sheet_name in writer.sheets:
            writer.sheets[sheet_name].autofit()

    output_writer = StorageWrapper(output_dir)
    output_writer.write_bytes(f'{region_map.target_region.code}.xlsx',
                              excel_buffer.getvalue())


