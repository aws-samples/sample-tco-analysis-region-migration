import os
import boto3
import pandas as pd
from datetime import datetime


def get_spot_pricing(instance_types: list[str],
                     start_date: datetime, end_date: datetime,
                     product_description: str = 'Linux/UNIX',
                     region: str = 'eu-south-2') -> pd.DataFrame:
    """
    Get the maximum, minimum and mean spot price for the given

    Parameters
    ----------
    region: AWS region to check spot pricing for
    instance_types : List of instance types
    start_date : Start date
    end_date: End date
    product_description: Spot operating system

    Returns
    -------
    Dataframe with minimum, median, mean and maximum spot prices for the given region and period
    """
    # Get the spot pricing, handling pagination
    if region in ('eusc-de-east-1',):
        session = boto3.Session(region_name='eusc-de-east-1',
                                profile_name=os.environ.get('EUSC_PROFILE_NAME'))
        client = session.client('ec2')
    else:
        client = boto3.client('ec2', region_name=region)
    history = []
    for page in client.get_paginator('describe_spot_price_history').paginate(DryRun=False,
                                                                             StartTime=start_date,
                                                                             EndTime=end_date,
                                                                             ProductDescriptions=[product_description],
                                                                             InstanceTypes=instance_types):
        history += page['SpotPriceHistory']

    price_history = pd.DataFrame(history)
    if price_history.shape[0] == 0:
        return pd.DataFrame(columns=['min', 'median', 'mean', 'max'])
    price_history['SpotPrice'] = price_history['SpotPrice'].astype(float)
    df = price_history[['InstanceType', 'SpotPrice']].groupby('InstanceType').min()
    df['median'] = price_history[['InstanceType', 'SpotPrice']].groupby('InstanceType').median()
    df['mean'] = price_history[['InstanceType', 'SpotPrice']].groupby('InstanceType').mean()
    df['max'] = price_history[['InstanceType', 'SpotPrice']].groupby('InstanceType').max()

    return df.rename(columns={'SpotPrice': 'min'})
