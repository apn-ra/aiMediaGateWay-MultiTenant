"""
AMI Event Filtering and Tenant Routing

This module provides advanced event filtering and tenant routing capabilities
for AMI events, including performance optimization, event throttling,
and configuration-based routing rules.
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional, Set, Callable, Any
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from django.utils import timezone
from django.conf import settings

from core.models import Tenant, SystemConfiguration


logger = logging.getLogger(__name__)


@dataclass
class EventFilter:
    """Configuration for event filtering."""
    name: str
    event_types: Set[str] = field(default_factory=set)  # Events to include/exclude
    channels: Set[str] = field(default_factory=set)     # Channel patterns to match
    contexts: Set[str] = field(default_factory=set)     # Context patterns to match
    variables: Dict[str, str] = field(default_factory=dict)  # Variable conditions
    tenant_ids: Set[str] = field(default_factory=set)   # Specific tenants
    is_include_filter: bool = True  # True for whitelist, False for blacklist
    is_enabled: bool = True
    priority: int = 100  # Lower number = higher priority


@dataclass
class TenantRoutingRule:
    """Routing rule for tenant-specific event handling."""
    tenant_id: str
    context_patterns: List[str] = field(default_factory=list)
    channel_patterns: List[str] = field(default_factory=list)
    variable_mappings: Dict[str, str] = field(default_factory=dict)
    event_priority: Dict[str, int] = field(default_factory=dict)
    max_events_per_second: Optional[int] = None
    is_enabled: bool = True


@dataclass
class EventThrottlingConfig:
    """Configuration for event throttling."""
    max_events_per_second: int = 100
    max_events_per_minute: int = 1000
    burst_allowance: int = 50
    throttle_window_seconds: int = 60
    backpressure_threshold: float = 0.8  # When to start applying backpressure


class EventThrottler:
    """Throttles events to prevent system overload."""
    
    def __init__(self, config: EventThrottlingConfig):
        self.config = config
        self._event_timestamps: List[float] = []
        self._per_tenant_counts: Dict[str, List[float]] = {}
        self._lock = asyncio.Lock()
    
    async def should_process_event(self, tenant_id: str, event_type: str) -> bool:
        """Check if event should be processed based on throttling rules."""
        async with self._lock:
            now = time.time()
            
            # Clean old timestamps
            cutoff_time = now - self.config.throttle_window_seconds
            self._event_timestamps = [ts for ts in self._event_timestamps if ts > cutoff_time]
            
            # Check global rate limit
            if len(self._event_timestamps) >= self.config.max_events_per_second:
                logger.warning(f"Global event rate limit exceeded: {len(self._event_timestamps)} events/second")
                return False
            
            # Check tenant-specific rate limit
            if tenant_id not in self._per_tenant_counts:
                self._per_tenant_counts[tenant_id] = []
            
            tenant_events = self._per_tenant_counts[tenant_id]
            tenant_events = [ts for ts in tenant_events if ts > cutoff_time]
            self._per_tenant_counts[tenant_id] = tenant_events
            
            # Apply tenant-specific limits (default to global / number of active tenants)
            tenant_limit = self.config.max_events_per_second // max(1, len(self._per_tenant_counts))
            if len(tenant_events) >= tenant_limit:
                logger.warning(f"Tenant {tenant_id} event rate limit exceeded: {len(tenant_events)} events/second")
                return False
            
            # Record event
            self._event_timestamps.append(now)
            tenant_events.append(now)
            
            return True
    
    def get_throttling_stats(self) -> Dict[str, Any]:
        """Get current throttling statistics."""
        now = time.time()
        cutoff_time = now - self.config.throttle_window_seconds
        
        active_events = [ts for ts in self._event_timestamps if ts > cutoff_time]
        
        tenant_stats = {}
        for tenant_id, events in self._per_tenant_counts.items():
            active_tenant_events = [ts for ts in events if ts > cutoff_time]
            tenant_stats[tenant_id] = len(active_tenant_events)
        
        return {
            'total_events_per_window': len(active_events),
            'events_per_second': len(active_events) / self.config.throttle_window_seconds,
            'tenant_events': tenant_stats,
            'backpressure_active': len(active_events) > (self.config.max_events_per_second * self.config.backpressure_threshold)
        }


class AMIEventRouter:
    """Routes AMI events based on tenant and filtering rules."""
    
    def __init__(self):
        self.filters: List[EventFilter] = []
        self.tenant_rules: Dict[str, TenantRoutingRule] = {}
        self.throttler = EventThrottler(EventThrottlingConfig())
        self._tenant_cache: Dict[str, str] = {}
        self._stats = {
            'events_processed': 0,
            'events_filtered': 0,
            'events_throttled': 0,
            'routing_cache_hits': 0,
            'routing_cache_misses': 0
        }
    
    async def initialize(self):
        """Initialize the router with configuration from database."""
        await self._load_filters_from_config()
        await self._load_tenant_routing_rules()
    
    async def should_process_event(self, event_type: str, message: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        """
        Determine if an event should be processed and identify the tenant.
        
        Returns:
            tuple: (should_process, tenant_id)
        """
        # Extract basic information
        channel = message.get('Channel', '')
        context = message.get('Context', '')
        uniqueid = message.get('Uniqueid', '')
        
        # Determine tenant
        tenant_id = await self._route_to_tenant(context, channel, message)
        if not tenant_id:
            logger.debug(f"No tenant found for event {event_type}")
            return False, None
        
        # Check throttling
        if not await self.throttler.should_process_event(tenant_id, event_type):
            self._stats['events_throttled'] += 1
            return False, tenant_id
        
        # Apply filters
        if not await self._passes_filters(event_type, message, tenant_id):
            self._stats['events_filtered'] += 1
            return False, tenant_id
        
        self._stats['events_processed'] += 1
        return True, tenant_id
    
    async def _route_to_tenant(self, context: str, channel: str, message: Dict[str, Any]) -> Optional[str]:
        """Route event to appropriate tenant based on context and channel."""
        # Check cache first
        cache_key = f"{context}:{channel}"
        if cache_key in self._tenant_cache:
            self._stats['routing_cache_hits'] += 1
            return self._tenant_cache[cache_key]
        
        self._stats['routing_cache_misses'] += 1
        
        # Try tenant-specific routing rules first
        for tenant_id, rule in self.tenant_rules.items():
            if not rule.is_enabled:
                continue
                
            # Check context patterns
            for pattern in rule.context_patterns:
                if self._matches_pattern(context, pattern):
                    self._tenant_cache[cache_key] = tenant_id
                    return tenant_id
            
            # Check channel patterns
            for pattern in rule.channel_patterns:
                if self._matches_pattern(channel, pattern):
                    self._tenant_cache[cache_key] = tenant_id
                    return tenant_id
            
            # Check variable mappings
            for var_name, expected_value in rule.variable_mappings.items():
                if message.get(var_name) == expected_value:
                    self._tenant_cache[cache_key] = tenant_id
                    return tenant_id
        
        # Fallback to generic tenant extraction
        tenant_id = await self._extract_tenant_generic(context, channel, message)
        if tenant_id:
            self._tenant_cache[cache_key] = tenant_id
        
        return tenant_id
    
    async def _extract_tenant_generic(self, context: str, channel: str, message: Dict[str, Any]) -> Optional[str]:
        """Generic tenant extraction logic."""
        import re
        
        # Try to extract from context (e.g., "tenant_123_inbound")
        if context and 'tenant_' in context.lower():
            match = re.search(r'tenant[_-]?(\w+)', context, re.IGNORECASE)
            if match:
                potential_tenant = match.group(1)
                if await self._validate_tenant(potential_tenant):
                    return potential_tenant
        
        # Try to extract from channel (e.g., "PJSIP/tenant123-00000001")
        if channel and '/' in channel:
            tech, rest = channel.split('/', 1)
            match = re.search(r'tenant[_-]?(\w+)', rest, re.IGNORECASE)
            if match:
                potential_tenant = match.group(1)
                if await self._validate_tenant(potential_tenant):
                    return potential_tenant
        
        # Check for explicit tenant variables
        tenant_vars = ['TENANT_ID', 'TenantID', 'tenant_id']
        for var in tenant_vars:
            if var in message and message[var]:
                if await self._validate_tenant(message[var]):
                    return message[var]
        
        # Default to first active tenant for single-tenant setups
        if not hasattr(self, '_default_tenant'):
            self._default_tenant = await self._get_default_tenant()
        
        return self._default_tenant
    
    async def _passes_filters(self, event_type: str, message: Dict[str, Any], tenant_id: str) -> bool:
        """Check if event passes all configured filters."""
        # Sort filters by priority (lower number = higher priority)
        sorted_filters = sorted([f for f in self.filters if f.is_enabled], key=lambda x: x.priority)
        
        for event_filter in sorted_filters:
            # Check if filter applies to this tenant
            if event_filter.tenant_ids and tenant_id not in event_filter.tenant_ids:
                continue
            
            filter_matches = await self._filter_matches_event(event_filter, event_type, message)
            
            if event_filter.is_include_filter:
                # Include filter: event must match to be processed
                if not filter_matches:
                    logger.debug(f"Event {event_type} rejected by include filter {event_filter.name}")
                    return False
            else:
                # Exclude filter: event must not match to be processed
                if filter_matches:
                    logger.debug(f"Event {event_type} rejected by exclude filter {event_filter.name}")
                    return False
        
        return True
    
    async def _filter_matches_event(self, event_filter: EventFilter, event_type: str, message: Dict[str, Any]) -> bool:
        """Check if a specific filter matches the event."""
        # Check event types
        if event_filter.event_types and event_type not in event_filter.event_types:
            return False
        
        # Check channel patterns
        if event_filter.channels:
            channel = message.get('Channel', '')
            if not any(self._matches_pattern(channel, pattern) for pattern in event_filter.channels):
                return False
        
        # Check context patterns
        if event_filter.contexts:
            context = message.get('Context', '')
            if not any(self._matches_pattern(context, pattern) for pattern in event_filter.contexts):
                return False
        
        # Check variable conditions
        for var_name, expected_value in event_filter.variables.items():
            if message.get(var_name) != expected_value:
                return False
        
        return True
    
    def _matches_pattern(self, text: str, pattern: str) -> bool:
        """Check if text matches pattern (supports wildcards)."""
        import re
        
        if not text or not pattern:
            return False
        
        # Convert shell-style wildcards to regex
        if '*' in pattern or '?' in pattern:
            regex_pattern = pattern.replace('*', '.*').replace('?', '.')
            return bool(re.match(f"^{regex_pattern}$", text, re.IGNORECASE))
        
        # Exact match (case-insensitive)
        return text.lower() == pattern.lower()
    
    async def _validate_tenant(self, tenant_id: str) -> bool:
        """Validate that a tenant exists and is active."""
        try:
            return await asyncio.to_thread(
                lambda: Tenant.objects.filter(
                    id=tenant_id,
                    is_active=True
                ).exists()
            )
        except Exception as e:
            logger.error(f"Error validating tenant {tenant_id}: {e}")
            return False
    
    async def _get_default_tenant(self) -> Optional[str]:
        """Get default tenant for single-tenant scenarios."""
        try:
            tenant = await asyncio.to_thread(
                lambda: Tenant.objects.filter(is_active=True).first()
            )
            return str(tenant.id) if tenant else None
        except Exception as e:
            logger.error(f"Error getting default tenant: {e}")
            return None
    
    async def _load_filters_from_config(self):
        """Load event filters from system configuration."""
        try:
            # This would typically load from SystemConfiguration table
            # For now, we'll set up some default filters
            
            # Filter to ignore noisy events
            noise_filter = EventFilter(
                name="noise_reduction",
                event_types={
                    'VarSet', 'NewExten', 'NewStateChange', 'PeerStatus',
                    'RegistryEntry', 'ContactStatus'
                },
                is_include_filter=False,  # Exclude these events
                priority=10
            )
            self.filters.append(noise_filter)
            
            # Filter to only process call-related events
            call_events_filter = EventFilter(
                name="call_events_only",
                event_types={
                    'Newchannel', 'Dial', 'DialEnd', 'Hangup', 'Answer',
                    'Bridge', 'BridgeCreate', 'BridgeDestroy', 'BridgeEnter', 'BridgeLeave'
                },
                is_include_filter=True,  # Only include these events
                priority=20
            )
            self.filters.append(call_events_filter)
            
            logger.info(f"Loaded {len(self.filters)} event filters")
            
        except Exception as e:
            logger.error(f"Error loading filters from config: {e}")
    
    async def _load_tenant_routing_rules(self):
        """Load tenant routing rules from system configuration."""
        try:
            tenants = await asyncio.to_thread(
                lambda: list(Tenant.objects.filter(is_active=True))
            )
            
            for tenant in tenants:
                # Load tenant-specific routing configuration
                tenant_configs = await asyncio.to_thread(
                    lambda: list(SystemConfiguration.objects.filter(
                        tenant=tenant,
                        key__startswith='routing_'
                    ))
                )
                
                config_dict = {config.key: config.get_typed_value() for config in tenant_configs}
                
                # Build routing rule
                rule = TenantRoutingRule(
                    tenant_id=str(tenant.id),
                    context_patterns=config_dict.get('routing_context_patterns', [f'tenant_{tenant.id}_*']),
                    channel_patterns=config_dict.get('routing_channel_patterns', [f'*/tenant{tenant.id}*']),
                    variable_mappings=config_dict.get('routing_variable_mappings', {'TENANT_ID': str(tenant.id)}),
                    max_events_per_second=config_dict.get('routing_max_events_per_second', None)
                )
                
                self.tenant_rules[str(tenant.id)] = rule
            
            logger.info(f"Loaded routing rules for {len(self.tenant_rules)} tenants")
            
        except Exception as e:
            logger.error(f"Error loading tenant routing rules: {e}")
    
    def add_filter(self, event_filter: EventFilter):
        """Add a new event filter."""
        self.filters.append(event_filter)
        logger.info(f"Added event filter: {event_filter.name}")
    
    def remove_filter(self, filter_name: str) -> bool:
        """Remove an event filter by name."""
        original_count = len(self.filters)
        self.filters = [f for f in self.filters if f.name != filter_name]
        removed = len(self.filters) < original_count
        if removed:
            logger.info(f"Removed event filter: {filter_name}")
        return removed
    
    def add_tenant_rule(self, rule: TenantRoutingRule):
        """Add or update a tenant routing rule."""
        self.tenant_rules[rule.tenant_id] = rule
        # Clear cache for this tenant
        keys_to_remove = [k for k in self._tenant_cache.keys() if self._tenant_cache[k] == rule.tenant_id]
        for key in keys_to_remove:
            del self._tenant_cache[key]
        logger.info(f"Added/updated routing rule for tenant {rule.tenant_id}")
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get routing and filtering statistics."""
        return {
            **self._stats,
            'active_filters': len([f for f in self.filters if f.is_enabled]),
            'tenant_rules': len(self.tenant_rules),
            'cache_size': len(self._tenant_cache),
            'throttling': self.throttler.get_throttling_stats()
        }
    
    def reset_statistics(self):
        """Reset all statistics."""
        for key in self._stats:
            self._stats[key] = 0
        self._tenant_cache.clear()


# Global router instance
_event_router: Optional[AMIEventRouter] = None


async def get_event_router() -> AMIEventRouter:
    """Get or create global event router instance."""
    global _event_router
    if _event_router is None:
        _event_router = AMIEventRouter()
        await _event_router.initialize()
    return _event_router


async def cleanup_event_router():
    """Cleanup global event router instance."""
    global _event_router
    _event_router = None
