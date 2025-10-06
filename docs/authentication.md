# aiMediaGateway API Authentication Guide

## Overview

The aiMediaGateway API supports multiple authentication methods to accommodate different client types and integration scenarios. This multi-authentication strategy ensures flexibility while maintaining security across the multi-tenant PBX management system.

## Supported Authentication Methods

### 1. JWT Authentication (Recommended)

**Best for:** Web applications, mobile apps, and modern API clients

JWT (JSON Web Tokens) provide stateless authentication with automatic token expiration and refresh capabilities.

#### Configuration
- **Access Token Lifetime:** 1 hour
- **Refresh Token Lifetime:** 7 days
- **Token Rotation:** Enabled with blacklisting

#### Usage Examples

**Obtain Token:**
```bash
curl -X POST http://localhost:8000/api/v1/auth/token/ \
  -H "Content-Type: application/json" \
  -d '{
    "username": "your_username",
    "password": "your_password"
  }'
```

**Response:**
```json
{
  "access": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
  "refresh": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9..."
}
```

**Use Token in Requests:**
```bash
curl -X GET http://localhost:8000/api/v1/call-sessions/ \
  -H "Authorization: Bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9..."
```

**Refresh Token:**
```bash
curl -X POST http://localhost:8000/api/v1/auth/token/refresh/ \
  -H "Content-Type: application/json" \
  -d '{
    "refresh": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9..."
  }'
```

**Logout (Blacklist Token):**
```bash
curl -X POST http://localhost:8000/api/v1/auth/token/blacklist/ \
  -H "Content-Type: application/json" \
  -d '{
    "refresh": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9..."
  }'
```

### 2. Session Authentication

**Best for:** Web applications with Django session management

Uses Django's built-in session framework for authentication. Ideal for applications that already use Django's user authentication system.

#### Usage Examples

**Login via Django Admin or Custom View:**
```python
# In your Django view
from django.contrib.auth import authenticate, login

user = authenticate(username='username', password='password')
if user is not None:
    login(request, user)
```

**API Requests:**
Once logged in via Django session, API requests automatically include session cookies:

```javascript
// JavaScript example with session cookies
fetch('/api/v1/call-sessions/', {
  method: 'GET',
  credentials: 'include'  // Include session cookies
})
```

### 3. Token Authentication

**Best for:** Legacy systems and simple integrations

Uses Django REST Framework's built-in token authentication with permanent tokens.

#### Setup

**Create Token for User:**
```python
# In Django shell or management command
from django.contrib.auth.models import User
from rest_framework.authtoken.models import Token

user = User.objects.get(username='your_username')
token, created = Token.objects.get_or_create(user=user)
print(f"Token: {token.key}")
```

#### Usage Examples

```bash
curl -X GET http://localhost:8000/api/v1/call-sessions/ \
  -H "Authorization: Token 9944b09199c62bcf9418ad846dd0e4bbdfc6ee4b"
```

### 4. API Key Authentication

**Best for:** Third-party integrations and automated systems

Custom authentication method designed for programmatic access with tenant-specific API keys.

#### Features
- Tenant-specific API keys
- Automatic API user creation
- Support for header or query parameter authentication
- Enhanced tenant isolation

#### Setup

**Generate API Key for Tenant:**
```python
# In Django shell
import hashlib
import secrets
from core.models import Tenant

# Generate secure API key
api_key = secrets.token_urlsafe(32)
api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()

# Store hash in tenant model (add api_key_hash field to Tenant model)
tenant = Tenant.objects.get(slug='your-tenant')
tenant.api_key_hash = api_key_hash
tenant.api_access_enabled = True
tenant.save()

print(f"API Key: {api_key}")
```

#### Usage Examples

**Header Authentication:**
```bash
curl -X GET http://localhost:8000/api/v1/call-sessions/ \
  -H "Authorization: ApiKey your_api_key_here"
```

**Query Parameter Authentication:**
```bash
curl -X GET "http://localhost:8000/api/v1/call-sessions/?api_key=your_api_key_here"
```

