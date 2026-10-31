#!/usr/bin/env python3
"""Check StrikerRx Snowflake key-pair authentication without modifying data."""
import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key', type=Path, default=Path.home() / '.ssh/strikeradmin_rsa.p8')
    args = parser.parse_args()
    try:
        import snowflake.connector
    except ImportError:
        raise SystemExit('Run with a Python environment that has snowflake-connector-python installed.')
    if not args.key.is_file():
        raise SystemExit(f'Private key file not found: {args.key}')
    try:
        with snowflake.connector.connect(
            account='HDSBMSO-NNC01524',
            user='SNOWFLAKEADMIN@STRIKERRX.COM',
            warehouse='DEVELOPER_WH',
            database='RAW_PRD',
            schema='STRIKER_WEBSITE',
            authenticator='SNOWFLAKE_JWT',
            private_key_file=str(args.key),
            login_timeout=15,
            network_timeout=15,
            socket_timeout=10,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute('SELECT CURRENT_ACCOUNT(), CURRENT_USER(), CURRENT_ROLE(), '
                               'CURRENT_WAREHOUSE(), CURRENT_DATABASE(), CURRENT_SCHEMA()')
                columns = ('account', 'user', 'role', 'warehouse', 'database', 'schema')
                for name, value in zip(columns, cursor.fetchone()):
                    print(f'{name}: {value}')
                print('Key-pair authentication succeeded. No data or schema changes made.')
    except snowflake.connector.Error as error:
        raise SystemExit(f'Snowflake connection failed ({error.errno}): {error.msg}') from None


if __name__ == '__main__':
    main()
