"""Circuit breaker implementation for preventing cascade failures.

This module provides a circuit breaker pattern implementation to protect
against cascade failures when the Asterisk server becomes unavailable.
"""

import asyncio
import logging
import time
from enum import Enum
from typing import Any, Callable, Optional, Type, Union
from dataclasses import dataclass

from .exceptions import ARIError, ConnectionError

logger = logging.getLogger(__name__)


class CircuitBreakerState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Circuit is open, failing fast
    HALF_OPEN = "half_open"  # Testing if service is back


@dataclass
class CircuitBreakerStats:
    """Circuit breaker statistics."""
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: Optional[float] = None
    last_success_time: Optional[float] = None
    state_change_time: float = 0.0


class CircuitBreaker:
    """Circuit breaker for preventing cascade failures.

    This implementation follows the circuit breaker pattern:
    - CLOSED: Normal operation, failures are counted
    - OPEN: Circuit is open, calls fail immediately
    - HALF_OPEN: Testing state, allows one call through

    Args:
        failure_threshold: Number of failures to trigger opening
        recovery_timeout: Time to wait before trying to recover (seconds)
        expected_exception: Exception type that triggers the circuit breaker
        name: Name for logging purposes
    """

    def __init__(
            self,
            failure_threshold: int = 5,
            recovery_timeout: float = 60.0,
            expected_exception: Type[Exception] = ConnectionError,
            name: str = "ARI"
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception
        self.name = name

        self._state = CircuitBreakerState.CLOSED
        self._stats = CircuitBreakerStats()
        self._stats.state_change_time = time.time()
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitBreakerState:
        """Get current circuit breaker state."""
        return self._state

    @property
    def stats(self) -> CircuitBreakerStats:
        """Get circuit breaker statistics."""
        return self._stats

    async def call(self, func: Callable, *args, **kwargs) -> Any:
        """Execute a function call through the circuit breaker.

        Args:
            func: Function to execute
            *args: Positional arguments for the function
            **kwargs: Keyword arguments for the function

        Returns:
            Function result

        Raises:
            CircuitBreakerOpenError: If circuit is open
            Original exception: If function fails
        """
        async with self._lock:
            # Check if we should move from OPEN to HALF_OPEN
            if self._state == CircuitBreakerState.OPEN:
                if self._should_attempt_reset():
                    self._state = CircuitBreakerState.HALF_OPEN
                    self._stats.state_change_time = time.time()
                    logger.info(f"Circuit breaker '{self.name}' moved to HALF_OPEN state")
                else:
                    raise CircuitBreakerOpenError(
                        f"Circuit breaker '{self.name}' is OPEN. "
                        f"Will retry after {self.recovery_timeout}s"
                    )

        try:
            # Execute the function
            if asyncio.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)

            # Record success
            async with self._lock:
                await self._on_success()

            return result

        except Exception as e:
            # Check if this is an expected exception
            if isinstance(e, self.expected_exception):
                async with self._lock:
                    await self._on_failure()
            raise

    async def _on_success(self) -> None:
        """Handle successful call."""
        self._stats.success_count += 1
        self._stats.last_success_time = time.time()

        if self._state == CircuitBreakerState.HALF_OPEN:
            # Reset to CLOSED state after successful test
            self._state = CircuitBreakerState.CLOSED
            self._stats.failure_count = 0
            self._stats.state_change_time = time.time()
            logger.info(f"Circuit breaker '{self.name}' moved to CLOSED state")

    async def _on_failure(self) -> None:
        """Handle failed call."""
        self._stats.failure_count += 1
        self._stats.last_failure_time = time.time()

        if self._state == CircuitBreakerState.CLOSED:
            if self._stats.failure_count >= self.failure_threshold:
                # Open the circuit
                self._state = CircuitBreakerState.OPEN
                self._stats.state_change_time = time.time()
                logger.warning(
                    f"Circuit breaker '{self.name}' opened after "
                    f"{self._stats.failure_count} failures"
                )
        elif self._state == CircuitBreakerState.HALF_OPEN:
            # Back to OPEN state
            self._state = CircuitBreakerState.OPEN
            self._stats.state_change_time = time.time()
            logger.warning(
                f"Circuit breaker '{self.name}' returned to OPEN state "
                f"after failed test call"
            )

    def _should_attempt_reset(self) -> bool:
        """Check if we should attempt to reset from OPEN to HALF_OPEN."""
        return (
                time.time() - self._stats.state_change_time >= self.recovery_timeout
        )

    async def reset(self) -> None:
        """Manually reset the circuit breaker to CLOSED state."""
        async with self._lock:
            self._state = CircuitBreakerState.CLOSED
            self._stats.failure_count = 0
            self._stats.state_change_time = time.time()
            logger.info(f"Circuit breaker '{self.name}' manually reset to CLOSED state")

    def __str__(self) -> str:
        """String representation of circuit breaker."""
        return (
            f"CircuitBreaker(name='{self.name}', state={self._state.value}, "
            f"failures={self._stats.failure_count}, "
            f"successes={self._stats.success_count})"
        )


class CircuitBreakerOpenError(ARIError):
    """Exception raised when circuit breaker is open."""

    def __init__(self, message: str):
        super().__init__(message)
        self.circuit_breaker_open = True
