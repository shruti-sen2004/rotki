import binascii
import hashlib
import hmac
import json
import logging
import time
from base64 import b64decode, b64encode
from collections import defaultdict
from collections.abc import Iterator
from contextlib import suppress
from http import HTTPStatus
from json.decoder import JSONDecodeError
from typing import TYPE_CHECKING, Any, Literal, Optional, Union
from urllib.parse import urlencode

import gevent
import requests

from rotkehlchen.accounting.structures.balance import Balance
from rotkehlchen.assets.asset import AssetWithOracles
from rotkehlchen.assets.converters import asset_from_coinbasepro
from rotkehlchen.assets.types import AssetType
from rotkehlchen.constants import ZERO
from rotkehlchen.constants.assets import A_ETH
from rotkehlchen.db.settings import CachedSettings
from rotkehlchen.errors.asset import UnknownAsset, UnprocessableTradePair, UnsupportedAsset
from rotkehlchen.errors.misc import RemoteError
from rotkehlchen.errors.serialization import DeserializationError
from rotkehlchen.exchanges.data_structures import AssetMovement, MarginPosition, Trade
from rotkehlchen.exchanges.exchange import ExchangeInterface, ExchangeQueryBalances
from rotkehlchen.history.deserialization import deserialize_price
from rotkehlchen.inquirer import Inquirer
from rotkehlchen.logging import RotkehlchenLogsAdapter
from rotkehlchen.serialization.deserialize import (
    deserialize_asset_amount,
    deserialize_asset_amount_force_positive,
    deserialize_asset_movement_category,
    deserialize_fee,
    deserialize_timestamp_from_date,
)
from rotkehlchen.types import (
    ApiKey,
    ApiSecret,
    AssetMovementCategory,
    ExchangeAuthCredentials,
    Fee,
    Location,
    Timestamp,
    TradeType,
)
from rotkehlchen.user_messages import MessagesAggregator
from rotkehlchen.utils.mixins.cacheable import cache_response_timewise
from rotkehlchen.utils.mixins.lockable import protect_with_lock
from rotkehlchen.utils.serialization import jsonloads_dict, jsonloads_list

if TYPE_CHECKING:
    from rotkehlchen.accounting.structures.base import HistoryEvent
    from rotkehlchen.db.dbhandler import DBHandler

logger = logging.getLogger(__name__)
log = RotkehlchenLogsAdapter(logger)


COINBASEPRO_PAGINATION_LIMIT = 100  # default + max limit


def coinbasepro_to_worldpair(product: str) -> tuple[AssetWithOracles, AssetWithOracles]:
    """Turns a coinbasepro product into our base/quote assets

    - Can raise UnprocessableTradePair if product is in unexpected format
    - Case raise UnknownAsset if any of the pair assets are not known to rotki
    """
    parts = product.split('-')
    if len(parts) != 2:
        raise UnprocessableTradePair(product)

    base_asset = asset_from_coinbasepro(parts[0])
    quote_asset = asset_from_coinbasepro(parts[1])

    return base_asset, quote_asset


class CoinbaseProPermissionError(Exception):
    pass


def coinbasepro_deserialize_timestamp(entry: dict[str, Any], key: str) -> Timestamp:
    """Deserialize a timestamp from coinbasepro

    In case of an error raises:
    - KeyError
    - DeserializationError
    """
    raw_time = entry[key]
    if raw_time.endswith('+00'):  # proper iso8601 needs + 00:00 for timezone
        raw_time = raw_time.replace('+00', '+00:00')
    timestamp = deserialize_timestamp_from_date(raw_time, 'iso8601', 'coinbasepro')
    return timestamp


