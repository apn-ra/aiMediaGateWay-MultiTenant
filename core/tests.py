from django.test import TestCase, TransactionTestCase
from asgiref.sync import sync_to_async
from unittest.mock import Mock, AsyncMock, patch
from decouple import config
import asyncio
import unittest
from datetime import timedelta
from django.utils import timezone

from ari.config import ARIConfig
from core.models import Tenant, SystemConfiguration
from core.junie_codes.ami.ami_manager import (
    AMIConnectionConfig, 
    ConnectionStats, 
    AMIConnection, 
    AMIManager, 
    get_ami_manager,
    cleanup_ami_manager
)
from core.junie_codes.ami.ami_events import (
    AMIEventHandler,
    get_event_handler,
    cleanup_event_handler
)
from core.junie_codes.ari import (
    ARIConnectionStats,
    ARIConnection,
    ARIManager,
    get_ari_manager,
    cleanup_ari_manager
)
from core.junie_codes.ari import (
    ARIEventStats,
    ARIEventHandler,
    get_ari_event_handler,
    cleanup_ari_event_handler
)


class AMIConnectionConfigTest(TestCase):
    """Test AMI connection configuration dataclass."""
    
    def test_default_values(self):
        """Test default configuration values."""
        config = AMIConnectionConfig(host='localhost')
        self.assertEqual(config.host, 'localhost')
        self.assertEqual(config.port, 5038)
        self.assertEqual(config.timeout, 30.0)
        self.assertEqual(config.max_retries, 3)
        self.assertEqual(config.retry_delay, 5.0)
        self.assertEqual(config.ping_interval, 30.0)
    
    def test_custom_values(self):
        """Test custom configuration values."""
        config = AMIConnectionConfig(
            host='asterisk.example.com',
            port=5039,
            username='testuser',
            secret='testpass',
            timeout=45.0,
            max_retries=5,
            retry_delay=10.0,
            ping_interval=60.0
        )
        self.assertEqual(config.host, 'asterisk.example.com')
        self.assertEqual(config.port, 5039)
        self.assertEqual(config.username, 'testuser')
        self.assertEqual(config.secret, 'testpass')
        self.assertEqual(config.timeout, 45.0)
        self.assertEqual(config.max_retries, 5)
        self.assertEqual(config.retry_delay, 10.0)
        self.assertEqual(config.ping_interval, 60.0)


class ConnectionStatsTest(TestCase):
    """Test connection statistics dataclass."""
    
    def test_default_values(self):
        """Test default statistics values."""
        stats = ConnectionStats()
        self.assertIsNone(stats.connected_at)
        self.assertIsNone(stats.last_event_at)
        self.assertEqual(stats.events_processed, 0)
        self.assertEqual(stats.reconnection_count, 0)
        self.assertIsNone(stats.last_error)
        self.assertFalse(stats.is_healthy)
    
    def test_custom_values(self):
        """Test custom statistics values."""
        now = timezone.now()
        stats = ConnectionStats(
            connected_at=now,
            last_event_at=now,
            events_processed=100,
            reconnection_count=2,
            last_error="Test error",
            is_healthy=True
        )
        self.assertEqual(stats.connected_at, now)
        self.assertEqual(stats.last_event_at, now)
        self.assertEqual(stats.events_processed, 100)
        self.assertEqual(stats.reconnection_count, 2)
        self.assertEqual(stats.last_error, "Test error")
        self.assertTrue(stats.is_healthy)