### 5. Signed Request Authentication

**Best for:** High-security integrations and webhook callbacks

Cryptographic request signing using HMAC-SHA256 for maximum security.

#### Features
- Request signature verification
- Timestamp-based replay attack prevention
- Tenant-specific shared secrets
- Automatic webhook user creation

#### Setup

**Configure Webhook Secret:**
```python
# In Django shell
from core.models import Tenant
import secrets

tenant = Tenant.objects.get(slug='your-tenant')
tenant.webhook_secret = secrets.token_urlsafe(32)
tenant.save()
```

#### Usage Examples

**Python Client Example:**
```python
import hmac
import hashlib
import time
import requests

def make_signed_request(url, data, tenant_id, shared_secret):
    timestamp = str(int(time.time()))
    message = f"{timestamp}.{data}"
    
    signature = hmac.new(
        shared_secret.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    
    headers = {
        'X-Signature': signature,
        'X-Timestamp': timestamp,
        'X-Tenant-ID': str(tenant_id),
        'Content-Type': 'application/json'
    }
    
    return requests.post(url, data=data, headers=headers)

# Usage
response = make_signed_request(
    'http://localhost:8000/api/v1/call-sessions/',
    '{"caller_id": "1234567890"}',
    tenant_id=1,
    shared_secret='your_webhook_secret'
)
```

## Multi-Tenant Considerations

### Tenant Isolation

All authentication methods respect tenant boundaries:

- **JWT/Session/Token Auth:** Users are scoped to their tenant via UserProfile
- **API Key Auth:** API keys are tenant-specific
- **Signed Request Auth:** Shared secrets are per-tenant

### Tenant Context

The authentication middleware automatically sets tenant context:

```python
# In your API views, access tenant context
def my_api_view(request):
    if hasattr(request, 'tenant'):
        tenant = request.tenant  # Current user's tenant
        # Perform tenant-scoped operations
```

## Security Best Practices

### API Key Management
- Store API key hashes, never plain text keys
- Implement key rotation policies
- Monitor API key usage
- Disable compromised keys immediately

### JWT Security
- Use HTTPS in production
- Implement proper token storage on client side
- Monitor for token abuse
- Implement refresh token rotation

### Signed Request Security
- Use strong shared secrets (min 32 characters)
- Implement proper timestamp validation
- Monitor for signature validation failures
- Rotate shared secrets regularly

## Error Handling

### Common Authentication Errors

**401 Unauthorized:**
```json
{
  "detail": "Authentication credentials were not provided."
}
```

**401 Invalid Token:**
```json
{
  "detail": "Given token not valid for any token type",
  "code": "token_not_valid",
  "messages": [
    {
      "token_class": "AccessToken",
      "token_type": "access",
      "message": "Token is invalid or expired"
    }
  ]
}
```

**401 Invalid API Key:**
```json
{
  "detail": "Invalid API key"
}
```

**401 Invalid Signature:**
```json
{
  "detail": "Invalid request signature"
}
```

## Testing Authentication

### Unit Tests

```python
from django.test import TestCase
from rest_framework.test import APIClient
from django.contrib.auth.models import User
from core.models import Tenant, UserProfile

class AuthenticationTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.tenant = Tenant.objects.create(name="Test Tenant", slug="test")
        self.user = User.objects.create_user(username='testuser', password='testpass')
        self.profile = UserProfile.objects.create(user=self.user, tenant=self.tenant)
    
    def test_jwt_authentication(self):
        # Test JWT token generation and usage
        response = self.client.post('/api/v1/auth/token/', {
            'username': 'testuser',
            'password': 'testpass'
        })
        self.assertEqual(response.status_code, 200)
        
        token = response.json()['access']
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        
        response = self.client.get('/api/v1/call-sessions/')
        self.assertEqual(response.status_code, 200)
```

## Integration Examples

### JavaScript/React Application

