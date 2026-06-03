import logging
import pandas as pd
from pathlib import Path
from misc.sku_normalization import normalize_usage_types


def extract_sku_annotations(excel_path: Path, region_code: str,
                            output_dir: Path = Path('data/sku_annotations'),
                            exclude_services: set[str] = {'AWSDirectConnect'}):
    """
    Extract SKU annotations from an Excel report and update CSV files.

    Annotations let users link unavailable SKUs to components or services in
    their own applications (e.g. "logging component in App-X").

    Parameters
    ----------
    excel_path : Path
        Path to the Excel report file
    region_code : str
        AWS region code (e.g., 'eu-south-2')
    output_dir : Path
        Base directory for SKU annotations (default: 'data/sku_annotations')
    exclude_services : set[str]
        Services to exclude from extraction (default: {'AWSDirectConnect'})
    """
    xl = pd.ExcelFile(excel_path, engine='calamine')
    sheet_names = [n for n in xl.sheet_names if 'unav' in n]
    svc_names = {n.split(' ')[0]: n for n in sheet_names}

    region_output_dir = output_dir / region_code
    region_output_dir.mkdir(parents=True, exist_ok=True)

    cols = ['normalizedUsageType', 'operation', 'normalizedRateCode', 'Annotation']

    for svc in svc_names:
        if svc in exclude_services:
            continue

        logging.info(f'Processing {svc}...')
        output_csv = region_output_dir / f'{svc}.csv'
        df = pd.read_excel(xl, sheet_name=svc_names[svc], engine='calamine')

        df['normalizedUsageType'] = df['usageType'].apply(lambda x: normalize_usage_types(x, region_code))
        df['normalizedRateCode'] = df['rateCode'].apply(lambda x: '.'.join(x.split('.')[1:]))
        df.dropna(subset='Annotation', inplace=True)

        if output_csv.is_file():
            df = pd.concat([pd.read_csv(output_csv), df[cols]]).drop_duplicates(
                subset=[c for c in cols if c != 'Annotation'],
                keep='last',
                ignore_index=True
            ).sort_values(by=cols)

        df.to_csv(output_csv, columns=cols, index=False)
        output_csv.chmod(0o600)

    logging.info(f'SKU annotations updated in {region_output_dir}')