class AMIConnectionTest(TransactionTestCase):
    """Test AMI connection class with async operations."""
    
    def setUp(self):
        """Set up test data."""
        self.tenant = Tenant.objects.create(
            name="Test Tenant",
            asterisk_host=config('ASTERISK_HOST', cast=str),
            asterisk_ami_port=config('ASTERISK_AMI_PORT', cast=int),
            asterisk_ami_username=config('ASTERISK_AMI_USERNAME', cast=str),
            asterisk_ami_secret=config('ASTERISK_AMI_SECRET', cast=str),
            domain="test",
            is_active=True
        )
        self.config = AMIConnectionConfig(
            host=self.tenant.asterisk_host,
            port=self.tenant.asterisk_ami_port,
            username=self.tenant.asterisk_ami_username,
            secret=self.tenant.asterisk_ami_secret,
            timeout=10.0,
            max_retries=2,
            retry_delay=1.0,
            ping_interval=5.0
        )

    def tearDown(self):
        """Tear down test environment."""
        Tenant.objects.all().delete()

    def test_init(self):
        """Test AMI connection initialization."""
        connection = AMIConnection(str(self.tenant.id), self.config)
        self.assertEqual(connection.tenant_id, str(self.tenant.id))
        self.assertEqual(connection.config, self.config)
        self.assertIsNone(connection.manager)
        self.assertIsInstance(connection.stats, ConnectionStats)
        self.assertFalse(connection.stats.is_healthy)
    
    @patch('core.ami.ami_manager.panoramisk.Manager')
    def test_register_event_handler(self, mock_manager_class):
        """Test event handler registration."""
        connection = AMIConnection(str(self.tenant.id), self.config)
        
        # Mock handler function
        mock_handler = Mock()
        
        # Register event handler
        connection.register_event_handler('Newchannel', mock_handler)
        
        # Check that handler was registered
        self.assertIn('Newchannel', connection._event_handlers)
        self.assertIn(mock_handler, connection._event_handlers['Newchannel'])
    
    def test_stats_update(self):
        """Test statistics updates."""
        connection = AMIConnection(str(self.tenant.id), self.config)
        
        # Initial state
        self.assertEqual(connection.stats.events_processed, 0)
        self.assertIsNone(connection.stats.last_event_at)
        
        # Simulate event handling
        asyncio.run(connection._handle_any_event(None, {}))
        
        # Check statistics were updated
        self.assertEqual(connection.stats.events_processed, 1)
        self.assertIsNotNone(connection.stats.last_event_at)


class AMIManagerTest(TransactionTestCase):
    """Test AMI manager class with database operations."""
    
    def setUp(self):
        """Set up test data."""
        Tenant.objects.all().delete()

        self.tenant = Tenant.objects.create(
            name="Test Tenant",
            domain="test",
            is_active=True
        )
        
        # Create AMI configuration
        SystemConfiguration.objects.create(
            tenant=self.tenant,
            key='ami_host',
            value='localhost',
            value_type='str'
        )
        SystemConfiguration.objects.create(
            tenant=self.tenant,
            key='ami_port',
            value='5038',
            value_type='int'
        )
        SystemConfiguration.objects.create(
            tenant=self.tenant,
            key='ami_username',
            value='testuser',
            value_type='str'
        )
        SystemConfiguration.objects.create(
            tenant=self.tenant,
            key='ami_secret',
            value='testpass',
            value_type='str'
        )

    def tearDown(self):
        Tenant.objects.all().delete()
        SystemConfiguration.objects.all().delete()

    def test_init(self):
        """Test AMI manager initialization."""
        manager = AMIManager()
        self.assertEqual(len(manager.connections), 0)
        self.assertIsNotNone(manager.session_manager)
    
    def test_get_connection_stats(self):
        """Test connection statistics retrieval."""
        manager = AMIManager()
        stats = manager.get_connection_stats()
        self.assertIsInstance(stats, dict)
        self.assertEqual(len(stats), 0)
    
    async def test_get_tenant_ami_config(self):
        """Test tenant AMI configuration loading."""
        manager = AMIManager()
        config = await manager._get_tenant_ami_config(str(self.tenant.id))
        
        self.assertIsNotNone(config)
        self.assertIsInstance(config, AMIConnectionConfig)
        self.assertEqual(config.host, 'localhost')
        self.assertEqual(config.port, '5038')
        self.assertEqual(config.username, 'testuser')
        self.assertEqual(config.secret, 'testpass')
    
    async def test_get_tenant_ami_config_missing_credentials(self):
        """Test configuration loading with missing credentials."""
        # Create tenant without credentials
        tenant2 = await sync_to_async(Tenant.objects.create)(
            name="Test Tenant 2",
            domain="test2",
            schema_name="test2_schema",
            is_active=True
        )

        await sync_to_async(SystemConfiguration.objects.create)(
            tenant=tenant2,
            key='ami_host',
            value='localhost',
            value_type='str'
        )
        
        manager = AMIManager()
        config = await manager._get_tenant_ami_config(str(tenant2.id))
        
        # Should return None due to missing credentials
        self.assertIsNone(config)
    
    async def test_get_tenant_ami_config_nonexistent_tenant(self):
        """Test configuration loading for nonexistent tenant."""
        manager = AMIManager()
        config = await manager._get_tenant_ami_config('nonexistent-id')
        
        self.assertIsNone(config)
    
    async def test_disconnect_all(self):
        """Test disconnecting all connections."""
        manager = AMIManager()
        
        # Mock some connections
        mock_connection1 = Mock()
        mock_connection1.disconnect = AsyncMock()
        mock_connection2 = Mock()
        mock_connection2.disconnect = AsyncMock()
        
        manager.connections = {
            'tenant1': mock_connection1,
            'tenant2': mock_connection2
        }
        
        await manager.disconnect_all()
        
        # Check connections were disconnected and cleared
        mock_connection1.disconnect.assert_called_once()
        mock_connection2.disconnect.assert_called_once()
        self.assertEqual(len(manager.connections), 0)
    
    def test_health_check_sync(self):
        """Test health check functionality (sync version)."""
        manager = AMIManager()
        
        # Mock connections with different health states
        mock_connection1 = Mock()
        mock_connection1.stats.is_healthy = True
        mock_connection1.stats.last_event_at = timezone.now()
        
        mock_connection2 = Mock()
        mock_connection2.stats.is_healthy = False
        mock_connection2.stats.last_event_at = timezone.now() - timedelta(minutes=10)
        
        manager.connections = {
            'tenant1': mock_connection1,
            'tenant2': mock_connection2
        }
        
        # Run health check (sync version for testing)
        health_status = {}
        for tenant_id, connection in manager.connections.items():
            health_status[tenant_id] = connection.stats.is_healthy
            
            # Check if connection is stale
            if connection.stats.last_event_at:
                time_since_event = timezone.now() - connection.stats.last_event_at
                if time_since_event > timedelta(minutes=5):
                    health_status[tenant_id] = False
        
        # Check results
        self.assertTrue(health_status['tenant1'])
        self.assertFalse(health_status['tenant2'])  # Should be False due to stale events