```javascript
// JWT Authentication with automatic refresh
class APIClient {
  constructor(baseURL) {
    this.baseURL = baseURL;
    this.accessToken = localStorage.getItem('access_token');
    this.refreshToken = localStorage.getItem('refresh_token');
  }
  
  async request(endpoint, options = {}) {
    const url = `${this.baseURL}${endpoint}`;
    
    // Add authorization header if token exists
    if (this.accessToken) {
      options.headers = {
        ...options.headers,
        'Authorization': `Bearer ${this.accessToken}`
      };
    }
    
    let response = await fetch(url, options);
    
    // If token expired, try to refresh
    if (response.status === 401 && this.refreshToken) {
      const refreshed = await this.refreshAccessToken();
      if (refreshed) {
        options.headers['Authorization'] = `Bearer ${this.accessToken}`;
        response = await fetch(url, options);
      }
    }
    
    return response;
  }
  
  async login(username, password) {
    const response = await fetch(`${this.baseURL}/api/v1/auth/token/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password })
    });
    
    if (response.ok) {
      const data = await response.json();
      this.accessToken = data.access;
      this.refreshToken = data.refresh;
      localStorage.setItem('access_token', this.accessToken);
      localStorage.setItem('refresh_token', this.refreshToken);
    }
    
    return response;
  }
  
  async refreshAccessToken() {
    const response = await fetch(`${this.baseURL}/api/v1/auth/token/refresh/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh: this.refreshToken })
    });
    
    if (response.ok) {
      const data = await response.json();
      this.accessToken = data.access;
      localStorage.setItem('access_token', this.accessToken);
      return true;
    }
    
    return false;
  }
}
```

### Python Integration

```python
import requests
import time

class aiMediaGatewayClient:
    def __init__(self, base_url, api_key=None):
        self.base_url = base_url
        self.api_key = api_key
        self.session = requests.Session()
        
        if api_key:
            self.session.headers.update({
                'Authorization': f'ApiKey {api_key}'
            })
    
    def login_jwt(self, username, password):
        """Login using JWT authentication"""
        response = self.session.post(
            f'{self.base_url}/api/v1/auth/token/',
            json={'username': username, 'password': password}
        )
        
        if response.ok:
            tokens = response.json()
            self.session.headers.update({
                'Authorization': f'Bearer {tokens["access"]}'
            })
            return tokens
        
        return None
    
    def get_call_sessions(self):
        """Get all call sessions for authenticated tenant"""
        response = self.session.get(f'{self.base_url}/api/v1/call-sessions/')
        return response.json() if response.ok else None
    
    def create_call_session(self, session_data):
        """Create new call session"""
        response = self.session.post(
            f'{self.base_url}/api/v1/call-sessions/',
            json=session_data
        )
        return response.json() if response.ok else None

# Usage examples
# API Key authentication
client = aiMediaGatewayClient('http://localhost:8000', api_key='your_api_key')

# JWT authentication  
client = aiMediaGatewayClient('http://localhost:8000')
client.login_jwt('username', 'password')

# Use the client
sessions = client.get_call_sessions()
```

## Troubleshooting

### Common Issues

1. **CORS Issues with JWT**
   - Ensure CORS is properly configured for your frontend domain
   - Check that credentials are included in requests

2. **Token Expiration**
   - Implement automatic token refresh
   - Handle 401 responses gracefully

3. **API Key Not Working**
   - Verify API key is properly hashed and stored
   - Check that tenant has `api_access_enabled = True`

4. **Signed Request Failures**
   - Verify timestamp is within 5-minute window
   - Check signature generation algorithm
   - Ensure shared secret matches

### Debug Mode

Enable authentication debugging in Django settings:

```python
LOGGING = {
    'version': 1,
    'handlers': {
        'console': {
            'level': 'DEBUG',
            'class': 'logging.StreamHandler',
        },
    },
    'loggers': {
        'core.authentication': {
            'handlers': ['console'],
            'level': 'DEBUG',
        },
    },
}
```

---

**Last Updated:** October 6, 2025  
**Version:** 1.0  
**Compatibility:** aiMediaGateway v2.0+
