import json
import logging
import pandas as pd


class InstanceReplacer:
    """
    Handles instance type replacements based on a configuration file.
    
    This class reads a JSON configuration file that specifies how instance types
    should be replaced.
    
    The JSON file should have the following structure:
    {
      "AmazonEC2": {
        "m5.large": "m6g.large",
        "m5.xlarge": "m6g.xlarge",
        "m5.2xlarge": "m6g.2xlarge",
        "c5.large": "c6g.large",
        "c5.xlarge": "c6g.xlarge",
        "c5.2xlarge": "c6g.2xlarge",
        "r5.large": "r6g.large",
        "r5.xlarge": "r6g.xlarge",
        "r5.2xlarge": "r6g.2xlarge"
      },
      "AmazonRDS": {
        "db.m5.large": "db.m6g.large",
        "db.m5.xlarge": "db.m6g.xlarge",
        "db.r5.large": "db.r6g.large",
        "db.r5.xlarge": "db.r6g.xlarge"
      }
    }
    """

    def __init__(self):
        """
        Initialize the InstanceReplacer with an optional configuration file path.
        """
        self.services = {}

    def get_replacement(self, service: str, instance_type: str) -> str:
        """
        Get the replacement instance type for a given service and instance type.

        If there is no replacement, the original instance type will be returned.
        
        Args:
            service: The AWS service code (e.g., 'AmazonEC2')
            instance_type: The original instance type (e.g., 'm5.large')

        Returns:
            The replacement instance type
        """
        return self.services.get(service, {}).get(instance_type) or instance_type

    def apply_replacements(self, df: pd.DataFrame,
                           service_code: str,
                           instance_type_col: str = 'usageType',
                           with_colon: bool = True) -> pd.DataFrame:
        """
        Apply instance replacements to a DataFrame containing service and instance type information.

        Args:
            df: The DataFrame to process
            service_code: The service code to replace
            instance_type_col: The name of the column containing the instance type
            with_colon: Flag indicating whether the instance column contains a colon

        Returns:
            The modified DataFrame with replacement information
        """
        # Skip if no replacements are defined
        if not self.services:
            return df

        # Replace instances as needed, and create a column with a flag for instances that were indeed replaced
        df['Replaced'] = False
        if service_code in self.services:
            # Store the original version of the instances
            original_instances = df[instance_type_col].copy()

            # Replace the instances for each service
            for service_code in self.services:
                if with_colon:
                    mask = (df[instance_type_col].str.split(':', n=1, expand=True)[1].isin(
                        self.services[service_code].keys()))
                else:
                    mask = df[instance_type_col].isin(self.services[service_code].keys())
                for original_instance, dest_instance in self.services[service_code].items():
                    df.loc[mask, instance_type_col] = df.loc[mask, instance_type_col].str.replace(
                        f'{original_instance}',
                        f'{dest_instance}', n=1)

            df['Replaced'] = (~(df[instance_type_col] == original_instances))

        return df

    @classmethod
    def from_config(cls, config: dict):
        """
        Load the replacement configuration from a JSON file.

        Args:
            config: Instance replacement config dict
        """
        replacer = cls()
        try:
            replacer.services = config
            logging.info(f"Loaded instance replacements for {len(replacer.services)} "
                         f"service{'s' if len(replacer.services) > 1 else ''}")

            # Log the number of replacements per service
            for service, config in config.items():
                logging.info(f"  - {service}: {len(config)} instance replacements")

        except (json.JSONDecodeError, FileNotFoundError) as e:
            logging.warning(f"Failed to load instance replacement configuration: {e}")

        return replacer

    def to_dataframe(self) -> pd.DataFrame | None:
        """
        Convert the instance replacement configuration to a pandas DataFrame.

        This method transforms the nested dictionary structure of the replacement config
        into a tabular format with one row per replacement mapping.

        Returns:
            pd.DataFrame: A DataFrame with the following columns:
                - service: The AWS service code (e.g. 'AmazonEC2', 'AmazonRDS')
                - source_instance: The original instance type to be replaced
                - target_instance: The replacement instance type

        Example:
            >>> replacer = InstanceReplacer.from_config(config)
            >>> df = replacer.to_dataframe()
            >>> print(df)
               service source_instance target_instance
            0  AmazonEC2      m5.large      m6g.large
            1  AmazonEC2     m5.xlarge     m6g.xlarge
        """
        if not self.services:
            return None

        rows = []
        for service, replacements in self.services.items():
            for source, target in replacements.items():
                rows.append({'service': service,
                             'source_instance': source,
                             'target_instance': target})
        return pd.DataFrame(rows)
