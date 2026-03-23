"""
Unified CircuitBreaker pattern for handling cascading failures in external API connectors.
Used by business_central_connector, xero_connector, and odoo_connector.
"""
import time
import logging
from enum import Enum

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states"""
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Too many failures, rejecting calls
    HALF_OPEN = "half_open" # Testing if service is back


class CircuitBreaker:
    """Circuit breaker pattern for handling cascading failures"""

    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time = None
        self.state = CircuitState.CLOSED

    def call(self, func, *args, **kwargs):
        """Execute function with circuit breaker protection.

        Also available as call_with_circuit_breaker for backward compatibility.
        """
        if self.state == CircuitState.OPEN:
            if self.last_failure_time and \
               time.time() - self.last_failure_time > self.recovery_timeout:
                logger.info("Circuit breaker: Moving to HALF_OPEN state for testing")
                self.state = CircuitState.HALF_OPEN
                self.failure_count = 0
            else:
                raise Exception(
                    f"Circuit breaker is OPEN. Service unavailable. Retry after {self.recovery_timeout}s"
                )

        try:
            result = func(*args, **kwargs)

            if self.state == CircuitState.HALF_OPEN:
                logger.info("Circuit breaker: Recovery successful (CLOSED)")
                self.state = CircuitState.CLOSED
                self.failure_count = 0

            return result

        except Exception as e:
            self.failure_count += 1
            self.last_failure_time = time.time()

            logger.warning(
                f"Circuit breaker: Failure {self.failure_count}/{self.failure_threshold}"
            )

            if self.failure_count >= self.failure_threshold:
                self.state = CircuitState.OPEN
                logger.error(
                    f"Circuit breaker: OPEN after {self.failure_count} failures. "
                    f"Service unavailable for {self.recovery_timeout}s"
                )

            raise e

    # Backward compatibility alias
    call_with_circuit_breaker = call

    def reset(self):
        self.failure_count = 0
        self.last_failure_time = None
        self.state = CircuitState.CLOSED
