"""
AMI Command Execution with Error Handling

This module provides robust AMI command execution with comprehensive error handling,
retry logic, response validation, and high-level command abstractions for common
Asterisk management operations.
"""
from __future__ import annotations

import asyncio
import logging
import time
from asyncio import Task
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
from django.utils import timezone

from core.ami.ami_manager import get_ami_manager


logger = logging.getLogger(__name__)


class CommandStatus(Enum):
    """Status of AMI command execution."""
    PENDING = "pending"
    EXECUTING = "executing"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    RETRY = "retry"


@dataclass
class AMICommandConfig:
    """Configuration for AMI command execution."""
    timeout: float = 30.0
    max_retries: int = 3
    retry_delay: float = 2.0
    retry_exponential_backoff: bool = True
    validate_response: bool = True
    require_success_response: bool = True
    allowed_failure_responses: List[str] = field(default_factory=list)


@dataclass
class AMICommandResult:
    """Result of AMI command execution."""
    command: str
    action_id: Optional[str]
    parameters: Dict[str, Any]
    status: CommandStatus
    response: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    execution_time: Optional[float] = None
    retry_count: int = 0
    tenant_id: Optional[str] = None
    timestamp: datetime = field(default_factory=timezone.now)


@dataclass
class QueuedCommand:
    """Queued AMI command waiting for execution."""
    command: str
    parameters: Dict[str, Any]
    config: AMICommandConfig
    tenant_id: str
    priority: int = 100
    created_at: datetime = field(default_factory=timezone.now)
    callback: Optional[Callable[[AMICommandResult], None]] = None