class AMIManagerGlobalTest(TestCase):
    """Test global AMI manager functions."""
    
    def test_get_ami_manager(self):
        """Test global AMI manager retrieval."""
        async def run_test():
            manager = await get_ami_manager()
            self.assertIsInstance(manager, AMIManager)
            
            # Should return same instance on subsequent calls
            manager2 = await get_ami_manager()
            self.assertIs(manager, manager2)
            
            # Cleanup
            await cleanup_ami_manager()
        
        asyncio.run(run_test())
    
    def test_cleanup_ami_manager(self):
        """Test global AMI manager cleanup."""
        async def run_test():
            # Get manager instance
            manager = await get_ami_manager()
            self.assertIsNotNone(manager)
            
            # Cleanup
            await cleanup_ami_manager()
            
            # Should create new instance after cleanup
            manager2 = await get_ami_manager()
            self.assertIsInstance(manager2, AMIManager)
            
            # Final cleanup
            await cleanup_ami_manager()
        
        asyncio.run(run_test())


class AMIIntegrationTest(TransactionTestCase):
    """Integration tests for AMI manager with session manager."""
    
    def setUp(self):
        """Set up test data."""
        self.tenant = Tenant.objects.create(
            name="Integration Test Tenant",
            domain="integration",
            is_active=True
        )
        
        # Create complete AMI configuration
        ami_configs = [
            ('ami_host', 'localhost', 'str'),
            ('ami_port', '5038', 'int'),
            ('ami_username', 'testuser', 'str'),
            ('ami_secret', 'testpass', 'str'),
            ('ami_timeout', '30.0', 'float'),
            ('ami_max_retries', '3', 'int'),
            ('ami_retry_delay', '5.0', 'float'),
            ('ami_ping_interval', '30.0', 'float')
        ]
        
        for key, value, value_type in ami_configs:
            SystemConfiguration.objects.create(
                tenant=self.tenant,
                key=key,
                value=value,
                value_type=value_type
            )
    
    def test_configuration_loading_integration(self):
        """Test complete configuration loading integration."""
        async def run_test():
            manager = AMIManager()
            config = await manager._get_tenant_ami_config(str(self.tenant.id))
            
            self.assertIsNotNone(config)
            self.assertEqual(config.host, 'localhost')
            self.assertEqual(int(config.port), 5038)
            self.assertEqual(config.username, 'testuser')
            self.assertEqual(config.secret, 'testpass')
            self.assertEqual(float(config.timeout), 30.0)
            self.assertEqual(int(config.max_retries), 3)
            self.assertEqual(float(config.retry_delay), 5.0)
            self.assertEqual(float(config.ping_interval), 30.0)
        
        asyncio.run(run_test())


