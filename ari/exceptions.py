"""Custom exceptions for the Asterisk ARI library.

This module provides a comprehensive exception hierarchy for different error types
that can occur when interacting with the Asterisk ARI interface.
"""

from typing import Optional, Dict, Any, Union
import json


class ARIError(Exception):
    """Base exception class for all ARI-related errors.

    This is the base class for all exceptions raised by the asterisk-ari library.
    All other exceptions in this module inherit from this class.

    Attributes:
        message: Human-readable error message
        details: Additional error details (optional)
        asterisk_response: Original response from Asterisk (optional)
    """

    def __init__(
            self,
            message: str,
            details: Optional[Dict[str, Any]] = None,
            asterisk_response: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Initialize ARIError.

        Args:
            message: Human-readable error message
            details: Additional error details
            asterisk_response: Original response from Asterisk
        """
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.asterisk_response = asterisk_response

    def __str__(self) -> str:
        """Return string representation of the error."""
        if self.details:
            return f"{self.message} (details: {self.details})"
        return self.message

    def __repr__(self) -> str:
        """Return detailed representation of the error."""
        return (
            f"{self.__class__.__name__}("
            f"message={self.message!r}, "
            f"details={self.details!r}, "
            f"asterisk_response={self.asterisk_response!r})"
        )


class ConnectionError(ARIError):
    """Exception raised when connection to Asterisk fails.

    This includes network connectivity issues, DNS resolution failures,
    connection timeouts, and other transport-level errors.

    Attributes:
        host: Asterisk host that connection failed to reach
        port: Asterisk port that connection failed to reach
        timeout: Connection timeout value (if applicable)
    """

    def __init__(
            self,
            message: str,
            host: Optional[str] = None,
            port: Optional[int] = None,
            timeout: Optional[float] = None,
            details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Initialize ConnectionError.

        Args:
            message: Human-readable error message
            host: Asterisk host
            port: Asterisk port
            timeout: Connection timeout value
            details: Additional error details
        """
        super().__init__(message, details)
        self.host = host
        self.port = port
        self.timeout = timeout

    def __str__(self) -> str:
        """Return string representation of the connection error."""
        parts = [self.message]
        if self.host:
            parts.append(f"host={self.host}")
        if self.port:
            parts.append(f"port={self.port}")
        if self.timeout:
            parts.append(f"timeout={self.timeout}s")

        if len(parts) > 1:
            return f"{parts[0]} ({', '.join(parts[1:])})"
        return parts[0]


class AuthenticationError(ARIError):
    """Exception raised when authentication with Asterisk fails.

    This includes invalid credentials, expired tokens, insufficient permissions,
    or other authentication-related issues.

    Attributes:
        username: Username used for authentication
        status_code: HTTP status code from authentication attempt
    """

    def __init__(
            self,
            message: str,
            username: Optional[str] = None,
            status_code: Optional[int] = None,
            details: Optional[Dict[str, Any]] = None,
            asterisk_response: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Initialize AuthenticationError.

        Args:
            message: Human-readable error message
            username: Username used for authentication
            status_code: HTTP status code from authentication attempt
            details: Additional error details
            asterisk_response: Original response from Asterisk
        """
        super().__init__(message, details, asterisk_response)
        self.username = username
        self.status_code = status_code

    def __str__(self) -> str:
        """Return string representation of the authentication error."""
        parts = [self.message]
        if self.username:
            parts.append(f"username={self.username}")
        if self.status_code:
            parts.append(f"status={self.status_code}")

        if len(parts) > 1:
            return f"{parts[0]} ({', '.join(parts[1:])})"
        return parts[0]


class HTTPError(ARIError):
    """Exception raised for HTTP-related errors.

    This includes 4xx client errors and 5xx server errors from the Asterisk
    REST API. The exception provides detailed information about the HTTP
    request and response.

    Attributes:
        status_code: HTTP status code
        method: HTTP method (GET, POST, etc.)
        url: Request URL
        response_text: Raw response text
        is_client_error: True if 4xx error, False if 5xx error
        is_server_error: True if 5xx error, False if 4xx error
    """

    def __init__(
            self,
            message: str,
            status_code: int,
            method: Optional[str] = None,
            url: Optional[str] = None,
            response_text: Optional[str] = None,
            details: Optional[Dict[str, Any]] = None,
            asterisk_response: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Initialize HTTPError.

        Args:
            message: Human-readable error message
            status_code: HTTP status code
            method: HTTP method
            url: Request URL
            response_text: Raw response text
            details: Additional error details
            asterisk_response: Original response from Asterisk
        """
        super().__init__(message, details, asterisk_response)
        self.status_code = status_code
        self.method = method
        self.url = url
        self.response_text = response_text

    @property
    def is_client_error(self) -> bool:
        """Return True if this is a 4xx client error."""
        return 400 <= self.status_code < 500

    @property
    def is_server_error(self) -> bool:
        """Return True if this is a 5xx server error."""
        return 500 <= self.status_code < 600

    def __str__(self) -> str:
        """Return string representation of the HTTP error."""
        parts = [f"{self.status_code}: {self.message}"]
        if self.method and self.url:
            parts.append(f"{self.method} {self.url}")
        elif self.method:
            parts.append(f"method={self.method}")
        elif self.url:
            parts.append(f"url={self.url}")

        if len(parts) > 1:
            return f"{parts[0]} ({', '.join(parts[1:])})"
        return parts[0]


class ResourceNotFoundError(HTTPError):
    """Exception raised when a requested ARI resource is not found.

    This is a specialized HTTPError for 404 Not Found responses when
    trying to access channels, bridges, endpoints, or other ARI resources.

    Attributes:
        resource_type: Type of resource (channel, bridge, etc.)
        resource_id: ID of the resource that was not found
    """

    def __init__(
            self,
            message: str,
            resource_type: Optional[str] = None,
            resource_id: Optional[str] = None,
            method: Optional[str] = None,
            url: Optional[str] = None,
            details: Optional[Dict[str, Any]] = None,
            asterisk_response: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Initialize ResourceNotFoundError.

        Args:
            message: Human-readable error message
            resource_type: Type of resource
            resource_id: ID of the resource
            method: HTTP method
            url: Request URL
            details: Additional error details
            asterisk_response: Original response from Asterisk
        """
        super().__init__(
            message, 404, method, url, None, details, asterisk_response
        )
        self.resource_type = resource_type
        self.resource_id = resource_id

    def __str__(self) -> str:
        """Return string representation of the resource not found error."""
        parts = [self.message]
        if self.resource_type and self.resource_id:
            parts.append(f"{self.resource_type}={self.resource_id}")
        elif self.resource_type:
            parts.append(f"type={self.resource_type}")
        elif self.resource_id:
            parts.append(f"id={self.resource_id}")

        if len(parts) > 1:
            return f"{parts[0]} ({', '.join(parts[1:])})"
        return parts[0]


class ValidationError(ARIError):
    """Exception raised when data validation fails.

    This includes Pydantic validation errors, parameter validation failures,
    and other data-related errors.

    Attributes:
        field_errors: Dictionary of field-specific validation errors
        invalid_value: The value that failed validation
    """

    def __init__(
            self,
            message: str,
            field_errors: Optional[Dict[str, str]] = None,
            invalid_value: Optional[Any] = None,
            details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Initialize ValidationError.

        Args:
            message: Human-readable error message
            field_errors: Field-specific validation errors
            invalid_value: Value that failed validation
            details: Additional error details
        """
        super().__init__(message, details)
        self.field_errors = field_errors or {}
        self.invalid_value = invalid_value

    def __str__(self) -> str:
        """Return string representation of the validation error."""
        if self.field_errors:
            field_msgs = [f"{field}: {error}" for field, error in self.field_errors.items()]
            return f"{self.message} (field errors: {', '.join(field_msgs)})"
        return self.message


class WebSocketError(ARIError):
    """Exception raised for WebSocket-related errors.

    This includes WebSocket connection failures, message parsing errors,
    and event handling issues.

    Attributes:
        close_code: WebSocket close code (if applicable)
        close_reason: WebSocket close reason (if applicable)
    """

    def __init__(
            self,
            message: str,
            close_code: Optional[int] = None,
            close_reason: Optional[str] = None,
            details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Initialize WebSocketError.

        Args:
            message: Human-readable error message
            close_code: WebSocket close code
            close_reason: WebSocket close reason
            details: Additional error details
        """
        super().__init__(message, details)
        self.close_code = close_code
        self.close_reason = close_reason

    def __str__(self) -> str:
        """Return string representation of the WebSocket error."""
        parts = [self.message]
        if self.close_code:
            parts.append(f"code={self.close_code}")
        if self.close_reason:
            parts.append(f"reason={self.close_reason}")

        if len(parts) > 1:
            return f"{parts[0]} ({', '.join(parts[1:])})"
        return parts[0]


def parse_asterisk_error(
        response_data: Union[Dict[str, Any], str],
        status_code: int,
        method: Optional[str] = None,
        url: Optional[str] = None,
) -> ARIError:
    """Parse Asterisk error response and return appropriate exception.

    This function analyzes error responses from Asterisk and returns
    the most appropriate exception type based on the error content.

    Args:
        response_data: Response data from Asterisk (dict or string)
        status_code: HTTP status code
        method: HTTP method
        url: Request URL

    Returns:
        Appropriate ARIError subclass instance

    Example:
        ```python
        try:
            response = await session.get(url)
            response.raise_for_status()
        except aiohttp.ClientResponseError as e:
            error_data = await response.json()
            raise parse_asterisk_error(error_data, e.status, "GET", url)
        ```
    """
    # Parse response data
    if isinstance(response_data, str):
        try:
            data = json.loads(response_data)
        except json.JSONDecodeError:
            data = {"message": response_data}
    else:
        data = response_data

    # Extract error message
    message = data.get("message", f"HTTP {status_code} error")

    # Determine exception type based on status code and content
    if status_code == 401:
        return AuthenticationError(
            message,
            status_code=status_code,
            asterisk_response=data,
        )
    elif status_code == 404:
        # Try to determine resource type from URL
        resource_type = None
        resource_id = None
        if url:
            url_parts = url.strip("/").split("/")
            if len(url_parts) >= 2 and url_parts[-2] in ["channels", "bridges", "endpoints"]:
                resource_type = url_parts[-2][:-1]  # Remove 's' from plural
                resource_id = url_parts[-1]

        return ResourceNotFoundError(
            message,
            resource_type=resource_type,
            resource_id=resource_id,
            method=method,
            url=url,
            asterisk_response=data,
        )
    elif 400 <= status_code < 500:
        return HTTPError(
            message,
            status_code=status_code,
            method=method,
            url=url,
            asterisk_response=data,
        )
    elif 500 <= status_code < 600:
        return HTTPError(
            message,
            status_code=status_code,
            method=method,
            url=url,
            asterisk_response=data,
        )
    else:
        return ARIError(
            message,
            details={"status_code": status_code, "method": method, "url": url},
            asterisk_response=data,
        )