class AMICommandExecutor:
    """Executes AMI commands with error handling and retry logic."""
    
    def __init__(self):
        self.ami_manager = None
        self._command_queue: List[QueuedCommand] = []
        self._active_commands: Dict[str, AMICommandResult] = {}
        self._command_history: List[AMICommandResult] = []
        self._queue_lock = asyncio.Lock()
        self._stats = {
            'commands_executed': 0,
            'commands_successful': 0,
            'commands_failed': 0,
            'commands_timeout': 0,
            'commands_retried': 0,
            'total_execution_time': 0.0
        }
        self._processing_queue = False
    
    async def initialize(self):
        """Initialize the command executor."""
        self.ami_manager = await get_ami_manager()
        
    async def execute_command(
        self, 
        tenant_id: str, 
        command: str, 
        parameters: Optional[Dict[str, Any]] = None,
        config: Optional[AMICommandConfig] = None
    ) -> AMICommandResult:
        """
        Execute an AMI command with error handling and retry logic.
        
        Args:
            tenant_id: Tenant ID for multi-tenant routing
            command: AMI action/command to execute
            parameters: Command parameters
            config: Execution configuration
            
        Returns:
            AMICommandResult with execution details
        """
        if not self.ami_manager:
            await self.initialize()
        
        if parameters is None:
            parameters = {}
        
        if config is None:
            config = AMICommandConfig()
        
        # Generate action ID for tracking
        action_id = f"{command}_{int(time.time() * 1000)}"
        
        # Create command result
        result = AMICommandResult(
            command=command,
            action_id=action_id,
            parameters=parameters,
            status=CommandStatus.PENDING,
            tenant_id=tenant_id
        )
        
        # Track active command
        self._active_commands[action_id] = result
        
        try:
            # Execute with retry logic
            await self._execute_with_retry(result, config)
            
        except Exception as e:
            result.status = CommandStatus.FAILED
            result.error_message = str(e)
            logger.error(f"Unexpected error executing command {command}: {e}")
            
        finally:
            # Remove from active commands and add to history
            if action_id in self._active_commands:
                del self._active_commands[action_id]
            
            self._add_to_history(result)
            self._update_stats(result)
        
        return result
    
    async def queue_command(
        self,
        tenant_id: str,
        command: str,
        parameters: Optional[Dict[str, Any]] = None,
        config: Optional[AMICommandConfig] = None,
        priority: int = 100,
        callback: Optional[Callable[[AMICommandResult], None]] = None
    ) -> str:
        """
        Queue a command for later execution.
        
        Args:
            tenant_id: Tenant ID
            command: AMI command
            parameters: Command parameters
            config: Execution configuration
            priority: Command priority (lower = higher priority)
            callback: Callback function for result
            
        Returns:
            Queue ID for tracking
        """
        if parameters is None:
            parameters = {}
        
        if config is None:
            config = AMICommandConfig()
        
        queued_command = QueuedCommand(
            command=command,
            parameters=parameters,
            config=config,
            tenant_id=tenant_id,
            priority=priority,
            callback=callback
        )
        
        async with self._queue_lock:
            self._command_queue.append(queued_command)
            # Sort by priority (lower number = higher priority)
            self._command_queue.sort(key=lambda x: x.priority)
        
        # Start processing if not already running
        if not self._processing_queue:
            asyncio.create_task(self._process_command_queue())
        
        return str(id(queued_command))
    
    async def _execute_with_retry(self, result: AMICommandResult, config: AMICommandConfig):
        """Execute command with retry logic."""
        start_time = time.time()
        
        for attempt in range(config.max_retries + 1):
            try:
                result.status = CommandStatus.EXECUTING
                result.retry_count = attempt
                
                # Calculate timeout for this attempt
                attempt_timeout = config.timeout
                
                # Execute the command
                logger.debug(f"Executing AMI command {result.command} (attempt {attempt + 1})")
                
                response = await asyncio.wait_for(
                    self.ami_manager.send_command(
                        result.tenant_id, 
                        result.command, 
                        ActionID=result.action_id,
                        **result.parameters
                    ),
                    timeout=attempt_timeout
                )
                
                # Validate response
                if await self._validate_response(response, config):
                    result.status = CommandStatus.SUCCESS
                    result.response = response
                    result.execution_time = time.time() - start_time
                    logger.debug(f"AMI command {result.command} completed successfully")
                    return
                else:
                    # Response validation failed
                    error_msg = f"Response validation failed: {response}"
                    logger.warning(error_msg)
                    
                    if attempt == config.max_retries:
                        result.status = CommandStatus.FAILED
                        result.error_message = error_msg
                        return
                    
                    # Retry
                    result.status = CommandStatus.RETRY
                    self._stats['commands_retried'] += 1
                    
            except asyncio.TimeoutError:
                error_msg = f"Command {result.command} timed out after {attempt_timeout} seconds"
                logger.warning(error_msg)
                
                if attempt == config.max_retries:
                    result.status = CommandStatus.TIMEOUT
                    result.error_message = error_msg
                    return
                
                result.status = CommandStatus.RETRY
                self._stats['commands_retried'] += 1
                
            except Exception as e:
                error_msg = f"Command {result.command} failed: {str(e)}"
                logger.error(error_msg)
                
                if attempt == config.max_retries:
                    result.status = CommandStatus.FAILED
                    result.error_message = error_msg
                    return
                
                result.status = CommandStatus.RETRY
                self._stats['commands_retried'] += 1
            
            # Wait before retry
            if attempt < config.max_retries:
                if config.retry_exponential_backoff:
                    delay = config.retry_delay * (2 ** attempt)
                else:
                    delay = config.retry_delay
                
                logger.debug(f"Retrying command {result.command} in {delay} seconds")
                await asyncio.sleep(delay)
        
        # If we get here, all retries failed
        result.execution_time = time.time() - start_time
    
    async def _validate_response(self, response: Optional[Dict[str, Any]], config: AMICommandConfig) -> bool:
        """Validate AMI command response."""
        if not config.validate_response:
            return True
        
        if response is None:
            return False
        
        # Check for success response
        if config.require_success_response:
            response_status = response.get('Response', '').lower()
            
            if response_status == 'success':
                return True
            
            # Check if this is an allowed failure response
            if response_status in [status.lower() for status in config.allowed_failure_responses]:
                return True
            
            return False
        
        # If no specific validation required, any response is valid
        return True
    
    async def _process_command_queue(self):
        """Process queued commands."""
        if self._processing_queue:
            return
        
        self._processing_queue = True
        
        try:
            while True:
                async with self._queue_lock:
                    if not self._command_queue:
                        break
                    
                    # Get highest priority command
                    queued_command = self._command_queue.pop(0)
                
                # Execute the command
                result = await self.execute_command(
                    queued_command.tenant_id,
                    queued_command.command,
                    queued_command.parameters,
                    queued_command.config
                )
                
                # Call callback if provided
                if queued_command.callback:
                    try:
                        queued_command.callback(result)
                    except Exception as e:
                        logger.error(f"Error in command callback: {e}")
        
        finally:
            self._processing_queue = False
    
    def _add_to_history(self, result: AMICommandResult):
        """Add command result to history."""
        self._command_history.append(result)
        
        # Keep only last 1000 commands in history
        if len(self._command_history) > 1000:
            self._command_history = self._command_history[-1000:]
    
    def _update_stats(self, result: AMICommandResult):
        """Update execution statistics."""
        self._stats['commands_executed'] += 1
        
        if result.status == CommandStatus.SUCCESS:
            self._stats['commands_successful'] += 1
        elif result.status == CommandStatus.FAILED:
            self._stats['commands_failed'] += 1
        elif result.status == CommandStatus.TIMEOUT:
            self._stats['commands_timeout'] += 1
        
        if result.execution_time:
            self._stats['total_execution_time'] += result.execution_time
    
    # High-level command abstractions
    
    async def ping(self, tenant_id: str) -> AMICommandResult:
        """Send ping command to check AMI connection."""
        return await self.execute_command(tenant_id, 'Ping')
    
    async def originate_call(
        self, 
        tenant_id: str, 
        channel: str, 
        context: str, 
        exten: str, 
        priority: int = 1,
        caller_id: Optional[str] = None,
        variables: Optional[Dict[str, str]] = None,
        timeout: int = 30000
    ) -> AMICommandResult:
        """Originate a new call."""
        parameters = {
            'Channel': channel,
            'Context': context,
            'Exten': exten,
            'Priority': priority,
            'Timeout': timeout
        }
        
        if caller_id:
            parameters['CallerID'] = caller_id
        
        if variables:
            for i, (key, value) in enumerate(variables.items(), 1):
                parameters[f'Variable{i}'] = f'{key}={value}'
        
        config = AMICommandConfig(timeout=40.0)  # Longer timeout for originate
        return await self.execute_command(tenant_id, 'Originate', parameters, config)
    
    async def hangup_channel(self, tenant_id: str, channel: str, cause: Optional[int] = None) -> AMICommandResult:
        """Hangup a channel."""
        parameters = {'Channel': channel}
        if cause is not None:
            parameters['Cause'] = cause
        
        return await self.execute_command(tenant_id, 'Hangup', parameters)
    
    async def get_channel_status(self, tenant_id: str, channel: str) -> AMICommandResult:
        """Get status of a channel."""
        parameters = {'Channel': channel}
        return await self.execute_command(tenant_id, 'Status', parameters)
    
    async def list_channels(self, tenant_id: str) -> AMICommandResult:
        """List all active channels."""
        return await self.execute_command(tenant_id, 'CoreShowChannels')
    
    async def set_channel_variable(
        self, 
        tenant_id: str, 
        channel: str, 
        variable: str, 
        value: str
    ) -> AMICommandResult:
        """Set a channel variable."""
        parameters = {
            'Channel': channel,
            'Variable': variable,
            'Value': value
        }
        return await self.execute_command(tenant_id, 'Setvar', parameters)
    
    async def get_channel_variable(self, tenant_id: str, channel: str, variable: str) -> AMICommandResult:
        """Get a channel variable."""
        parameters = {
            'Channel': channel,
            'Variable': variable
        }
        return await self.execute_command(tenant_id, 'Getvar', parameters)
    
    async def redirect_channel(
        self, 
        tenant_id: str, 
        channel: str, 
        context: str, 
        exten: str, 
        priority: int = 1
    ) -> AMICommandResult:
        """Redirect a channel to a new destination."""
        parameters = {
            'Channel': channel,
            'Context': context,
            'Exten': exten,
            'Priority': priority
        }
        return await self.execute_command(tenant_id, 'Redirect', parameters)
    
    async def monitor_start(
        self, 
        tenant_id: str, 
        channel: str, 
        filename: str, 
        format: str = 'wav',
        mix: bool = True
    ) -> AMICommandResult:
        """Start monitoring/recording a channel."""
        parameters = {
            'Channel': channel,
            'File': filename,
            'Format': format,
            'Mix': 'true' if mix else 'false'
        }
        return await self.execute_command(tenant_id, 'Monitor', parameters)
    
    async def monitor_stop(self, tenant_id: str, channel: str) -> AMICommandResult:
        """Stop monitoring a channel."""
        parameters = {'Channel': channel}
        return await self.execute_command(tenant_id, 'StopMonitor', parameters)
    
    # Statistics and monitoring
    
    def get_command_stats(self) -> Dict[str, Any]:
        """Get command execution statistics."""
        stats = self._stats.copy()
        stats['queue_size'] = len(self._command_queue)
        stats['active_commands'] = len(self._active_commands)
        stats['history_size'] = len(self._command_history)
        
        if stats['commands_executed'] > 0:
            stats['average_execution_time'] = stats['total_execution_time'] / stats['commands_executed']
            stats['success_rate'] = stats['commands_successful'] / stats['commands_executed']
        else:
            stats['average_execution_time'] = 0.0
            stats['success_rate'] = 0.0
        
        return stats
    
    def get_active_commands(self) -> List[AMICommandResult]:
        """Get currently executing commands."""
        return list(self._active_commands.values())
    
    def get_command_history(self, limit: int = 100) -> List[AMICommandResult]:
        """Get recent command execution history."""
        return self._command_history[-limit:] if limit > 0 else self._command_history.copy()
    
    def get_queue_status(self) -> Task[dict[str, int | bool | dict[str, int] | dict[int, int]]]:
        """Get command queue status."""
        async def _get_queue_info():
            async with self._queue_lock:
                return {
                    'queue_size': len(self._command_queue),
                    'processing': self._processing_queue,
                    'commands_by_tenant': self._get_queue_breakdown_by_tenant(),
                    'commands_by_priority': self._get_queue_breakdown_by_priority()
                }
        
        return asyncio.create_task(_get_queue_info())
    
    def _get_queue_breakdown_by_tenant(self) -> Dict[str, int]:
        """Get queue breakdown by tenant."""
        breakdown = {}
        for cmd in self._command_queue:
            breakdown[cmd.tenant_id] = breakdown.get(cmd.tenant_id, 0) + 1
        return breakdown
    
    def _get_queue_breakdown_by_priority(self) -> Dict[int, int]:
        """Get queue breakdown by priority."""
        breakdown = {}
        for cmd in self._command_queue:
            breakdown[cmd.priority] = breakdown.get(cmd.priority, 0) + 1
        return breakdown
    
    def clear_history(self):
        """Clear command execution history."""
        self._command_history.clear()
    
    def reset_stats(self):
        """Reset execution statistics."""
        for key in self._stats:
            if isinstance(self._stats[key], (int, float)):
                self._stats[key] = 0


# Global command executor instance
_command_executor: Optional[AMICommandExecutor] = None


async def get_command_executor() -> AMICommandExecutor:
    """Get or create global command executor instance."""
    global _command_executor
    if _command_executor is None:
        _command_executor = AMICommandExecutor()
        await _command_executor.initialize()
    return _command_executor


async def cleanup_command_executor():
    """Cleanup global command executor instance."""
    global _command_executor
    _command_executor = None