class AMIEventHandlerTest(TransactionTestCase):
    """Test AMI event handler functionality."""
    
    def setUp(self):
        """Set up test data."""
        self.tenant = Tenant.objects.create(
            name="Event Test Tenant",
            domain="eventtest",
            is_active=True
        )
        
    def test_initialization(self):
        """Test event handler initialization."""
        handler = AMIEventHandler()
        self.assertIsNone(handler.session_manager)
        self.assertIsNone(handler.ami_manager)
        self.assertEqual(len(handler._tenant_cache), 0)
        self.assertIsInstance(handler._event_statistics, dict)
        self.assertEqual(handler._event_statistics['total'], 0)
    
    def test_statistics_tracking(self):
        """Test event statistics tracking."""
        handler = AMIEventHandler()
        
        # Initial state
        stats = handler.get_statistics()
        self.assertEqual(stats['new_channel'], 0)
        self.assertEqual(stats['dial'], 0)
        self.assertEqual(stats['total'], 0)
        
        # Simulate processing events
        handler._event_statistics['new_channel'] += 1
        handler._event_statistics['dial'] += 2
        
        stats = handler.get_statistics()
        self.assertEqual(stats['new_channel'], 1)
        self.assertEqual(stats['dial'], 2)
        self.assertEqual(stats['total'], 3)
        
        # Reset statistics
        handler.reset_statistics()
        stats = handler.get_statistics()
        self.assertEqual(stats['total'], 0)
    
    def test_is_inbound_channel(self):
        """Test channel direction detection."""
        handler = AMIEventHandler()
        
        # Test inbound channels
        self.assertTrue(handler._is_inbound_channel('PJSIP/user123-00000001'))
        self.assertTrue(handler._is_inbound_channel('SIP/trunk-00000002'))
        self.assertTrue(handler._is_inbound_channel('DAHDI/1-1'))
        
        # Test outbound channels
        self.assertFalse(handler._is_inbound_channel('Local/123@outbound-00000001;1'))
        
        # Test unknown channels (defaults to inbound)
        self.assertTrue(handler._is_inbound_channel('UNKNOWN/test-123'))
    
    def test_tenant_validation_sync_wrapper(self):
        """Test tenant validation with sync wrapper."""
        async def run_test():
            handler = AMIEventHandler()
            
            # Test valid tenant
            is_valid = await handler._validate_tenant(str(self.tenant.id))
            self.assertTrue(is_valid)
            
            # Test invalid tenant
            is_valid = await handler._validate_tenant('invalid-id')
            self.assertFalse(is_valid)
        
        asyncio.run(run_test())


class AMIEventHandlerNewChannelTest(TransactionTestCase):
    """Test Newchannel event handling."""
    
    def setUp(self):
        """Set up test data."""
        self.tenant = Tenant.objects.create(
            name="New channel Test Tenant",
            domain="new_channel",
            is_active=True
        )
        self.handler = AMIEventHandler()

    def tearDown(self):
        """Tear down test environment."""
        Tenant.objects.all().delete()

    def test_new_channel_event_processing(self):
        """Test processing of New channel events."""
        async def run_test():
            # Mock session manager
            mock_session_manager = Mock()
            mock_session_manager.create_session = AsyncMock(return_value=True)
            self.handler.session_manager = mock_session_manager
            
            # Mock tenant extraction
            self.handler._extract_tenant_from_context = AsyncMock(
                return_value=str(self.tenant.id)
            )
            self.handler._create_database_session = AsyncMock()
            
            # Test message
            message = {
                'Channel': 'PJSIP/user123-00000001',
                'CallerIDNum': '1234567890',
                'CallerIDName': 'John Doe',
                'Context': f'tenant_{self.tenant.id}_inbound',
                'Exten': '100',
                'Uniqueid': 'test-unique-id-123'
            }
            
            # Process event
            await self.handler.handle_new_channel(None, message)
            
            # Verify session creation was called
            mock_session_manager.create_session.assert_called_once()
            
            # Verify session data
            call_args = mock_session_manager.create_session.call_args[0]
            session_data = call_args[0]
            self.assertEqual(session_data.session_id, 'test-unique-id-123')
            self.assertEqual(session_data.asterisk_channel_id, 'PJSIP/user123-00000001')
            self.assertEqual(session_data.caller_id_number, '1234567890')
            self.assertEqual(session_data.caller_id_name, 'John Doe')
            self.assertEqual(session_data.dialed_number, '100')
            self.assertEqual(session_data.direction, 'inbound')
            self.assertEqual(session_data.status, 'ringing')
            
            # Verify statistics
            self.assertEqual(self.handler._event_statistics['new_channel'], 1)
        
        asyncio.run(run_test())


