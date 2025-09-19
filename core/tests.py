from django.test import TestCase, TransactionTestCase
from django.contrib.auth.models import User
from unittest.mock import Mock, AsyncMock, patch, MagicMock
import asyncio
from datetime import datetime, timedelta
from django.utils import timezone

from .models import Tenant, SystemConfiguration, CallSession
from .ami_manager import (
    AMIConnectionConfig, 
    ConnectionStats, 
    AMIConnection, 
    AMIManager, 
    get_ami_manager,
    cleanup_ami_manager
)
from .ami_events import (
    AMIEventHandler,
    get_event_handler,
    setup_event_handlers_for_tenant,
    cleanup_event_handler
)
from .session_manager import CallSessionData


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
            subdomain="test",
            is_active=True
        )
        self.config = AMIConnectionConfig(
            host='localhost',
            port=5038,
            username='testuser',
            secret='testpass',
            timeout=10.0,
            max_retries=2,
            retry_delay=1.0,
            ping_interval=5.0
        )
    
    def test_init(self):
        """Test AMI connection initialization."""
        connection = AMIConnection(str(self.tenant.id), self.config)
        self.assertEqual(connection.tenant_id, str(self.tenant.id))
        self.assertEqual(connection.config, self.config)
        self.assertIsNone(connection.manager)
        self.assertIsInstance(connection.stats, ConnectionStats)
        self.assertFalse(connection.stats.is_healthy)
    
    @patch('core.ami_manager.panoramisk.Manager')
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
        self.tenant = Tenant.objects.create(
            name="Test Tenant",
            subdomain="test",
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
        self.assertEqual(config.port, 5038)
        self.assertEqual(config.username, 'testuser')
        self.assertEqual(config.secret, 'testpass')
    
    async def test_get_tenant_ami_config_missing_credentials(self):
        """Test configuration loading with missing credentials."""
        # Create tenant without credentials
        tenant2 = Tenant.objects.create(
            name="Test Tenant 2",
            subdomain="test2",
            is_active=True
        )
        SystemConfiguration.objects.create(
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
            subdomain="integration",
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
            self.assertEqual(config.port, 5038)
            self.assertEqual(config.username, 'testuser')
            self.assertEqual(config.secret, 'testpass')
            self.assertEqual(config.timeout, 30.0)
            self.assertEqual(config.max_retries, 3)
            self.assertEqual(config.retry_delay, 5.0)
            self.assertEqual(config.ping_interval, 30.0)
        
        asyncio.run(run_test())


class AMIEventHandlerTest(TransactionTestCase):
    """Test AMI event handler functionality."""
    
    def setUp(self):
        """Set up test data."""
        self.tenant = Tenant.objects.create(
            name="Event Test Tenant",
            subdomain="eventtest",
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
        self.assertEqual(stats['newchannel'], 0)
        self.assertEqual(stats['dial'], 0)
        self.assertEqual(stats['total'], 0)
        
        # Simulate processing events
        handler._event_statistics['newchannel'] += 1
        handler._event_statistics['dial'] += 2
        
        stats = handler.get_statistics()
        self.assertEqual(stats['newchannel'], 1)
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


class AMIEventHandlerNewchannelTest(TransactionTestCase):
    """Test Newchannel event handling."""
    
    def setUp(self):
        """Set up test data."""
        self.tenant = Tenant.objects.create(
            name="Newchannel Test Tenant",
            subdomain="newchannel",
            is_active=True
        )
        self.handler = AMIEventHandler()
    
    def test_newchannel_event_processing(self):
        """Test processing of Newchannel events."""
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
            await self.handler.handle_newchannel(None, message)
            
            # Verify session creation was called
            mock_session_manager.create_session.assert_called_once()
            
            # Verify session data
            call_args = mock_session_manager.create_session.call_args[0]
            session_data = call_args[0]
            self.assertEqual(session_data.session_id, 'test-unique-id-123')
            self.assertEqual(session_data.channel_id, 'PJSIP/user123-00000001')
            self.assertEqual(session_data.caller_id, '1234567890')
            self.assertEqual(session_data.caller_name, 'John Doe')
            self.assertEqual(session_data.dialed_number, '100')
            self.assertEqual(session_data.call_direction, 'inbound')
            self.assertEqual(session_data.status, 'ringing')
            
            # Verify statistics
            self.assertEqual(self.handler._event_statistics['newchannel'], 1)
        
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
