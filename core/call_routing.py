"""
Call routing engine for tenant-based call handling

This module implements intelligent call routing based on tenant-specific configuration
stored in SystemConfiguration. Supports various routing strategies including:
- Extension-based routing
- DID (Direct Inward Dialing) routing
- Time-based routing
- Load balancing
- Priority routing
"""

import asyncio
import logging
import re
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass
from datetime import datetime, time
from django.utils import timezone
from django.db.models import Q

from .models import Tenant, SystemConfiguration, CallSession

logger = logging.getLogger(__name__)


@dataclass
class RoutingRule:
    """Individual routing rule configuration"""
    rule_id: str
    priority: int
    conditions: Dict[str, Any]
    actions: Dict[str, Any]
    enabled: bool = True
    description: str = ""


@dataclass
class RoutingDecision:
    """Result of routing decision process"""
    action: str  # 'answer', 'forward', 'reject', 'queue', 'voicemail'
    target: Optional[str] = None  # Target extension, number, or queue
    bridge_type: str = "mixing"
    recording_enabled: bool = True
    external_media_required: bool = True
    metadata: Dict[str, Any] = None


class CallRoutingEngine:
    """
    Intelligent call routing engine with tenant-specific configuration
    
    Handles routing decisions based on:
    - Caller ID patterns
    - Dialed number patterns
    - Time of day restrictions
    - Tenant-specific rules
    - Load balancing strategies
    """
    
    def __init__(self):
        self.routing_cache = {}  # Cache for frequently used routing rules
        self.cache_ttl = 300  # Cache TTL in seconds
        self.last_cache_update = {}
        logger.info("Call Routing Engine initialized")
    
    async def get_routing_decision(self, session_data: Dict[str, Any]) -> RoutingDecision:
        """
        Determine routing action for incoming call based on tenant configuration
        
        Args:
            session_data: Dictionary containing call session information
                - tenant_id: Tenant identifier
                - caller_id: Caller ID number
                - dialed_number: Number that was dialed
                - channel_id: Asterisk channel ID
                - direction: 'inbound' or 'outbound'
                - timestamp: Call timestamp
        
        Returns:
            RoutingDecision object with routing instructions
        """
        try:
            tenant_id = session_data.get('tenant_id')
            if not tenant_id:
                logger.warning("No tenant ID provided for routing decision")
                return self._get_default_routing_decision()
            
            # Get routing rules for tenant
            routing_rules = await self._get_tenant_routing_rules(tenant_id)
            if not routing_rules:
                logger.info(f"No routing rules found for tenant {tenant_id}, using default")
                return self._get_default_routing_decision()
            
            # Process rules in priority order
            for rule in sorted(routing_rules, key=lambda r: r.priority):
                if not rule.enabled:
                    continue
                
                if await self._evaluate_rule_conditions(rule, session_data):
                    logger.info(f"Routing rule {rule.rule_id} matched for tenant {tenant_id}")
                    return await self._execute_routing_actions(rule, session_data)
            
            # No rules matched, use default
            logger.info(f"No routing rules matched for tenant {tenant_id}, using default")
            return self._get_default_routing_decision()
            
        except Exception as e:
            logger.error(f"Error in routing decision process: {e}")
            return self._get_default_routing_decision()
    
    async def _get_tenant_routing_rules(self, tenant_id: str) -> List[RoutingRule]:
        """Get routing rules for specific tenant with caching"""
        cache_key = f"routing_rules_{tenant_id}"
        current_time = timezone.now()
        
        # Check cache
        if (cache_key in self.routing_cache and 
            cache_key in self.last_cache_update and
            (current_time - self.last_cache_update[cache_key]).seconds < self.cache_ttl):
            return self.routing_cache[cache_key]
        
        try:
            # Fetch routing rules from SystemConfiguration
            tenant = await Tenant.objects.aget(id=tenant_id)
            config_entries = SystemConfiguration.objects.filter(
                Q(tenant=tenant) | Q(tenant__isnull=True, scope='global'),
                key__startswith='routing_rule_',
                is_active=True
            ).order_by('key')
            
            routing_rules = []
            async for config in config_entries:
                if config.value_type == 'json':
                    try:
                        rule_data = config.get_typed_value()
                        rule = RoutingRule(
                            rule_id=config.key.replace('routing_rule_', ''),
                            priority=rule_data.get('priority', 100),
                            conditions=rule_data.get('conditions', {}),
                            actions=rule_data.get('actions', {}),
                            enabled=rule_data.get('enabled', True),
                            description=rule_data.get('description', '')
                        )
                        routing_rules.append(rule)
                    except Exception as e:
                        logger.error(f"Error parsing routing rule {config.key}: {e}")
            
            # Cache the results
            self.routing_cache[cache_key] = routing_rules
            self.last_cache_update[cache_key] = current_time
            
            logger.debug(f"Loaded {len(routing_rules)} routing rules for tenant {tenant_id}")
            return routing_rules
            
        except Exception as e:
            logger.error(f"Error loading routing rules for tenant {tenant_id}: {e}")
            return []
    
    async def _evaluate_rule_conditions(self, rule: RoutingRule, session_data: Dict[str, Any]) -> bool:
        """Evaluate if rule conditions match session data"""
        try:
            conditions = rule.conditions
            
            # Check caller ID pattern
            if 'caller_id_pattern' in conditions:
                caller_id = session_data.get('caller_id', '')
                pattern = conditions['caller_id_pattern']
                if not re.match(pattern, caller_id):
                    return False
            
            # Check dialed number pattern
            if 'dialed_number_pattern' in conditions:
                dialed_number = session_data.get('dialed_number', '')
                pattern = conditions['dialed_number_pattern']
                if not re.match(pattern, dialed_number):
                    return False
            
            # Check call direction
            if 'direction' in conditions:
                if session_data.get('direction') != conditions['direction']:
                    return False
            
            # Check time-based conditions
            if 'time_restrictions' in conditions:
                if not await self._check_time_restrictions(conditions['time_restrictions']):
                    return False
            
            # Check custom conditions
            if 'custom_conditions' in conditions:
                for condition_name, condition_value in conditions['custom_conditions'].items():
                    if not await self._evaluate_custom_condition(
                        condition_name, condition_value, session_data):
                        return False
            
            return True
            
        except Exception as e:
            logger.error(f"Error evaluating rule conditions for rule {rule.rule_id}: {e}")
            return False
    
    async def _check_time_restrictions(self, time_restrictions: Dict[str, Any]) -> bool:
        """Check if current time matches time restrictions"""
        try:
            current_time = timezone.now()
            current_weekday = current_time.weekday()  # 0=Monday, 6=Sunday
            current_time_only = current_time.time()
            
            # Check weekday restrictions
            if 'weekdays' in time_restrictions:
                allowed_weekdays = time_restrictions['weekdays']
                if current_weekday not in allowed_weekdays:
                    return False
            
            # Check time range restrictions
            if 'time_range' in time_restrictions:
                time_range = time_restrictions['time_range']
                start_time = time.fromisoformat(time_range['start'])
                end_time = time.fromisoformat(time_range['end'])
                
                if start_time <= end_time:
                    # Same day range
                    if not (start_time <= current_time_only <= end_time):
                        return False
                else:
                    # Overnight range
                    if not (current_time_only >= start_time or current_time_only <= end_time):
                        return False
            
            return True
            
        except Exception as e:
            logger.error(f"Error checking time restrictions: {e}")
            return True  # Allow call on error
    
    async def _evaluate_custom_condition(self, condition_name: str, 
                                       condition_value: Any, 
                                       session_data: Dict[str, Any]) -> bool:
        """Evaluate custom routing conditions"""
        try:
            if condition_name == 'max_concurrent_calls':
                # Check if tenant has reached maximum concurrent calls
                tenant_id = session_data.get('tenant_id')
                active_calls = await CallSession.objects.filter(
                    tenant_id=tenant_id,
                    status__in=['answered', 'bridged', 'recording']
                ).acount()
                return active_calls < condition_value
            
            elif condition_name == 'caller_whitelist':
                caller_id = session_data.get('caller_id', '')
                return caller_id in condition_value
            
            elif condition_name == 'caller_blacklist':
                caller_id = session_data.get('caller_id', '')
                return caller_id not in condition_value
            
            # Add more custom conditions as needed
            logger.warning(f"Unknown custom condition: {condition_name}")
            return True
            
        except Exception as e:
            logger.error(f"Error evaluating custom condition {condition_name}: {e}")
            return True  # Allow call on error
    
    async def _execute_routing_actions(self, rule: RoutingRule, 
                                     session_data: Dict[str, Any]) -> RoutingDecision:
        """Execute routing actions based on rule"""
        try:
            actions = rule.actions
            action = actions.get('action', 'answer')
            
            routing_decision = RoutingDecision(
                action=action,
                target=actions.get('target'),
                bridge_type=actions.get('bridge_type', 'mixing'),
                recording_enabled=actions.get('recording_enabled', True),
                external_media_required=actions.get('external_media_required', True),
                metadata={
                    'rule_id': rule.rule_id,
                    'rule_description': rule.description,
                    'routing_timestamp': timezone.now().isoformat()
                }
            )
            
            # Add custom metadata from rule
            if 'metadata' in actions:
                routing_decision.metadata.update(actions['metadata'])
            
            logger.info(f"Routing decision: {action} for rule {rule.rule_id}")
            return routing_decision
            
        except Exception as e:
            logger.error(f"Error executing routing actions for rule {rule.rule_id}: {e}")
            return self._get_default_routing_decision()
    
    def _get_default_routing_decision(self) -> RoutingDecision:
        """Get default routing decision when no rules match or on error"""
        return RoutingDecision(
            action='answer',
            target=None,
            bridge_type='mixing',
            recording_enabled=True,
            external_media_required=True,
            metadata={
                'rule_id': 'default',
                'rule_description': 'Default routing - answer and record',
                'routing_timestamp': timezone.now().isoformat()
            }
        )
    
    async def clear_routing_cache(self, tenant_id: Optional[str] = None):
        """Clear routing cache for specific tenant or all tenants"""
        if tenant_id:
            cache_key = f"routing_rules_{tenant_id}"
            self.routing_cache.pop(cache_key, None)
            self.last_cache_update.pop(cache_key, None)
            logger.info(f"Cleared routing cache for tenant {tenant_id}")
        else:
            self.routing_cache.clear()
            self.last_cache_update.clear()
            logger.info("Cleared all routing cache")
    
    async def add_routing_rule(self, tenant_id: str, rule_id: str, rule_config: Dict[str, Any]):
        """Add or update a routing rule for a tenant"""
        try:
            tenant = await Tenant.objects.aget(id=tenant_id)
            config_key = f"routing_rule_{rule_id}"
            
            # Create or update SystemConfiguration entry
            config, created = await SystemConfiguration.objects.aupdate_or_create(
                tenant=tenant,
                key=config_key,
                defaults={
                    'value': str(rule_config),
                    'value_type': 'json',
                    'description': f'Routing rule: {rule_id}',
                    'scope': 'tenant',
                    'is_active': True
                }
            )
            
            # Clear cache for this tenant
            await self.clear_routing_cache(tenant_id)
            
            action = "created" if created else "updated"
            logger.info(f"Routing rule {rule_id} {action} for tenant {tenant_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error adding routing rule {rule_id} for tenant {tenant_id}: {e}")
            return False


# Global routing engine instance
_routing_engine = None


def get_routing_engine() -> CallRoutingEngine:
    """Get global routing engine instance"""
    global _routing_engine
    if _routing_engine is None:
        _routing_engine = CallRoutingEngine()
    return _routing_engine


async def cleanup_routing_engine():
    """Cleanup routing engine resources"""
    global _routing_engine
    if _routing_engine:
        await _routing_engine.clear_routing_cache()
        _routing_engine = None
        logger.info("Call routing engine cleaned up")