class AMIEventHandlerGlobalTest(TestCase):
    """Test global AMI event handler functions."""
    
    def test_get_event_handler(self):
        """Test global event handler retrieval."""
        async def run_test():
            handler = await get_event_handler()
            self.assertIsInstance(handler, AMIEventHandler)
            
            # Should return same instance on subsequent calls
            handler2 = await get_event_handler()
            self.assertIs(handler, handler2)
            
            # Cleanup
            await cleanup_event_handler()
        
        asyncio.run(run_test())
    
    def test_cleanup_event_handler(self):
        """Test event handler cleanup."""
        async def run_test():
            # Get handler instance
            handler = await get_event_handler()
            self.assertIsNotNone(handler)
            
            # Cleanup
            await cleanup_event_handler()
            
            # Should create new instance after cleanup
            handler2 = await get_event_handler()
            self.assertIsInstance(handler2, AMIEventHandler)
            
            # Final cleanup
            await cleanup_event_handler()
        
        asyncio.run(run_test())


# ===============================
# ARI Manager Tests
# ===============================

class ARIConnectionConfigTest(unittest.IsolatedAsyncioTestCase):
    """Test ARI connection configuration dataclass."""

    def test_default_values(self):
        """Test default configuration values."""
        ari_config = ARIConfig()

        self.assertEqual(ari_config.host, config('ASTERISK_HOST', cast=str))
        self.assertEqual(ari_config.port, config('ASTERISK_ARI_PORT', cast=int))
        self.assertEqual(ari_config.username, config('ASTERISK_ARI_USERNAME', cast=str))
        self.assertEqual(ari_config.password, config('ASTERISK_ARI_PASSWORD', cast=str))
        self.assertEqual(ari_config.app_name, config('ASTERISK_ARI_APP_NAME', cast=str))
        self.assertEqual(ari_config.timeout, 30)

    def test_custom_values(self):
        """Test custom configuration values."""

        ari_config = ARIConfig(
            host='ari.example.com',
            port=8089,
            username='ari',
            password='testapp',
            app_name='testapp',
            retry_attempts=5,
            retry_backoff=10,
        )

        self.assertEqual(ari_config.host, 'ari.example.com')
        self.assertEqual(ari_config.port, 8089)
        self.assertEqual(ari_config.retry_attempts, 5)
        self.assertEqual(ari_config.retry_backoff, 10)


class ARIConnectionStatsTest(TestCase):
    """Test ARI connection statistics dataclass."""
    
    def test_default_values(self):
        """Test default statistics values."""
        stats = ARIConnectionStats()
        self.assertFalse(stats.connected)
        self.assertIsNone(stats.connection_time)
        self.assertIsNone(stats.last_activity)
        self.assertEqual(stats.total_operations, 0)
        self.assertEqual(stats.failed_operations, 0)
        self.assertEqual(stats.reconnect_count, 0)
    
    def test_custom_values(self):
        """Test custom statistics values."""
        now = timezone.now()
        stats = ARIConnectionStats(
            connected=True,
            connection_time=now,
            last_activity=now,
            total_operations=100,
            failed_operations=5,
            reconnect_count=2
        )
        self.assertTrue(stats.connected)
        self.assertEqual(stats.connection_time, now)
        self.assertEqual(stats.last_activity, now)
        self.assertEqual(stats.total_operations, 100)
        self.assertEqual(stats.failed_operations, 5)
        self.assertEqual(stats.reconnect_count, 2)


