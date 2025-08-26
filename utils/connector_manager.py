import asyncio
import logging
import time
from decimal import Decimal
from typing import Dict, List, Optional

# Create module-specific logger
logger = logging.getLogger(__name__)

from hummingbot.client.config.client_config_map import ClientConfigMap
from hummingbot.client.config.config_crypt import ETHKeyFileSecretManger
from hummingbot.client.config.config_helpers import ClientConfigAdapter, ReadOnlyClientConfigAdapter, get_connector_class
from hummingbot.client.settings import AllConnectorSettings
from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.connector.exchange.paper_trade import create_paper_trade_market
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState
from hummingbot.core.utils.async_utils import safe_ensure_future

from utils.file_system import fs_util
from utils.hummingbot_api_config_adapter import HummingbotAPIConfigAdapter
from utils.security import BackendAPISecurity


class ConnectorManager:
    """
    Manages the creation and caching of exchange connectors.
    Handles connector configuration and initialization.
    This is the single source of truth for all connector instances.
    """

    def __init__(self, secrets_manager: ETHKeyFileSecretManger, db_manager=None):
        self.secrets_manager = secrets_manager
        self.db_manager = db_manager
        self._connector_cache: Dict[str, ConnectorBase] = {}
        self._orders_recorders: Dict[str, any] = {}
        self._funding_recorders: Dict[str, any] = {}
        self._status_polling_tasks: Dict[str, asyncio.Task] = {}

    async def get_connector(self, account_name: str, connector_name: str):
        """
        Get the connector object for the specified account and connector.
        Uses caching to avoid recreating connectors unnecessarily.
        Ensures proper initialization including position mode setup.

        :param account_name: The name of the account.
        :param connector_name: The name of the connector.
        :return: The connector object.
        """
        cache_key = f"{account_name}:{connector_name}"

        if cache_key in self._connector_cache:
            return self._connector_cache[cache_key]

        # Create connector with full initialization
        connector = await self._create_and_initialize_connector(account_name, connector_name)
        return connector

    def _create_connector(self, account_name: str, connector_name: str):
        """
        Create a new connector instance.
        Handles both regular connectors and paper trading connectors.

        :param account_name: The name of the account.
        :param connector_name: The name of the connector.
        :return: The connector object.
        """
        BackendAPISecurity.login_account(account_name=account_name, secrets_manager=self.secrets_manager)
        client_config_map = ClientConfigAdapter(ClientConfigMap())

        # Debug logging
        logger.info(f"Creating connector {connector_name} for account {account_name}")

        # Check if this is a paper trading connector
        if connector_name.endswith("_paper_trade"):
            return self._create_paper_trading_connector(connector_name, client_config_map)
        else:
            return self._create_regular_connector(connector_name, client_config_map)

    def _create_regular_connector(self, connector_name: str, client_config_map: ClientConfigAdapter):
        """
        Create a regular (live trading) connector instance.
        
        :param connector_name: The name of the connector.
        :param client_config_map: Client configuration map.
        :return: The connector object.
        """
        conn_setting = AllConnectorSettings.get_connector_settings()[connector_name]
        keys = BackendAPISecurity.api_keys(connector_name)

        logger.debug(f"API keys retrieved for {connector_name}: {list(keys.keys()) if keys else 'None'}")

        read_only_config = ReadOnlyClientConfigAdapter.lock_config(client_config_map)

        init_params = conn_setting.conn_init_parameters(
            trading_pairs=[],
            trading_required=True,
            api_keys=keys,
            client_config_map=read_only_config,
        )

        logger.debug(f"Init params keys for {connector_name}: {list(init_params.keys())}")

        connector_class = get_connector_class(connector_name)
        connector = connector_class(**init_params)
        return connector

    def _create_paper_trading_connector(self, connector_name: str, client_config_map: ClientConfigAdapter):
        """
        Create a paper trading connector instance.
        
        :param connector_name: The paper trading connector name (e.g., 'binance_paper_trade').
        :param client_config_map: Client configuration map.
        :return: The paper trading connector object.
        """
        # Extract base exchange name (remove '_paper_trade' suffix)
        base_exchange_name = connector_name.replace("_paper_trade", "")
        
        logger.info(f"Creating paper trading connector for base exchange: {base_exchange_name}")
        
        # Use standard trading pairs for paper trading
        # These can be updated later via _initialize_trading_pair_symbol_map
        trading_pairs = ["BTC-USDT", "ETH-USDT", "BNB-USDT"]
        
        try:
            # Create paper trading connector using Hummingbot's built-in function
            connector = create_paper_trade_market(
                exchange_name=base_exchange_name,
                client_config_map=client_config_map, 
                trading_pairs=trading_pairs
            )
            
            # Set paper trading balances from configuration
            paper_trade_config = client_config_map.paper_trade
            if paper_trade_config and paper_trade_config.paper_trade_account_balance:
                for asset, balance in paper_trade_config.paper_trade_account_balance.items():
                    connector.set_balance(asset, float(balance))
                    logger.debug(f"Set paper trading balance: {asset} = {balance}")
            
            # Initialize trading rules by accessing the underlying connector
            # Paper trading connectors wrap a real connector that has trading rules
            if hasattr(connector, '_market_data_tracker') and hasattr(connector._market_data_tracker, '_connector_class'):
                # Access the underlying connector for trading rules
                logger.debug(f"Paper trading connector wraps: {connector._market_data_tracker._connector_class}")
            
            logger.info(f"Successfully created paper trading connector {connector_name}")
            return connector
            
        except Exception as e:
            logger.error(f"Error creating paper trading connector {connector_name}: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise

    def clear_cache(self, account_name: Optional[str] = None, connector_name: Optional[str] = None):
        """
        Clear the connector cache.

        :param account_name: If provided, only clear cache for this account.
        :param connector_name: If provided with account_name, only clear this specific connector.
        """
        if account_name and connector_name:
            cache_key = f"{account_name}:{connector_name}"
            self._connector_cache.pop(cache_key, None)
        elif account_name:
            # Clear all connectors for this account
            keys_to_remove = [k for k in self._connector_cache.keys() if k.startswith(f"{account_name}:")]
            for key in keys_to_remove:
                self._connector_cache.pop(key)
        else:
            # Clear entire cache
            self._connector_cache.clear()

    @staticmethod
    def get_connector_config_map(connector_name: str):
        """
        Get the connector config map for the specified connector.

        :param connector_name: The name of the connector.
        :return: The connector config map.
        """
        connector_config = HummingbotAPIConfigAdapter(AllConnectorSettings.get_connector_config_keys(connector_name))
        return [key for key in connector_config.hb_config.__fields__.keys() if key != "connector"]

    async def update_connector_keys(self, account_name: str, connector_name: str, keys: dict):
        """
        Update the API keys for a connector and refresh the connector instance.

        :param account_name: The name of the account.
        :param connector_name: The name of the connector.
        :param keys: Dictionary of API keys to update.
        :return: The updated connector instance.
        """
        BackendAPISecurity.login_account(account_name=account_name, secrets_manager=self.secrets_manager)
        connector_config = HummingbotAPIConfigAdapter(AllConnectorSettings.get_connector_config_keys(connector_name))

        for key, value in keys.items():
            setattr(connector_config, key, value)

        BackendAPISecurity.update_connector_keys(account_name, connector_config)

        # Re-decrypt all credentials to ensure the new keys are available
        BackendAPISecurity.decrypt_all(account_name=account_name)

        # Clear the cache for this connector to force recreation with new keys
        self.clear_cache(account_name, connector_name)

        # Create and return new connector instance
        new_connector = await self.get_connector(account_name, connector_name)

        return new_connector

    def list_account_connectors(self, account_name: str) -> List[str]:
        """
        List all initialized connectors for a specific account.

        :param account_name: The name of the account.
        :return: List of connector names.
        """
        connectors = []
        for cache_key in self._connector_cache.keys():
            acc_name, conn_name = cache_key.split(":", 1)
            if acc_name == account_name:
                connectors.append(conn_name)
        return connectors

    def get_all_connectors(self) -> Dict[str, Dict[str, ConnectorBase]]:
        """
        Get all connectors organized by account.

        :return: Dictionary mapping account names to their connectors.
        """
        result = {}
        for cache_key, connector in self._connector_cache.items():
            account_name, connector_name = cache_key.split(":", 1)
            if account_name not in result:
                result[account_name] = {}
            result[account_name][connector_name] = connector
        return result

    def is_connector_initialized(self, account_name: str, connector_name: str) -> bool:
        """
        Check if a connector is already initialized and cached.

        :param account_name: The name of the account.
        :param connector_name: The name of the connector.
        :return: True if the connector is initialized, False otherwise.
        """
        cache_key = f"{account_name}:{connector_name}"
        return cache_key in self._connector_cache

    async def _create_and_initialize_connector(self, account_name: str, connector_name: str) -> ConnectorBase:
        """
        Create and fully initialize a connector with all necessary setup.
        This includes creating the connector, starting its network, setting up order recording,
        and configuring position mode for perpetual connectors.

        :param account_name: The name of the account.
        :param connector_name: The name of the connector.
        :return: The initialized connector instance.
        """
        cache_key = f"{account_name}:{connector_name}"
        # Create the base connector
        connector = self._create_connector(account_name, connector_name)

        # Handle initialization differently for paper trading vs regular connectors
        if connector_name.endswith("_paper_trade"):
            await self._initialize_paper_trading_connector(connector, account_name, connector_name)
        else:
            await self._initialize_regular_connector(connector, account_name, connector_name)

        self._connector_cache[cache_key] = connector
        logger.info(f"Initialized connector {connector_name} for account {account_name}")
        return connector

    async def _initialize_regular_connector(self, connector: ConnectorBase, account_name: str, connector_name: str):
        """
        Initialize a regular (live trading) connector with full setup.
        """
        cache_key = f"{account_name}:{connector_name}"
        
        # Initialize symbol map
        await connector._initialize_trading_pair_symbol_map()

        # Update trading rules
        await connector._update_trading_rules()

        # Update initial balances
        await connector._update_balances()

        # Set default position mode to HEDGE for perpetual connectors
        if "_perpetual" in connector_name:
            if PositionMode.HEDGE in connector.supported_position_modes():
                connector.set_position_mode(PositionMode.HEDGE)
            await connector._update_positions()

        # Load existing orders from database before starting network
        if self.db_manager:
            await self._load_existing_orders_from_database(connector, account_name, connector_name)

        # Start order tracking if db_manager is available
        if self.db_manager:
            if cache_key not in self._orders_recorders:
                # Import OrdersRecorder dynamically to avoid circular imports
                from services.orders_recorder import OrdersRecorder

                # Create and start orders recorder
                orders_recorder = OrdersRecorder(self.db_manager, account_name, connector_name)
                orders_recorder.start(connector)
                self._orders_recorders[cache_key] = orders_recorder

            # Start funding tracking for perpetual connectors
            if "_perpetual" in connector_name and cache_key not in self._funding_recorders:
                # Import FundingRecorder dynamically to avoid circular imports
                from services.funding_recorder import FundingRecorder

                # Create and start funding recorder
                funding_recorder = FundingRecorder(self.db_manager, account_name, connector_name)
                funding_recorder.start(connector)
                self._funding_recorders[cache_key] = funding_recorder

        # Start network manually without clock system
        await self._start_connector_network(connector)
        
        # Perform initial update of connector state
        await self._update_connector_state(connector, connector_name)

    async def _initialize_paper_trading_connector(self, connector, account_name: str, connector_name: str):
        """
        Initialize a paper trading connector with minimal setup.
        Paper trading connectors are wrappers and don't need the same initialization as regular connectors.
        """
        cache_key = f"{account_name}:{connector_name}"
        
        try:
            logger.info(f"Initializing paper trading connector {connector_name} for account {account_name}")
            
            # Just verify the connector is ready
            logger.debug(f"Paper trading connector type: {type(connector)}")
            
            # Check if it has the required attributes
            if hasattr(connector, '_account_balances'):
                balances = getattr(connector, '_account_balances', {})
                logger.debug(f"Paper trading connector {connector_name} balances: {balances}")
            
            # Ensure trading rules are accessible
            # Paper trading connectors should delegate trading rules to their underlying tracker
            if not hasattr(connector, 'trading_rules') or not connector.trading_rules:
                logger.debug(f"Paper trading connector {connector_name} has no trading rules, checking underlying connector")
                
                # Try to initialize trading rules from the order book tracker
                if hasattr(connector, '_order_book_tracker') and connector._order_book_tracker:
                    try:
                        # The order book tracker should have access to trading rules via its connector
                        await connector._order_book_tracker._update_trading_rules()
                        logger.debug(f"Updated trading rules via order book tracker for {connector_name}")
                    except Exception as e:
                        logger.warning(f"Could not update trading rules via tracker for {connector_name}: {e}")
                
                # As a fallback, create a minimal set of trading rules for common pairs
                if not hasattr(connector, 'trading_rules') or not connector.trading_rules:
                    logger.warning(f"Creating minimal trading rules for paper trading connector {connector_name}")
                    # This will be handled by the place_trade method if needed

            # Start order tracking if db_manager is available
            if self.db_manager:
                if cache_key not in self._orders_recorders:
                    try:
                        # Import OrdersRecorder dynamically to avoid circular imports
                        from services.orders_recorder import OrdersRecorder

                        # Create and start orders recorder
                        orders_recorder = OrdersRecorder(self.db_manager, account_name, connector_name)
                        orders_recorder.start(connector)
                        self._orders_recorders[cache_key] = orders_recorder
                        logger.debug(f"Started order recorder for paper trading connector {connector_name}")
                    except Exception as e:
                        logger.warning(f"Could not start order recorder for paper trading connector {connector_name}: {e}")
                        # Don't fail the entire initialization for order recorder issues

            logger.info(f"Paper trading connector {connector_name} initialized successfully")
            
        except Exception as e:
            logger.error(f"Error initializing paper trading connector {connector_name}: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise

    async def _start_connector_network(self, connector: ConnectorBase):
        """
        Start connector network tasks manually without clock system.
        Based on the original start_network method but without order book tracker.
        """
        try:
            # Stop any existing network tasks
            await self._stop_connector_network(connector)
            
            # Start trading rules polling
            connector._trading_rules_polling_task = safe_ensure_future(connector._trading_rules_polling_loop())

            # Start trading fees polling
            connector._trading_fees_polling_task = safe_ensure_future(connector._trading_fees_polling_loop())

            # Start user stream tracker (websocket connection)
            connector._user_stream_tracker_task = connector._create_user_stream_tracker_task()

            # Start user stream event listener
            connector._user_stream_event_listener_task = safe_ensure_future(connector._user_stream_event_listener())

            # Start lost orders update task
            connector._lost_orders_update_task = safe_ensure_future(connector._lost_orders_update_polling_loop())

            logger.info(f"Started connector network tasks for {connector}")

        except Exception as e:
            logger.error(f"Error starting connector network: {e}")
            raise

    async def _stop_connector_network(self, connector: ConnectorBase):
        """
        Stop connector network tasks.
        """
        try:
            # Stop trading rules polling
            if connector._trading_rules_polling_task:
                connector._trading_rules_polling_task.cancel()
                connector._trading_rules_polling_task = None
                
            # Stop trading fees polling
            if connector._trading_fees_polling_task:
                connector._trading_fees_polling_task.cancel()
                connector._trading_fees_polling_task = None
                
            # Stop status polling
            if connector._status_polling_task:
                connector._status_polling_task.cancel()
                connector._status_polling_task = None
                
            # Stop user stream tracker
            if connector._user_stream_tracker_task:
                connector._user_stream_tracker_task.cancel()
                connector._user_stream_tracker_task = None
                
            # Stop user stream event listener
            if connector._user_stream_event_listener_task:
                connector._user_stream_event_listener_task.cancel()
                connector._user_stream_event_listener_task = None
                
            # Stop lost orders update task
            if connector._lost_orders_update_task:
                connector._lost_orders_update_task.cancel()
                connector._lost_orders_update_task = None
                
        except Exception as e:
            logger.error(f"Error stopping connector network: {e}")

    async def _update_connector_state(self, connector: ConnectorBase, connector_name: str):
        """
        Update connector state including balances, orders, positions, and trading rules.
        This function can be called both during initialization and periodically.
        """
        try:
            # Update balances
            await connector._update_balances()
            
            # Update trading rules
            await connector._update_trading_rules()
            
            # Update positions for perpetual connectors
            if "_perpetual" in connector_name:
                await connector._update_positions()
            
            # Update order status for in-flight orders
            if hasattr(connector, '_update_order_status') and connector.in_flight_orders:
                await connector._update_order_status()
                
            logger.debug(f"Updated connector state for {connector_name}")
            
        except Exception as e:
            logger.error(f"Error updating connector state for {connector_name}: {e}")

    async def update_all_connector_states(self):
        """
        Update state for all cached connectors.
        This can be called periodically to refresh connector data.
        """
        for cache_key, connector in self._connector_cache.items():
            account_name, connector_name = cache_key.split(":", 1)
            try:
                await self._update_connector_state(connector, connector_name)
            except Exception as e:
                logger.error(f"Error updating state for {account_name}/{connector_name}: {e}")

    async def _load_existing_orders_from_database(self, connector: ConnectorBase, account_name: str, connector_name: str):
        """
        Load existing active orders from database and add them to connector's in_flight_orders.
        This ensures that orders placed before an API restart can still be managed.

        :param connector: The connector instance to load orders into
        :param account_name: The name of the account
        :param connector_name: The name of the connector
        """
        try:
            # Import OrderRepository dynamically to avoid circular imports
            from database import OrderRepository

            async with self.db_manager.get_session_context() as session:
                order_repo = OrderRepository(session)

                # Get active orders from database for this account/connector
                active_orders = await order_repo.get_active_orders(account_name=account_name, connector_name=connector_name)

                logger.info(f"Loading {len(active_orders)} existing active orders for {account_name}/{connector_name}")

                for order_record in active_orders:
                    try:
                        # Convert database order to InFlightOrder
                        in_flight_order = self._convert_db_order_to_in_flight_order(order_record)

                        # Add to connector's in_flight_orders
                        connector.in_flight_orders[in_flight_order.client_order_id] = in_flight_order

                        logger.debug(f"Loaded order {in_flight_order.client_order_id} from database into connector")

                    except Exception as e:
                        logger.error(f"Error converting database order {order_record.client_order_id} to InFlightOrder: {e}")
                        continue

                logger.info(
                    f"Successfully loaded {len(connector.in_flight_orders)} in-flight orders for {account_name}/{connector_name}"
                )

        except Exception as e:
            logger.error(f"Error loading existing orders from database for {account_name}/{connector_name}: {e}")

    def _convert_db_order_to_in_flight_order(self, order_record) -> InFlightOrder:
        """
        Convert a database Order record to a Hummingbot InFlightOrder object.

        :param order_record: Database Order model instance
        :return: InFlightOrder instance
        """
        # Map database status to OrderState
        status_mapping = {
            "SUBMITTED": OrderState.PENDING_CREATE,
            "OPEN": OrderState.OPEN,
            "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED,
            "FILLED": OrderState.FILLED,
            "CANCELLED": OrderState.CANCELED,
            "FAILED": OrderState.FAILED,
        }

        # Get the appropriate OrderState
        order_state = status_mapping.get(order_record.status, OrderState.PENDING_CREATE)

        # Convert string enums to proper enum instances
        try:
            order_type = OrderType[order_record.order_type]
        except (KeyError, ValueError):
            logger.warning(f"Unknown order type '{order_record.order_type}', defaulting to LIMIT")
            order_type = OrderType.LIMIT

        try:
            trade_type = TradeType[order_record.trade_type]
        except (KeyError, ValueError):
            logger.warning(f"Unknown trade type '{order_record.trade_type}', defaulting to BUY")
            trade_type = TradeType.BUY

        # Convert creation timestamp - use order creation time or current time as fallback
        creation_timestamp = order_record.created_at.timestamp() if order_record.created_at else time.time()

        # Create InFlightOrder instance
        in_flight_order = InFlightOrder(
            client_order_id=order_record.client_order_id,
            trading_pair=order_record.trading_pair,
            order_type=order_type,
            trade_type=trade_type,
            amount=Decimal(str(order_record.amount)),
            creation_timestamp=creation_timestamp,
            price=Decimal(str(order_record.price)) if order_record.price else None,
            exchange_order_id=order_record.exchange_order_id,
            initial_state=order_state,
            leverage=1,  # Default leverage
            position=PositionAction.NIL,  # Default position action
        )

        # Update current state and filled amount if order has progressed
        in_flight_order.current_state = order_state
        if order_record.filled_amount:
            in_flight_order.executed_amount_base = Decimal(str(order_record.filled_amount))
        if order_record.average_fill_price:
            in_flight_order.last_executed_quantity = Decimal(str(order_record.filled_amount or 0))
            in_flight_order.last_executed_price = Decimal(str(order_record.average_fill_price))

        return in_flight_order

    async def stop_connector(self, account_name: str, connector_name: str):
        """
        Stop a connector and its associated services.

        :param account_name: The name of the account.
        :param connector_name: The name of the connector.
        """
        cache_key = f"{account_name}:{connector_name}"

        # Stop order recorder if exists
        if cache_key in self._orders_recorders:
            try:
                await self._orders_recorders[cache_key].stop()
                del self._orders_recorders[cache_key]
                logger.info(f"Stopped order recorder for {account_name}/{connector_name}")
            except Exception as e:
                logger.error(f"Error stopping order recorder for {account_name}/{connector_name}: {e}")

        # Stop funding recorder if exists
        if cache_key in self._funding_recorders:
            try:
                await self._funding_recorders[cache_key].stop()
                del self._funding_recorders[cache_key]
                logger.info(f"Stopped funding recorder for {account_name}/{connector_name}")
            except Exception as e:
                logger.error(f"Error stopping funding recorder for {account_name}/{connector_name}: {e}")

        # Stop manual status polling task if exists
        if cache_key in self._status_polling_tasks:
            try:
                self._status_polling_tasks[cache_key].cancel()
                del self._status_polling_tasks[cache_key]
                logger.info(f"Stopped manual status polling for {account_name}/{connector_name}")
            except Exception as e:
                logger.error(f"Error stopping manual status polling for {account_name}/{connector_name}: {e}")

        # Stop connector network if exists
        if cache_key in self._connector_cache:
            try:
                connector = self._connector_cache[cache_key]
                await self._stop_connector_network(connector)
                logger.info(f"Stopped connector network for {account_name}/{connector_name}")
            except Exception as e:
                logger.error(f"Error stopping connector network for {account_name}/{connector_name}: {e}")

    async def stop_all_connectors(self):
        """
        Stop all connectors and their associated services.
        """
        # Get all account/connector pairs
        pairs = [(k.split(":", 1)[0], k.split(":", 1)[1]) for k in self._connector_cache.keys()]

        # Stop each connector
        for account_name, connector_name in pairs:
            await self.stop_connector(account_name, connector_name)

    def list_available_credentials(self, account_name: str) -> List[str]:
        """
        List all available connector credentials for an account.

        :param account_name: The name of the account.
        :return: List of connector names that have credentials.
        """
        try:
            files = fs_util.list_files(f"credentials/{account_name}/connectors")
            return [file.replace(".yml", "") for file in files if file.endswith(".yml")]
        except FileNotFoundError:
            return []