class Coinbasepro(ExchangeInterface):

    def __init__(
            self,
            name: str,
            api_key: ApiKey,
            secret: ApiSecret,
            database: 'DBHandler',
            msg_aggregator: MessagesAggregator,
            passphrase: str,
    ):
        super().__init__(
            name=name,
            location=Location.COINBASEPRO,
            api_key=api_key,
            secret=secret,
            database=database,
        )
        self.base_uri = 'https://api.pro.coinbase.com'
        self.msg_aggregator = msg_aggregator
        self.account_to_currency: Optional[dict[str, AssetWithOracles]] = None
        self.available_products = {0}

        self.session.headers.update({
            'Content-Type': 'Application/JSON',
            'CB-ACCESS-KEY': self.api_key,
            'CB-ACCESS-PASSPHRASE': passphrase,
        })

    def update_passphrase(self, new_passphrase: str) -> None:
        self.session.headers.update({'CB-ACCESS-PASSPHRASE': new_passphrase})

    def edit_exchange_credentials(self, credentials: ExchangeAuthCredentials) -> bool:
        changed = super().edit_exchange_credentials(credentials)
        if credentials.api_key is not None:
            self.session.headers.update({'CB-ACCESS-KEY': self.api_key})
        if credentials.passphrase is not None:
            self.update_passphrase(credentials.passphrase)

        return changed

    def validate_api_key(self) -> tuple[bool, str]:
        """Validates that the Coinbase Pro API key is good for usage in rotki

        Makes sure that the following permissions are given to the key:
        - View
        """
        try:
            self._api_query('accounts')
        except CoinbaseProPermissionError:
            msg = (
                'Provided Coinbase Pro API key needs to have "View" permission activated. '
                'Please log into your coinbase account and create a key with '
                'the required permissions.'
            )
            return False, msg
        except RemoteError as e:
            error = str(e)
            if 'Invalid Passphrase' in error:
                msg = (
                    'The passphrase for the given API key does not match. Please '
                    'create a key with the preset passphrase "rotki"'
                )
                return False, msg

            return False, error

        return True, ''

    def first_connection(self) -> None:
        if self.first_connection_made:
            return

        products_response, _ = self._api_query('products')
        self.available_products = {x['id'] for x in products_response}
        self.first_connection_made = True

    def _api_query(
            self,
            endpoint: str,
            request_method: Literal['GET', 'POST'] = 'GET',
            options: Optional[dict[str, Any]] = None,
            query_options: Optional[dict[str, Any]] = None,
    ) -> tuple[list[Any], Optional[str]]:
        """Performs a coinbase PRO API Query for endpoint

        You can optionally provide extra arguments to the endpoint via the options argument.

        Returns a tuple of the result and optional pagination cursor.

        Raises RemoteError if something went wrong with connecting or reading from the exchange
        Raises CoinbaseProPermissionError if the API Key does not have sufficient
        permissions for the endpoint
        """
        request_url = f'/{endpoint}'

        timestamp = str(int(time.time()))
        if options:
            stringified_options = json.dumps(options, separators=(',', ':'))
        else:
            stringified_options = ''
            options = {}

        if query_options:
            request_url += '?' + urlencode(query_options)

        message = timestamp + request_method + request_url + stringified_options

        if 'products' not in endpoint:
            try:
                signature = hmac.new(
                    b64decode(self.secret),
                    message.encode(),
                    hashlib.sha256,
                ).digest()
            except binascii.Error as e:
                raise RemoteError('Provided API Secret is invalid') from e

            self.session.headers.update({
                'CB-ACCESS-SIGN': b64encode(signature).decode('utf-8'),
                'CB-ACCESS-TIMESTAMP': timestamp,
            })

        retries_left = CachedSettings().get_query_retry_limit()
        while retries_left > 0:
            log.debug(
                'Coinbase Pro API query',
                request_method=request_method,
                request_url=request_url,
                options=options,
            )
            full_url = self.base_uri + request_url
            try:
                response = self.session.request(
                    request_method.lower(),
                    full_url,
                    data=stringified_options,
                    timeout=CachedSettings().get_timeout_tuple(),
                )
            except requests.exceptions.RequestException as e:
                raise RemoteError(
                    f'Coinbase Pro {request_method} query at '
                    f'{full_url} connection error: {e!s}',
                ) from e

            if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
                # Backoff a bit by sleeping. Sleep more, the more retries have been made
                backoff_secs = CachedSettings().get_query_retry_limit() / retries_left
                log.debug(f'Backing off coinbase pro api query for {backoff_secs} secs')
                gevent.sleep(backoff_secs)
                retries_left -= 1
            else:
                # get out of the retry loop, we did not get 429 complaint
                break

        json_ret: Union[list[Any], dict[str, Any]]
        if response.status_code == HTTPStatus.BAD_REQUEST:
            json_ret = jsonloads_dict(response.text)
            if json_ret['message'] == 'invalid signature':
                raise CoinbaseProPermissionError(
                    f'While doing {request_method} at {endpoint} endpoint the API secret '
                    f'created an invalid signature.',
                )
            # else do nothing and a generic remote error will be thrown below

        elif response.status_code == HTTPStatus.FORBIDDEN:
            raise CoinbaseProPermissionError(
                f'API key does not have permission for {endpoint}',
            )

        if response.status_code != HTTPStatus.OK:
            raise RemoteError(
                f'Coinbase Pro {request_method} query at {full_url} responded with error '
                f'status code: {response.status_code} and text: {response.text}',
            )

        try:
            json_ret = jsonloads_list(response.text)
        except JSONDecodeError as e:
            raise RemoteError(
                f'Coinbase Pro {request_method} query at {full_url} '
                f'returned invalid JSON response: {response.text}',
            ) from e

        return json_ret, response.headers.get('cb-after', None)

    def create_or_return_account_to_currency_map(self) -> dict[str, AssetWithOracles]:
        if self.account_to_currency is not None:
            return self.account_to_currency

        accounts, _ = self._api_query('accounts')
        self.account_to_currency = {}
        for account in accounts:
            try:
                asset = asset_from_coinbasepro(account['currency'])
                self.account_to_currency[account['id']] = asset
            except UnsupportedAsset as e:
                self.msg_aggregator.add_warning(
                    f'Found coinbase pro account with unsupported asset '
                    f'{e.identifier}. Ignoring it.',
                )
                continue
            except UnknownAsset as e:
                self.msg_aggregator.add_warning(
                    f'Found coinbase pro account result with unknown asset '
                    f'{e.identifier}. Ignoring it.',
                )
                continue
            except KeyError as e:
                self.msg_aggregator.add_warning(
                    f'Found coinbase pro account entry with missing {e!s} field. '
                    f'Ignoring it',
                )
                continue

        return self.account_to_currency

    @protect_with_lock()
    @cache_response_timewise()
    def query_balances(self) -> ExchangeQueryBalances:
        try:
            accounts, _ = self._api_query('accounts')
        except (CoinbaseProPermissionError, RemoteError) as e:
            msg = f'Coinbase Pro API request failed. {e!s}'
            log.error(msg)
            return None, msg

        assets_balance: defaultdict[AssetWithOracles, Balance] = defaultdict(Balance)
        for account in accounts:
            try:
                amount = deserialize_asset_amount(account['balance'])
                # ignore empty balances. Coinbase returns zero balances for everything
                # a user does not own
                if amount == ZERO:
                    continue

                asset = asset_from_coinbasepro(account['currency'])
                try:
                    usd_price = Inquirer().find_usd_price(asset=asset)
                except RemoteError as e:
                    self.msg_aggregator.add_error(
                        f'Error processing coinbasepro balance result due to inability to '
                        f'query USD price: {e!s}. Skipping balance entry',
                    )
                    continue

                assets_balance[asset] += Balance(
                    amount=amount,
                    usd_value=amount * usd_price,
                )
            except UnknownAsset as e:
                self.msg_aggregator.add_warning(
                    f'Found coinbase pro balance result with unknown asset '
                    f'{e.identifier}. Ignoring it.',
                )
                continue
            except UnsupportedAsset as e:
                self.msg_aggregator.add_warning(
                    f'Found coinbase pro balance result with unsupported asset '
                    f'{e.identifier}. Ignoring it.',
                )
                continue
            except (DeserializationError, KeyError) as e:
                msg = str(e)
                if isinstance(e, KeyError):
                    msg = f'Missing key entry for {msg}.'
                self.msg_aggregator.add_error(
                    'Error processing a coinbase pro account balance. Check logs '
                    'for details. Ignoring it.',
                )
                log.error(
                    'Error processing a coinbase pro account balance',
                    account_balance=account,
                    error=msg,
                )
                continue

        return dict(assets_balance), ''

    def _paginated_query(
            self,
            endpoint: str,
            query_options: Optional[dict[str, Any]] = None,
            limit: int = COINBASEPRO_PAGINATION_LIMIT,
    ) -> Iterator[list[dict[str, Any]]]:
        if query_options is None:
            query_options = {}
        query_options['limit'] = limit
        while True:
            result, after_cursor = self._api_query(endpoint=endpoint, query_options=query_options)
            yield result
            if after_cursor is None or len(result) < limit:
                break

            query_options['after'] = after_cursor

    def query_online_deposits_withdrawals(
            self,
            start_ts: Timestamp,
            end_ts: Timestamp,
    ) -> list[AssetMovement]:
        """Queries coinbase pro for asset movements"""
        log.debug('Query coinbasepro asset movements', start_ts=start_ts, end_ts=end_ts)
        movements = []
        raw_movements = []
        for batch in self._paginated_query(
            endpoint='transfers',
            query_options={'type': 'withdraw'},
        ):
            raw_movements.extend(batch)
        for batch in self._paginated_query(
            endpoint='transfers',
            query_options={'type': 'deposit'},
        ):
            raw_movements.extend(batch)

        account_to_currency = self.create_or_return_account_to_currency_map()
        for entry in raw_movements:
            try:
                # Check if the transaction has not been completed. If so it should be skipped
                if entry.get('completed_at', None) is None:
                    log.warning(
                        f'Skipping coinbase pro deposit/withdrawal '
                        f'due to not having been completed: {entry}',
                    )
                    continue

                timestamp = coinbasepro_deserialize_timestamp(entry, 'completed_at')
                if timestamp < start_ts or timestamp > end_ts:
                    continue

                category = deserialize_asset_movement_category(entry['type'])
                asset = account_to_currency.get(entry['account_id'], None)
                if asset is None:
                    log.warning(
                        f'Skipping coinbase pro asset_movement {entry} due to '
                        f'inability to match account id to an asset',
                    )
                    continue

                address = None
                transaction_id = None
                fee = Fee(ZERO)
                if category == AssetMovementCategory.DEPOSIT:
                    with suppress(KeyError):
                        address = entry['details']['crypto_address']
                        transaction_id = entry['details']['crypto_transaction_hash']
                else:  # withdrawal
                    with suppress(KeyError):
                        address = entry['details']['sent_to_address']
                        transaction_id = entry['details']['crypto_transaction_hash']
                        fee = deserialize_fee(entry['details']['fee'])

                if transaction_id and (asset == A_ETH or asset.asset_type == AssetType.EVM_TOKEN):
                    transaction_id = '0x' + transaction_id

                movements.append(AssetMovement(
                    location=Location.COINBASEPRO,
                    category=category,
                    address=address,
                    transaction_id=transaction_id,
                    timestamp=timestamp,
                    asset=asset,
                    amount=deserialize_asset_amount_force_positive(entry['amount']),
                    fee_asset=asset,
                    fee=fee,
                    link=str(entry['id']),
                ))
            except UnknownAsset as e:
                self.msg_aggregator.add_warning(
                    f'Found unknown Coinbasepro asset {e.identifier}. '
                    f'Ignoring its deposit/withdrawal.',
                )
                continue
            except (DeserializationError, KeyError) as e:
                msg = str(e)
                if isinstance(e, KeyError):
                    msg = f'Missing key entry for {msg}.'
                self.msg_aggregator.add_error(
                    'Failed to deserialize a Coinbasepro deposit/withdrawal. '
                    'Check logs for details. Ignoring it.',
                )
                log.error(
                    'Error processing a coinbasepro  deposit/withdrawal.',
                    raw_asset_movement=entry,
                    error=msg,
                )
                continue

        return movements

    def query_online_trade_history(
            self,
            start_ts: Timestamp,
            end_ts: Timestamp,
    ) -> tuple[list[Trade], tuple[Timestamp, Timestamp]]:
        """Queries coinbase pro for trades"""
        log.debug('Query coinbasepro trade history', start_ts=start_ts, end_ts=end_ts)
        self.first_connection()

        trades = []
        # first get all orders, to see which product ids we need to query fills for
        orders = []
        for batch in self._paginated_query(
            endpoint='orders',
            query_options={'status': 'done'},
        ):
            orders.extend(batch)

        queried_product_ids = set()

        for order_entry in orders:
            product_id = order_entry.get('product_id', None)
            if product_id is None:
                msg = (
                    'Skipping coinbasepro trade since it lacks a product_id. '
                    'Check logs for details'
                )
                self.msg_aggregator.add_error(msg)
                log.error(
                    'Error processing a coinbasepro order.',
                    raw_trade=order_entry,
                    error=msg,
                )
                continue

            if product_id in queried_product_ids or product_id not in self.available_products:
                continue  # already queried this product id or delisted product id

            # Now let's get all fills for this product id
            queried_product_ids.add(product_id)
            fills = []
            for batch in self._paginated_query(
                    endpoint='fills',
                    query_options={'product_id': product_id},
            ):
                fills.extend(batch)

            try:
                base_asset, quote_asset = coinbasepro_to_worldpair(product_id)
            except UnprocessableTradePair as e:
                self.msg_aggregator.add_warning(
                    f'Found unprocessable Coinbasepro pair {e.pair}. Ignoring the trade.',
                )
                continue
            except UnknownAsset as e:
                self.msg_aggregator.add_warning(
                    f'Found unknown Coinbasepro asset {e.identifier}. '
                    f'Ignoring the trade.',
                )
                continue

            for fill_entry in fills:
                try:
                    timestamp = coinbasepro_deserialize_timestamp(fill_entry, 'created_at')
                    if timestamp < start_ts or timestamp > end_ts:
                        continue

                    # Fee currency seems to always be quote asset
                    # https://github.com/ccxt/ccxt/blob/ddf3a15cbff01541f0b37c35891aa143bb7f9d7b/python/ccxt/coinbasepro.py#L724  # noqa: E501
                    trades.append(Trade(
                        timestamp=timestamp,
                        location=Location.COINBASEPRO,
                        base_asset=base_asset,
                        quote_asset=quote_asset,
                        trade_type=TradeType.deserialize(fill_entry['side']),
                        amount=deserialize_asset_amount(fill_entry['size']),
                        rate=deserialize_price(fill_entry['price']),
                        fee=deserialize_fee(fill_entry['fee']),
                        fee_currency=quote_asset,
                        link=str(fill_entry['trade_id']) + '_' + fill_entry['order_id'],
                        notes='',
                    ))
                except UnprocessableTradePair as e:
                    self.msg_aggregator.add_warning(
                        f'Found unprocessable Coinbasepro pair {e.pair}. Ignoring the trade.',
                    )
                    continue
                except UnknownAsset as e:
                    self.msg_aggregator.add_warning(
                        f'Found unknown Coinbasepro asset {e.identifier}. '
                        f'Ignoring the trade.',
                    )
                    continue
                except (DeserializationError, KeyError) as e:
                    msg = str(e)
                    if isinstance(e, KeyError):
                        msg = f'Missing key entry for {msg}.'
                    self.msg_aggregator.add_error(
                        'Failed to deserialize a coinbasepro trade. '
                        'Check logs for details. Ignoring it.',
                    )
                    log.error(
                        'Error processing a coinbasepro fill.',
                        raw_trade=fill_entry,
                        error=msg,
                    )
                    continue

        return trades, (start_ts, end_ts)

    def query_online_margin_history(
            self,
            start_ts: Timestamp,  # pylint: disable=unused-argument
            end_ts: Timestamp,
    ) -> list[MarginPosition]:
        return []  # noop for coinbasepro

    def query_online_income_loss_expense(
            self,
            start_ts: Timestamp,  # pylint: disable=unused-argument
            end_ts: Timestamp,
    ) -> list['HistoryEvent']:
        return []  # noop for coinbasepro