class ARIConnectionTest(TransactionTestCase):
    """Test ARI connection class with async operations."""
    
    def setUp(self):
        """Set up test data."""
        self.tenant = Tenant.objects.create(
            name="ARI Test Tenant",
            asterisk_host=config('ASTERISK_HOST', cast=str),
            asterisk_ari_port=config('ASTERISK_ARI_PORT', cast=int),
            asterisk_ari_username=config('ASTERISK_ARI_USERNAME', cast=str),
            asterisk_ari_password=config('ASTERISK_ARI_PASSWORD', cast=str),
            asterisk_ari_app_name=config('ASTERISK_ARI_APP_NAME', cast=str),
            domain="aritest",
            is_active=True
        )
        self.config = ARIConfig(
            host=self.tenant.asterisk_host,
            port=self.tenant.asterisk_ari_port,
            username=self.tenant.asterisk_ari_username,
            password=self.tenant.asterisk_ari_password,
            app_name=self.tenant.asterisk_ari_app_name
        )

    def tearDown(self):
        """Tear down test environment."""
        Tenant.objects.all().delete()

    def test_init(self):
        """Test ARI connection initialization."""
        connection = ARIConnection(str(self.tenant.id), self.config)
        self.assertEqual(connection.tenant_id, str(self.tenant.id))
        self.assertEqual(connection.config, self.config)
        self.assertIsNotNone(connection.client)
        self.assertIsInstance(connection.stats, ARIConnectionStats)
        self.assertFalse(connection.stats.connected)
        self.assertEqual(len(connection.event_handlers), 0)

    def test_connect_success(self):
        """Test successful ARI connection."""
        async def run_test():
            connection = ARIConnection(str(self.tenant.id), self.config)

            result = await connection.connect()
            
            self.assertTrue(result)
            self.assertTrue(connection.stats.connected)
            self.assertIsNotNone(connection.stats.connection_time)
        asyncio.run(run_test())
    
    @patch('core.ari.ari_manager.ARIConnection')
    def test_connect_failure(self, mock_ari_connect):
        """Test ARI connection failure."""
        async def run_test():
            # Mock connection failure
            mock_ari_connect.side_effect = Exception("Connection failed")
            
            connection = ARIConnection(str(self.tenant.id), self.config)
            connection._schedule_reconnect = AsyncMock()
            
            result = await connection.connect()
            
            self.assertFalse(result)
            self.assertFalse(connection.stats.connected)
            self.assertIsNone(connection.client)
            self.assertEqual(connection.stats.failed_operations, 2)  # max_retries attempts
        
        asyncio.run(run_test())
    
    def test_register_event_handler(self):
        """Test event handler registration."""
        connection = ARIConnection(str(self.tenant.id), self.config)
        
        # Mock handler function
        mock_handler = Mock()
        
        # Register event handler
        connection.register_event_handler('StasisStart', mock_handler)
        
        # Check that handler was registered
        self.assertIn('StasisStart', connection.event_handlers)
        self.assertEqual(connection.event_handlers['StasisStart'], mock_handler)
    
    def test_execute_operation_success(self):
        """Test successful operation execution."""
        async def run_test():
            connection = ARIConnection(str(self.tenant.id), self.config)
            
            # Mock connected client
            mock_client = Mock()
            connection.client = mock_client
            connection.stats.connected = True
            
            # Mock operation
            mock_operation = Mock(return_value="success")
            
            result = await connection.execute_operation(mock_operation, "arg1", key="value")
            
            self.assertEqual(result, "success")
            self.assertEqual(connection.stats.total_operations, 1)
            self.assertIsNotNone(connection.stats.last_activity)
        
        asyncio.run(run_test())


class ARIManagerTest(TransactionTestCase):
    """Test ARI manager class with database operations."""
    
    def setUp(self):
        """Set up test data."""
        self.tenant = Tenant.objects.create(
            name="ARI Manager Test Tenant",
            domain="arimanager",
            is_active=True
        )
        
        # Create ARI configuration
        SystemConfiguration.objects.create(
            tenant=self.tenant,
            key='ari_host',
            value='localhost',
            value_type='str'
        )
        SystemConfiguration.objects.create(
            tenant=self.tenant,
            key='ari_port',
            value='8088',
            value_type='int'
        )
        SystemConfiguration.objects.create(
            tenant=self.tenant,
            key='ari_username',
            value='testuser',
            value_type='str'
        )
        SystemConfiguration.objects.create(
            tenant=self.tenant,
            key='ari_password',
            value='testpass',
            value_type='str'
        )
    
    def test_init(self):
        """Test ARI manager initialization."""
        manager = ARIManager()
        self.assertEqual(len(manager.connections), 0)
        self.assertIsNotNone(manager._lock)
    
    def test_get_connection_stats(self):
        """Test connection statistics retrieval."""
        manager = ARIManager()
        stats = manager.get_connection_stats()
        self.assertIsInstance(stats, dict)
        self.assertEqual(len(stats), 0)
    
    async def test_get_tenant_ari_config(self):
        """Test tenant ARI configuration loading."""
        manager = ARIManager()
        config = await manager._get_tenant_ari_config(str(self.tenant.id))
        
        self.assertIsNotNone(config)
        self.assertIsInstance(config, ARIConnectionConfig)
        self.assertEqual(config.host, 'localhost')
        self.assertEqual(config.port, 8088)
        self.assertEqual(config.username, 'testuser')
        self.assertEqual(config.password, 'testpass')
        self.assertEqual(config.app_name, f'aiMediaGateway_{self.tenant.id}')


class ARIManagerGlobalTest(TestCase):
    """Test global ARI manager functions."""
    
    def test_get_ari_manager(self):
        """Test global ARI manager retrieval."""
        # Get manager instance
        manager1 = get_ari_manager()
        self.assertIsInstance(manager1, ARIManager)
        
        # Should return same instance on subsequent calls
        manager2 = get_ari_manager()
        self.assertIs(manager1, manager2)
        
        # Cleanup
        cleanup_ari_manager()
    
    def test_cleanup_ari_manager(self):
        """Test ARI manager cleanup."""
        # Get manager instance
        manager = get_ari_manager()
        self.assertIsNotNone(manager)
        
        # Cleanup
        cleanup_ari_manager()
        
        # Should create new instance after cleanup
        manager2 = get_ari_manager()
        self.assertIsInstance(manager2, ARIManager)
        
        # Final cleanup
        cleanup_ari_manager()


# ===============================
# ARI Event Handler Tests
# ===============================

class ARIEventStatsTest(TestCase):
    """Test ARI event statistics dataclass."""
    
    def test_default_values(self):
        """Test default statistics values."""
        stats = ARIEventStats()
        self.assertEqual(stats.stasis_start_events, 0)
        self.assertEqual(stats.stasis_end_events, 0)
        self.assertEqual(stats.channel_state_change_events, 0)
        self.assertEqual(stats.bridge_created_events, 0)
        self.assertEqual(stats.bridge_destroyed_events, 0)
        self.assertEqual(stats.bridge_entered_events, 0)
        self.assertEqual(stats.bridge_left_events, 0)
        self.assertEqual(stats.recording_started_events, 0)
        self.assertEqual(stats.recording_finished_events, 0)
        self.assertEqual(stats.total_events_processed, 0)
        self.assertEqual(stats.total_errors, 0)
        self.assertIsNone(stats.last_event_time)
    
    def test_custom_values(self):
        """Test custom statistics values."""
        now = timezone.now()
        stats = ARIEventStats(
            stasis_start_events=10,
            stasis_end_events=8,
            channel_state_change_events=25,
            total_events_processed=50,
            total_errors=2,
            last_event_time=now
        )
        self.assertEqual(stats.stasis_start_events, 10)
        self.assertEqual(stats.stasis_end_events, 8)
        self.assertEqual(stats.channel_state_change_events, 25)
        self.assertEqual(stats.total_events_processed, 50)
        self.assertEqual(stats.total_errors, 2)
        self.assertEqual(stats.last_event_time, now)


class ARIEventHandlerTest(TransactionTestCase):
    """Test ARI event handler class."""
    
    def setUp(self):
        """Set up test data."""
        self.tenant = Tenant.objects.create(
            name="ARI Event Test Tenant",
            schema_name="arievent",
            asterisk_host="localhost",
            asterisk_ami_username="testuser",
            asterisk_ami_secret="testpass",
            asterisk_ari_username="testuser",
            asterisk_ari_password="testpass",
            is_active=True
        )
        self.handler = ARIEventHandler()
    
    def test_init(self):
        """Test ARI event handler initialization."""
        self.assertIsNotNone(self.handler.session_manager)
        self.assertIsNotNone(self.handler.ari_manager)
        self.assertIsInstance(self.handler.stats, ARIEventStats)
        self.assertEqual(len(self.handler.custom_handlers), 0)
    
    def test_register_custom_handler(self):
        """Test custom event handler registration."""
        # Mock handler function
        mock_handler = Mock()
        
        # Register handler
        self.handler.register_custom_handler('StasisStart', mock_handler)
        
        # Verify registration
        self.assertIn('StasisStart', self.handler.custom_handlers)
        self.assertIn(mock_handler, self.handler.custom_handlers['StasisStart'])
    
    def test_unregister_custom_handler(self):
        """Test custom event handler unregistration."""
        # Mock handler function
        mock_handler = Mock()
        
        # Register and then unregister
        self.handler.register_custom_handler('StasisStart', mock_handler)
        self.handler.unregister_custom_handler('StasisStart', mock_handler)
        
        # Verify unregistration
        self.assertEqual(len(self.handler.custom_handlers['StasisStart']), 0)
    
    def test_get_statistics(self):
        """Test statistics retrieval."""
        stats = self.handler.get_statistics()
        self.assertIsInstance(stats, ARIEventStats)
        self.assertEqual(stats.total_events_processed, 0)
    
    def test_map_channel_state_to_status(self):
        """Test channel state mapping."""
        # Test various state mappings
        self.assertEqual(self.handler._map_channel_state_to_status('Up'), 'answered')
        self.assertEqual(self.handler._map_channel_state_to_status('Down'), 'completed')
        self.assertEqual(self.handler._map_channel_state_to_status('Ringing'), 'ringing')
        self.assertEqual(self.handler._map_channel_state_to_status('Busy'), 'busy')
        self.assertEqual(self.handler._map_channel_state_to_status('Unknown'), 'unknown')
        self.assertEqual(self.handler._map_channel_state_to_status('InvalidState'), 'unknown')
    
    def test_extract_tenant_from_channel(self):
        """Test tenant extraction from channel."""
        async def run_test():
            # Mock channel with tenant in name
            mock_channel = Mock()
            mock_channel.name = 'PJSIP/user123-tenant_test123_inbound-00000001'
            # Mock channelvars to return None so it falls back to name parsing
            mock_channel.channelvars = Mock()
            mock_channel.channelvars.get.return_value = None
            
            tenant_id = await self.handler._extract_tenant_from_channel(mock_channel)
            self.assertEqual(tenant_id, 'test123')
            
            # Mock channel with tenant in context
            mock_channel2 = Mock()
            mock_channel2.name = 'PJSIP/user456-00000002'
            mock_channel2.channelvars = Mock()
            mock_channel2.channelvars.get.return_value = None
            mock_channel2.dialplan = {'context': 'tenant_test456_context'}
            
            tenant_id2 = await self.handler._extract_tenant_from_channel(mock_channel2)
            self.assertEqual(tenant_id2, 'test456')
            
            # Mock channel with no tenant info
            mock_channel3 = Mock()
            mock_channel3.name = 'PJSIP/user789-00000003'
            mock_channel3.channelvars = Mock()
            mock_channel3.channelvars.get.return_value = None
            mock_channel3.dialplan = {'context': 'default'}
            
            tenant_id3 = await self.handler._extract_tenant_from_channel(mock_channel3)
            self.assertIsNone(tenant_id3)
        
        asyncio.run(run_test())
    
    @patch('core.ari_events.timezone.now')
    def test_handle_stasis_start_with_existing_session(self, mock_now):
        """Test handling StasisStart with existing session."""
        async def run_test():
            # Mock current time
            current_time = timezone.now()
            mock_now.return_value = current_time
            
            # Mock channel
            mock_channel = Mock()
            mock_channel.id = 'test-channel-123'
            mock_channel.name = 'PJSIP/user123-tenant_test123_inbound-00000001'
            mock_channel.caller = {'number': '1234567890', 'name': 'Test User'}
            
            # Mock existing session
            mock_session = Mock()
            mock_session.session_id = 'existing-session-123'
            mock_session.status = 'ringing'
            
            # Mock session manager
            self.handler.session_manager.get_session_by_channel = AsyncMock(return_value=mock_session)
            self.handler.session_manager.update_session = AsyncMock(return_value=True)
            
            # Mock event
            mock_event = {}
            
            # Handle the event
            await self.handler.handle_stasis_start(mock_channel, mock_event)
            
            # Verify stats were updated
            self.assertEqual(self.handler.stats.stasis_start_events, 1)
            self.assertEqual(self.handler.stats.total_events_processed, 1)
            self.assertEqual(self.handler.stats.last_event_time, current_time)
            
            # Verify session update was called
            self.handler.session_manager.update_session.assert_called_once()
        
        asyncio.run(run_test())


class ARIEventHandlerGlobalTest(TestCase):
    """Test global ARI event handler functions."""
    
    def test_get_ari_event_handler(self):
        """Test global ARI event handler retrieval."""
        async def run_test():
            # Get handler instance
            handler1 = await get_ari_event_handler()
            self.assertIsInstance(handler1, ARIEventHandler)
            
            # Should return same instance on subsequent calls
            handler2 = await get_ari_event_handler()
            self.assertIs(handler1, handler2)
            
            # Cleanup
            await cleanup_ari_event_handler()
        
        asyncio.run(run_test())
    
    def test_cleanup_ari_event_handler(self):
        """Test ARI event handler cleanup."""
        async def run_test():
            # Get handler instance
            handler = await get_ari_event_handler()
            self.assertIsNotNone(handler)
            
            # Add custom handler to verify cleanup
            mock_handler = Mock()
            handler.register_custom_handler('TestEvent', mock_handler)
            self.assertEqual(len(handler.custom_handlers), 1)
            
            # Cleanup
            await cleanup_ari_event_handler()
            
            # Should create new instance after cleanup
            handler2 = await get_ari_event_handler()
            self.assertIsInstance(handler2, ARIEventHandler)
            self.assertEqual(len(handler2.custom_handlers), 0)
            
            # Final cleanup
            await cleanup_ari_event_handler()
        
        asyncio.run(run_test())
