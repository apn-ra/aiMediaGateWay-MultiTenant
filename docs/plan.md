# Multi-Tenant Asterisk PBX Management System with Real-Time Audio Streaming

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [System Architecture & Data Flow](#system-architecture--data-flow)
3. [Key Components & Rationale](#key-components--rationale)
4. [Development Phases & Milestones](#development-phases--milestones)
5. [Security Considerations](#security-considerations)
6. [Testing Strategy](#testing-strategy)
7. [Deployment](#deployment)

## Executive Summary

This document outlines the development plan for a multi-tenant Asterisk PBX management system with real-time audio streaming capabilities. The system leverages Panoramisk 1.4 (AMI), python-ari 0.1.3 (ARI), and Django Channels (ASGI) to provide early call detection, programmatic call control, and real-time audio capture through a custom RTP server.

The architecture implements a session-first approach where AMI events trigger session creation before RTP streaming begins, ensuring zero audio loss and seamless multi-tenant operation.

## System Architecture & Data Flow

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Web Interface Layer                        │
│              (Django Admin + WebSocket Dashboard)              │
└────────────────────┬────────────────────────────────────────────┘
                     │
┌────────────────────┴────────────────────────────────────────────┐
│                 Application Layer                               │
│  ┌──────────────┐  ┌──────────────┐  ┌─────────────────────────┐│
│  │   Session    │  │    Django    │  │     Custom RTP          ││
│  │   Manager    │  │   Channels   │  │     Server              ││
│  │   (Redis)    │  │ (WebSocket)  │  │  (asyncio UDP)          ││
│  └──────────────┘  └──────────────┘  └─────────────────────────┘│
└────────────────────┬────────────────────────────────────────────┘
                     │
┌────────────────────┴────────────────────────────────────────────┐
│              Communication Layer                                │
│  ┌──────────────┐           ┌────────────────────────────────┐   │
│  │ Panoramisk   │           │        ARI Client              │   │
│  │ (AMI Events) │───────────│   (Call Control + Bridge)     │   │
│  │              │           │                                │   │
│  └──────────────┘           └────────────────────────────────┘   │
└────────────────────┬────────────────────────────────────────────┘
                     │
┌────────────────────┴────────────────────────────────────────────┐
│                   Asterisk Layer                               │
│  ┌──────────────┐  ┌──────────────┐  ┌─────────────────────────┐│
│  │  Asterisk    │  │ ExternalMedia│  │     Multi-Tenant        ││
│  │  Instance    │  │   Channels   │  │     Isolation           ││
│  │              │  │              │  │                         ││
│  └──────────────┘  └──────────────┘  └─────────────────────────┘│
└─────────────────────────────────────────────────────────────────┘
```

### Data Flow Architecture

#### 1. Call Detection & Session Creation Flow
```
Asterisk Newchannel/Dial → Panoramisk AMI → Session Manager → Redis
                                                  ↓
                                            Call Session Created
                                            (Before RTP starts)
```

#### 2. Call Control & Media Bridging Flow
```
ARI Answer Call → Create ExternalMedia Channel → Bridge to Caller
                                ↓
                    Custom RTP Server Endpoint
                           ↓
                Session Manager Attachment → Audio Processing
```

#### 3. Real-Time Notification Flow
```
Session Events → Django Channels → WebSocket → Dashboard Updates
                      ↓
                 Redis Pub/Sub → Multi-Instance Sync
```

### Session Management Architecture

The session manager operates on a pre-creation principle:

1. **Early Detection**: AMI Newchannel events detect calls before answer
2. **Session Pre-Creation**: Sessions created with caller metadata (ID, dialed number)
3. **RTP Attachment**: When ExternalMedia starts, RTP attaches to existing session
4. **Zero Loss**: No audio packets lost during session establishment

## Key Components & Rationale

### Django Channels (ASGI) - WebSocket Notifications & Async Integration

**Rationale**: 
- **Real-Time Requirements**: Essential for live call monitoring and dashboard updates
- **Async Event Loop Integration**: Seamlessly integrates with asyncio-based RTP server and AMI clients
- **WebSocket Support**: Enables bidirectional real-time communication with web clients
- **Channel Layers**: Provides distributed messaging for multi-instance deployments
- **Session Management**: Built-in session handling for WebSocket connections

**Key Features**:
- Real-time call status notifications
- Live audio streaming metrics
- Multi-tenant dashboard updates
- Event broadcasting across server instances

### Panoramisk 1.4 (AMI) - Fast Call Event Detection

**Rationale**:
- **Early Detection**: Newchannel/Dial events provide immediate call awareness before answer
- **Rich Metadata**: Access to caller ID, dialed number, channel variables, and call context
- **Async Architecture**: Native asyncio support for non-blocking event processing
- **Connection Reliability**: Automatic reconnection and connection pool management
- **Event Filtering**: Selective event subscription to reduce network overhead

**Key Implementation**:
```python
import asyncio
from panoramisk import Manager

class AMIEventHandler:
    def __init__(self, session_manager):
        self.session_manager = session_manager
        self.manager = Manager(
            host='asterisk-server',
            port=5038,
            username='ami_user',
            secret='ami_password'
        )
    
    async def handle_newchannel(self, manager, event):
        """Pre-create session on channel creation"""
        session_id = event.get('Uniqueid')
        caller_id = event.get('CallerIDNum')
        dialed_number = event.get('Exten')
        
        await self.session_manager.create_session(
            session_id=session_id,
            caller_id=caller_id,
            dialed_number=dialed_number,
            tenant_id=self.extract_tenant_id(event)
        )
```

### ARI (Asterisk REST Interface) - Programmatic Call Control

**Rationale**:
- **ExternalMedia Support**: Native support for bridging external media channels
- **RESTful API**: Modern HTTP-based interface for call control operations
- **WebSocket Events**: Real-time event streaming for application state synchronization
- **Bridge Management**: Sophisticated call bridging and media handling
- **Channel Control**: Complete control over channel lifecycle and media routing

**ExternalMedia Implementation**:
```python
import ari

class ARICallController:
    def __init__(self, ari_client, rtp_server):
        self.ari = ari_client
        self.rtp_server = rtp_server
    
    async def handle_incoming_call(self, channel, session_id):
        """Answer call and bridge to external media"""
        # Answer the incoming call
        channel.answer()
        
        # Create external media channel pointing to our RTP server
        external_media = self.ari.channels.externalMedia(
            app='media_gateway',
            external_host=f"{self.rtp_server.host}:{self.rtp_server.get_port(session_id)}",
            format='ulaw'  # or 'alaw', 'gsm', etc.
        )
        
        # Create bridge and add both channels
        bridge = self.ari.bridges.create(type='mixing')
        bridge.addChannel(channel=[channel.id, external_media.id])
        
        return bridge, external_media
```

### Custom RTP Server - Low-Latency Audio Capture

**Rationale**:
- **Direct UDP Processing**: Bypasses traditional SIP/RTP stack overhead
- **Session-Aware**: Integrates directly with session manager for context-aware processing
- **Asyncio Integration**: Non-blocking I/O for high-concurrency audio handling
- **Tenant Isolation**: Port-based isolation ensures tenant audio separation
- **Real-Time Processing**: Immediate audio packet processing and forwarding

**RTP Server Architecture**:
```python
import asyncio
import struct
from datetime import datetime

class CustomRTPServer:
    def __init__(self, session_manager, base_port=20000):
        self.session_manager = session_manager
        self.base_port = base_port
        self.active_sessions = {}
        self.servers = {}
    
    async def create_session_endpoint(self, session_id, tenant_id):
        """Create dedicated RTP endpoint for session"""
        port = self.base_port + hash(session_id) % 10000
        
        server = await asyncio.start_server(
            lambda reader, writer: self.handle_rtp_stream(reader, writer, session_id),
            '0.0.0.0', 
            port
        )
        
        self.servers[session_id] = server
        return port
    
    async def handle_rtp_stream(self, reader, writer, session_id):
        """Process incoming RTP packets"""
        session = await self.session_manager.get_session(session_id)
        
        while True:
            try:
                # Read RTP header (12 bytes minimum)
                rtp_header = await reader.read(12)
                if not rtp_header:
                    break
                
                # Parse RTP header
                version, payload_type, sequence, timestamp, ssrc = \
                    struct.unpack('!BBHII', rtp_header)
                
                # Read payload
                payload_length = int.from_bytes(await reader.read(2), 'big')
                payload = await reader.read(payload_length)
                
                # Process audio with session context
                await self.process_audio_packet(
                    session, payload, timestamp, sequence
                )
                
            except Exception as e:
                print(f"RTP processing error for session {session_id}: {e}")
                break
    
    async def process_audio_packet(self, session, audio_data, timestamp, sequence):
        """Process audio with session context"""
        # Add timestamp and session context
        processed_audio = {
            'session_id': session['id'],
            'tenant_id': session['tenant_id'],
            'caller_id': session['caller_id'],
            'audio_data': audio_data,
            'timestamp': timestamp,
            'sequence': sequence,
            'received_at': datetime.utcnow().isoformat()
        }
        
        # Forward to processing pipeline
        await self.session_manager.process_audio(processed_audio)
```

### Redis - Session Storage & Pub/Sub

**Rationale**:
- **Distributed Sessions**: Centralized session storage for multi-instance deployments
- **Pub/Sub Messaging**: Event distribution across application instances
- **High Performance**: Sub-millisecond session lookups for real-time processing
- **Persistence**: Configurable persistence for session recovery
- **Channel Layers**: Django Channels backend for WebSocket message distribution

**Session Storage Schema**:
```python
class SessionManager:
    def __init__(self, redis_client):
        self.redis = redis_client
        self.pubsub = redis_client.pubsub()
    
    async def create_session(self, session_id, caller_id, dialed_number, tenant_id):
        """Create session with metadata"""
        session_data = {
            'id': session_id,
            'caller_id': caller_id,
            'dialed_number': dialed_number,
            'tenant_id': tenant_id,
            'created_at': datetime.utcnow().isoformat(),
            'status': 'created',
            'rtp_port': None,
            'bridge_id': None
        }
        
        # Store session data
        await self.redis.hset(f"session:{session_id}", mapping=session_data)
        
        # Publish session creation event
        await self.redis.publish(
            f"tenant:{tenant_id}:sessions", 
            json.dumps({'event': 'session_created', 'data': session_data})
        )
        
        return session_data
    
    async def attach_rtp(self, session_id, rtp_port, bridge_id):
        """Attach RTP server to existing session"""
        await self.redis.hset(f"session:{session_id}", mapping={
            'rtp_port': rtp_port,
            'bridge_id': bridge_id,
            'status': 'active',
            'rtp_attached_at': datetime.utcnow().isoformat()
        })
        
        # Notify of RTP attachment
        session = await self.get_session(session_id)
        await self.redis.publish(
            f"tenant:{session['tenant_id']}:sessions",
            json.dumps({'event': 'rtp_attached', 'data': session})
        )
```

## Development Phases & Milestones

### Phase 1: Setup & Foundation (Weeks 1-2)

**Deliverables**:
- Django project setup with Channels integration
- Basic AMI/ARI connectivity
- Redis session storage implementation
- Multi-tenant data models

**Dependencies**: None

**Milestone 1.1: Project Infrastructure**
- [ ] Django Channels ASGI configuration
- [ ] Redis channel layer setup
- [ ] PostgreSQL database with tenant schemas
- [ ] Docker development environment
- [ ] Basic WebSocket consumer for testing

**Milestone 1.2: AMI Integration**
- [ ] Panoramisk AMI client setup
- [ ] Event handler for Newchannel/Dial events
- [ ] Session manager with Redis backend
- [ ] Basic session creation workflow
- [ ] AMI event filtering and routing

**Milestone 1.3: ARI Foundation**
- [ ] python-ari client configuration
- [ ] Basic call answering functionality
- [ ] Channel event subscription
- [ ] Error handling and reconnection logic

### Phase 2: Core Call Handling (Weeks 3-4)

**Deliverables**:
- Complete call detection and session creation
- Basic call control via ARI
- Session lifecycle management
- Real-time event notifications

**Dependencies**: Phase 1 completion

**Milestone 2.1: Session Management**
- [ ] Pre-session creation on AMI events
- [ ] Session metadata storage (caller ID, dialed number)
- [ ] Session state transitions (created → active → ended)
- [ ] Tenant-scoped session isolation
- [ ] Session cleanup and garbage collection

**Milestone 2.2: Call Control**
- [ ] Automated call answering via ARI
- [ ] Channel variable extraction
- [ ] Call routing and context management
- [ ] Bridge creation and management
- [ ] Call termination handling

**Milestone 2.3: Event Broadcasting**
- [ ] Django Channels WebSocket consumers
- [ ] Redis pub/sub event distribution
- [ ] Real-time session status updates
- [ ] Multi-tenant event isolation
- [ ] WebSocket authentication and authorization

### Phase 3: ExternalMedia & RTP Server (Weeks 5-6)

**Deliverables**:
- Custom asyncio UDP RTP server
- ExternalMedia channel integration
- Audio packet processing pipeline
- RTP session attachment to existing sessions

**Dependencies**: Phase 2 completion

**Milestone 3.1: Custom RTP Server**
- [ ] Asyncio UDP server implementation
- [ ] RTP packet parsing and validation
- [ ] Session-based port allocation
- [ ] Audio data extraction and processing
- [ ] Real-time packet statistics

**Milestone 3.2: ExternalMedia Integration**
- [ ] ARI ExternalMedia channel creation
- [ ] Dynamic RTP endpoint assignment
- [ ] Bridge integration (caller ↔ ExternalMedia)
- [ ] Audio format negotiation (ulaw/alaw)
- [ ] Media flow validation

**Milestone 3.3: Session-RTP Attachment**
- [ ] RTP server registration with session manager
- [ ] Automatic session-to-RTP binding
- [ ] Audio metadata enrichment with session context
- [ ] Near real-time audio processing
- [ ] RTP session cleanup on call end

### Phase 4: Real-Time Dashboards (Weeks 7-8)

**Deliverables**:
- Live call monitoring dashboard
- Real-time audio metrics
- WebSocket-based updates
- Multi-tenant dashboard isolation

**Dependencies**: Phase 3 completion

**Milestone 4.1: Dashboard Backend**
- [ ] WebSocket consumers for dashboard data
- [ ] Real-time metrics aggregation
- [ ] Session status broadcasting
- [ ] Audio quality metrics (jitter, packet loss)
- [ ] Call duration and volume statistics

**Milestone 4.2: Frontend Integration**
- [ ] WebSocket client implementation
- [ ] Live call status display
- [ ] Audio streaming visualizations
- [ ] Multi-tenant dashboard filtering
- [ ] Real-time notifications and alerts

**Milestone 4.3: Performance Optimization**
- [ ] WebSocket message batching
- [ ] Event filtering and throttling
- [ ] Dashboard update optimization
- [ ] Memory usage optimization
- [ ] Connection pool management

### Phase 5: Multi-Tenant Isolation (Weeks 9-10)

**Deliverables**:
- Complete tenant data isolation
- Tenant-specific Asterisk configurations
- Resource quota management
- Tenant provisioning automation

**Dependencies**: Phase 4 completion

**Milestone 5.1: Data Isolation**
- [ ] Database schema isolation per tenant
- [ ] Tenant-scoped ORM queries
- [ ] Session data segregation
- [ ] Audio data tenant tagging
- [ ] Cross-tenant access prevention

**Milestone 5.2: Infrastructure Isolation**
- [ ] Tenant-specific AMI credentials
- [ ] Separate ARI application contexts
- [ ] RTP port range allocation per tenant
- [ ] Asterisk instance isolation
- [ ] Resource usage tracking

**Milestone 5.3: Tenant Management**
- [ ] Tenant provisioning API
- [ ] Automated Asterisk configuration
- [ ] Tenant resource quotas
- [ ] Usage monitoring and alerting
- [ ] Tenant deprovisioning workflow

### Phase 6: Scaling & High Availability (Weeks 11-12)

**Deliverables**:
- Horizontal scaling architecture
- Load balancing configuration
- Database optimization
- Performance monitoring

**Dependencies**: Phase 5 completion

**Milestone 6.1: Application Scaling**
- [ ] Stateless application design
- [ ] Load balancer configuration
- [ ] Session affinity management
- [ ] Redis cluster setup
- [ ] Database connection pooling

**Milestone 6.2: RTP Server Scaling**
- [ ] Multi-instance RTP server deployment
- [ ] Dynamic port allocation
- [ ] RTP load balancing
- [ ] Session distribution strategies
- [ ] Audio processing pipeline scaling

**Milestone 6.3: Monitoring & Alerting**
- [ ] Comprehensive logging implementation
- [ ] Performance metrics collection
- [ ] Health check endpoints
- [ ] Automated alerting system
- [ ] Capacity planning metrics

### Estimated Timeline Summary

| Phase | Duration | Key Focus | Critical Path |
|-------|----------|-----------|---------------|
| Phase 1 | Weeks 1-2 | Foundation Setup | AMI/ARI integration |
| Phase 2 | Weeks 3-4 | Call Handling | Session management |
| Phase 3 | Weeks 5-6 | RTP & ExternalMedia | Custom RTP server |
| Phase 4 | Weeks 7-8 | Real-Time Dashboards | WebSocket optimization |
| Phase 5 | Weeks 9-10 | Multi-Tenant Isolation | Data segregation |
| Phase 6 | Weeks 11-12 | Scaling & HA | Performance optimization |

**Total Duration**: 12 weeks
**Team Size**: 2-3 developers
**Risk Buffers**: 15-20% additional time for integration challenges

## Security Considerations

### AMI/ARI Security & Network Protection

#### Secure Credential Management
```python
# Environment-based credential configuration
import os
from cryptography.fernet import Fernet

class SecureCredentialManager:
    def __init__(self):
        self.encryption_key = os.environ.get('CREDENTIAL_ENCRYPTION_KEY')
        self.cipher = Fernet(self.encryption_key.encode())
    
    def get_ami_credentials(self, tenant_id):
        """Retrieve encrypted AMI credentials per tenant"""
        encrypted_user = os.environ.get(f'AMI_USER_{tenant_id}')
        encrypted_pass = os.environ.get(f'AMI_PASS_{tenant_id}')
        
        return {
            'username': self.cipher.decrypt(encrypted_user.encode()).decode(),
            'password': self.cipher.decrypt(encrypted_pass.encode()).decode(),
            'host': os.environ.get(f'AMI_HOST_{tenant_id}'),
            'port': int(os.environ.get(f'AMI_PORT_{tenant_id}', '5038'))
        }
    
    def rotate_credentials(self, tenant_id):
        """Automated credential rotation"""
        # Implementation for periodic credential rotation
        pass
```

#### Network Security Measures
- **VPN-Only Access**: Administrative interfaces accessible only through VPN
- **Firewall Rules**: Strict ingress/egress controls for AMI (5038) and ARI (8088) ports
- **IP Whitelisting**: Restrict AMI/ARI access to specific application server IPs
- **Connection Encryption**: TLS encryption for all AMI/ARI communications
- **Certificate Management**: Automated SSL/TLS certificate rotation

#### AMI/ARI Connection Security
```python
# Secure AMI connection with TLS
from panoramisk import Manager
import ssl

class SecureAMIManager:
    def __init__(self, credentials):
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False  # For internal certificates
        
        self.manager = Manager(
            host=credentials['host'],
            port=credentials['port'],
            username=credentials['username'],
            secret=credentials['password'],
            ssl=ssl_context,  # Enable TLS encryption
            ping_delay=30,    # Keep-alive for connection monitoring
            reconnect_timeout=5
        )
    
    async def secure_login(self):
        """Enhanced login with connection validation"""
        try:
            await self.manager.connect()
            # Verify connection integrity
            response = await self.manager.send_action({'Action': 'Ping'})
            if response.get('Response') != 'Success':
                raise ConnectionError("AMI connection validation failed")
        except Exception as e:
            # Log security event
            await self.log_security_event('AMI_CONNECTION_FAILED', str(e))
            raise
```

### Tenant Data Isolation & Security

#### Database-Level Isolation
```python
# Enhanced tenant isolation with row-level security
class TenantSecurityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
    
    def __call__(self, request):
        # Extract tenant context from request
        tenant = self.extract_tenant(request)
        
        # Set database session variables for row-level security
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION app.current_tenant_id = %s", [tenant.id])
        
        response = self.get_response(request)
        return response
    
    def extract_tenant(self, request):
        """Secure tenant extraction from request context"""
        # Implementation depends on tenant identification strategy
        # (subdomain, JWT claim, etc.)
        pass
```

#### Session Data Security
```python
class SecureSessionManager:
    def __init__(self, redis_client):
        self.redis = redis_client
        self.encryption_key = os.environ.get('SESSION_ENCRYPTION_KEY')
        self.cipher = Fernet(self.encryption_key.encode())
    
    async def create_secure_session(self, session_data, tenant_id):
        """Create encrypted session with tenant isolation"""
        # Encrypt sensitive data
        encrypted_data = {
            'caller_id': self.cipher.encrypt(session_data['caller_id'].encode()).decode(),
            'dialed_number': self.cipher.encrypt(session_data['dialed_number'].encode()).decode(),
            'tenant_id': tenant_id,  # Not encrypted for routing
            'created_at': session_data['created_at'],
            'session_hash': hashlib.sha256(f"{tenant_id}:{session_data['id']}".encode()).hexdigest()
        }
        
        # Store with tenant-scoped key
        session_key = f"tenant:{tenant_id}:session:{session_data['id']}"
        await self.redis.hset(session_key, mapping=encrypted_data)
        
        # Set TTL for automatic cleanup
        await self.redis.expire(session_key, 3600)  # 1 hour default
        
        return encrypted_data
```

### SRTP and RTP Security

#### Secure RTP Implementation
```python
import hashlib
import hmac
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

class SecureRTPServer:
    def __init__(self, session_manager):
        self.session_manager = session_manager
        self.srtp_keys = {}  # Per-session SRTP keys
    
    async def create_secure_endpoint(self, session_id, tenant_id):
        """Create SRTP-enabled endpoint"""
        # Generate session-specific SRTP key
        srtp_key = self.generate_srtp_key(session_id, tenant_id)
        self.srtp_keys[session_id] = srtp_key
        
        # Configure Asterisk for SRTP
        ari_config = {
            'transport': 'udp',
            'encryption': 'sdes',
            'crypto': f'AES_CM_128_HMAC_SHA1_80 inline:{srtp_key}'
        }
        
        return ari_config
    
    def generate_srtp_key(self, session_id, tenant_id):
        """Generate cryptographically secure SRTP key"""
        key_material = f"{session_id}:{tenant_id}:{os.urandom(32).hex()}"
        return hashlib.pbkdf2_hmac('sha256', 
                                   key_material.encode(), 
                                   b'srtp_salt', 
                                   100000)[:30]  # 240-bit key
    
    async def decrypt_rtp_packet(self, encrypted_packet, session_id):
        """Decrypt SRTP packet"""
        if session_id not in self.srtp_keys:
            raise SecurityError(f"No SRTP key for session {session_id}")
        
        srtp_key = self.srtp_keys[session_id]
        
        # SRTP decryption implementation
        # (Simplified - use proper SRTP library in production)
        cipher = Cipher(algorithms.AES(srtp_key[:16]), modes.CTR(srtp_key[16:]))
        decryptor = cipher.decryptor()
        
        return decryptor.update(encrypted_packet) + decryptor.finalize()
```

#### RTP Security Measures
- **SRTP Encryption**: All RTP streams encrypted with AES-128
- **Key Management**: Per-session SRTP key generation and rotation
- **Authentication**: HMAC-SHA1 authentication for packet integrity
- **Port Security**: Dynamic port allocation with firewall rules
- **Replay Protection**: Sequence number validation to prevent replay attacks

### Authentication & Authorization

#### Multi-Factor Authentication
```python
import pyotp
from django.contrib.auth import authenticate

class MFAAuthenticationBackend:
    def authenticate(self, request, username=None, password=None, totp_token=None):
        user = authenticate(username=username, password=password)
        if not user:
            return None
        
        # Verify TOTP token
        totp = pyotp.TOTP(user.profile.totp_secret)
        if not totp.verify(totp_token, valid_window=1):
            # Log failed MFA attempt
            self.log_security_event(user, 'MFA_FAILED', request.META.get('REMOTE_ADDR'))
            return None
        
        return user
```

#### JWT Token Security
```python
from django_rest_framework_simplejwt.tokens import RefreshToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa, padding

class SecureJWTManager:
    def __init__(self):
        self.private_key = self.load_private_key()
        self.public_key = self.load_public_key()
    
    def create_tenant_scoped_token(self, user, tenant):
        """Create JWT with tenant isolation"""
        refresh = RefreshToken.for_user(user)
        
        # Add tenant-specific claims
        refresh['tenant_id'] = tenant.id
        refresh['tenant_permissions'] = list(user.get_tenant_permissions(tenant))
        refresh['session_id'] = self.generate_session_id()
        
        # Sign with RSA private key
        return str(refresh.access_token)
    
    def validate_tenant_access(self, token, required_tenant_id):
        """Validate token has access to specific tenant"""
        try:
            payload = jwt.decode(token, self.public_key, algorithms=['RS256'])
            token_tenant_id = payload.get('tenant_id')
            
            if str(token_tenant_id) != str(required_tenant_id):
                raise PermissionDenied("Token not valid for this tenant")
            
            return payload
        except jwt.InvalidTokenError:
            raise AuthenticationFailed("Invalid token")
```

### Infrastructure Security

#### Network Segmentation
```yaml
# Docker network security configuration
version: '3.8'
services:
  django-app:
    networks:
      - app-network
      - redis-network
    # No direct access to asterisk-network
  
  rtp-server:
    networks:
      - app-network
      - rtp-network
    ports:
      - "20000-30000:20000-30000/udp"  # RTP port range
  
  asterisk:
    networks:
      - asterisk-network
    # Isolated from direct external access

networks:
  app-network:
    driver: bridge
    internal: false
  asterisk-network:
    driver: bridge
    internal: true  # No external access
  rtp-network:
    driver: bridge
    internal: false
  redis-network:
    driver: bridge
    internal: true
```

#### Security Monitoring & Logging
```python
import structlog
from django.core.management.base import BaseCommand

class SecurityAuditLogger:
    def __init__(self):
        self.logger = structlog.get_logger("security_audit")
    
    async def log_security_event(self, event_type, details, user_id=None, tenant_id=None):
        """Comprehensive security event logging"""
        event_data = {
            'event_type': event_type,
            'timestamp': datetime.utcnow().isoformat(),
            'user_id': user_id,
            'tenant_id': tenant_id,
            'details': details,
            'source_ip': self.get_source_ip(),
            'session_id': self.get_session_id()
        }
        
        # Log to multiple destinations
        self.logger.info("security_event", **event_data)
        
        # Send to SIEM if critical
        if event_type in ['UNAUTHORIZED_ACCESS', 'MFA_FAILED', 'DATA_BREACH']:
            await self.send_to_siem(event_data)
    
    async def monitor_suspicious_activity(self):
        """Real-time suspicious activity detection"""
        # Implementation for anomaly detection
        pass
```

### Compliance & Regulatory Security

#### Data Protection
- **Encryption at Rest**: Database and file system encryption (AES-256)
- **Data Minimization**: Store only necessary data with automatic purging
- **Data Anonymization**: PII anonymization for analytics and testing
- **Backup Security**: Encrypted backups with secure key management
- **Data Retention**: Automated data lifecycle management per compliance requirements

#### Audit & Compliance
- **Audit Trails**: Comprehensive logging of all system access and changes
- **Compliance Reporting**: Automated generation of compliance reports
- **Access Reviews**: Periodic access rights review and certification
- **Vulnerability Management**: Regular security scanning and patch management
- **Incident Response**: Documented incident response procedures and testing

## Testing Strategy

### Unit Testing

#### Core Component Testing
```python
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock
from django.test import TestCase, TransactionTestCase

class SessionManagerTest(TransactionTestCase):
    def setUp(self):
        self.redis_mock = AsyncMock()
        self.session_manager = SessionManager(self.redis_mock)
        
    @pytest.mark.asyncio
    async def test_session_creation(self):
        """Test session creation with AMI event data"""
        session_data = {
            'session_id': 'test-123',
            'caller_id': '1234567890',
            'dialed_number': '0987654321',
            'tenant_id': 'tenant-1'
        }
        
        result = await self.session_manager.create_session(**session_data)
        
        # Verify session data structure
        assert result['id'] == 'test-123'
        assert result['caller_id'] == '1234567890'
        assert result['status'] == 'created'
        assert result['rtp_port'] is None
        
        # Verify Redis storage call
        self.redis_mock.hset.assert_called_once()
        
    @pytest.mark.asyncio
    async def test_rtp_attachment(self):
        """Test RTP server attachment to existing session"""
        session_id = 'test-123'
        rtp_port = 20001
        bridge_id = 'bridge-456'
        
        # Mock existing session
        self.redis_mock.hget.return_value = {
            'id': session_id,
            'status': 'created',
            'tenant_id': 'tenant-1'
        }
        
        await self.session_manager.attach_rtp(session_id, rtp_port, bridge_id)
        
        # Verify RTP attachment update
        expected_update = {
            'rtp_port': rtp_port,
            'bridge_id': bridge_id,
            'status': 'active'
        }
        self.redis_mock.hset.assert_called_with(
            f"session:{session_id}", 
            mapping=expected_update
        )

class AMIEventHandlerTest(TestCase):
    def setUp(self):
        self.session_manager_mock = AsyncMock()
        self.ami_handler = AMIEventHandler(self.session_manager_mock)
        
    @pytest.mark.asyncio
    async def test_newchannel_event_handling(self):
        """Test AMI Newchannel event processing"""
        event = {
            'Event': 'Newchannel',
            'Uniqueid': 'test-channel-123',
            'CallerIDNum': '1234567890',
            'Exten': '100',
            'Context': 'tenant-1-internal'
        }
        
        await self.ami_handler.handle_newchannel(None, event)
        
        # Verify session creation was triggered
        self.session_manager_mock.create_session.assert_called_once_with(
            session_id='test-channel-123',
            caller_id='1234567890',
            dialed_number='100',
            tenant_id='tenant-1'
        )
        
    def test_tenant_extraction_from_context(self):
        """Test tenant ID extraction from Asterisk context"""
        context = 'tenant-5-internal'
        tenant_id = self.ami_handler.extract_tenant_id({'Context': context})
        assert tenant_id == 'tenant-5'

class CustomRTPServerTest(TransactionTestCase):
    def setUp(self):
        self.session_manager_mock = AsyncMock()
        self.rtp_server = CustomRTPServer(self.session_manager_mock)
        
    @pytest.mark.asyncio
    async def test_rtp_packet_processing(self):
        """Test RTP packet parsing and processing"""
        # Mock session data
        session_data = {
            'id': 'test-session',
            'tenant_id': 'tenant-1',
            'caller_id': '1234567890'
        }
        
        # Sample RTP packet data (simplified)
        rtp_header = b'\x80\x08\x00\x01\x00\x00\x00\x02\x00\x00\x00\x03'
        audio_payload = b'audio_data_sample'
        
        await self.rtp_server.process_audio_packet(
            session_data, audio_payload, 12345, 1
        )
        
        # Verify session manager was called with processed audio
        self.session_manager_mock.process_audio.assert_called_once()
        call_args = self.session_manager_mock.process_audio.call_args[0][0]
        
        assert call_args['session_id'] == 'test-session'
        assert call_args['tenant_id'] == 'tenant-1'
        assert call_args['audio_data'] == audio_payload
        assert call_args['timestamp'] == 12345
        assert call_args['sequence'] == 1
```

#### ARI Integration Testing
```python
class ARICallControllerTest(TestCase):
    def setUp(self):
        self.ari_mock = MagicMock()
        self.rtp_server_mock = MagicMock()
        self.controller = ARICallController(self.ari_mock, self.rtp_server_mock)
        
    @pytest.mark.asyncio
    async def test_external_media_bridging(self):
        """Test ExternalMedia channel creation and bridging"""
        # Mock channel and RTP server
        channel_mock = MagicMock()
        channel_mock.id = 'channel-123'
        
        self.rtp_server_mock.host = '192.168.1.100'
        self.rtp_server_mock.get_port.return_value = 20001
        
        # Mock ARI responses
        external_media_mock = MagicMock()
        external_media_mock.id = 'external-456'
        self.ari_mock.channels.externalMedia.return_value = external_media_mock
        
        bridge_mock = MagicMock()
        bridge_mock.id = 'bridge-789'
        self.ari_mock.bridges.create.return_value = bridge_mock
        
        # Execute bridging
        bridge, external_media = await self.controller.handle_incoming_call(
            channel_mock, 'session-123'
        )
        
        # Verify call flow
        channel_mock.answer.assert_called_once()
        self.ari_mock.channels.externalMedia.assert_called_once_with(
            app='media_gateway',
            external_host='192.168.1.100:20001',
            format='ulaw'
        )
        bridge_mock.addChannel.assert_called_once_with(
            channel=['channel-123', 'external-456']
        )
```

### Integration Testing

#### End-to-End Call Flow Testing
```python
class CallFlowIntegrationTest(TransactionTestCase):
    def setUp(self):
        """Set up test environment with real components"""
        self.redis_client = redis.Redis(host='localhost', port=6379, db=1)
        self.session_manager = SessionManager(self.redis_client)
        self.rtp_server = CustomRTPServer(self.session_manager, base_port=30000)
        
    @pytest.mark.asyncio
    async def test_complete_call_flow(self):
        """Test complete call flow from AMI event to RTP processing"""
        # Simulate AMI Newchannel event
        ami_event = {
            'Event': 'Newchannel',
            'Uniqueid': 'integration-test-123',
            'CallerIDNum': '5551234567',
            'Exten': '200',
            'Context': 'tenant-test-internal'
        }
        
        ami_handler = AMIEventHandler(self.session_manager)
        
        # Step 1: Process AMI event - should create session
        await ami_handler.handle_newchannel(None, ami_event)
        
        # Verify session was created
        session = await self.session_manager.get_session('integration-test-123')
        assert session['status'] == 'created'
        assert session['caller_id'] == '5551234567'
        
        # Step 2: Simulate ARI call control
        rtp_port = await self.rtp_server.create_session_endpoint(
            'integration-test-123', 'tenant-test'
        )
        
        await self.session_manager.attach_rtp(
            'integration-test-123', rtp_port, 'bridge-test'
        )
        
        # Verify RTP attachment
        updated_session = await self.session_manager.get_session('integration-test-123')
        assert updated_session['status'] == 'active'
        assert updated_session['rtp_port'] == rtp_port
        
        # Step 3: Simulate RTP packet processing
        test_audio = b'test_audio_data'
        await self.rtp_server.process_audio_packet(
            updated_session, test_audio, 54321, 100
        )
        
        # Verify audio processing
        # (Implementation depends on your audio processing pipeline)

class MultiTenantIsolationTest(TransactionTestCase):
    """Test tenant isolation across all components"""
    
    def setUp(self):
        self.redis_client = redis.Redis(host='localhost', port=6379, db=1)
        self.session_manager = SessionManager(self.redis_client)
        
    @pytest.mark.asyncio
    async def test_tenant_session_isolation(self):
        """Verify sessions are isolated by tenant"""
        # Create sessions for different tenants
        session_1 = await self.session_manager.create_session(
            'session-1', '1111111111', '100', 'tenant-1'
        )
        session_2 = await self.session_manager.create_session(
            'session-2', '2222222222', '200', 'tenant-2'
        )
        
        # Verify tenant-1 cannot access tenant-2's sessions
        tenant_1_sessions = await self.session_manager.get_tenant_sessions('tenant-1')
        tenant_2_sessions = await self.session_manager.get_tenant_sessions('tenant-2')
        
        assert len(tenant_1_sessions) == 1
        assert len(tenant_2_sessions) == 1
        assert session_1['id'] in [s['id'] for s in tenant_1_sessions]
        assert session_2['id'] not in [s['id'] for s in tenant_1_sessions]
```

### RTP Packet Injection Testing

#### Simulated RTP Testing
```python
import socket
import struct
import time

class RTPPacketInjector:
    """Tool for injecting test RTP packets"""
    
    def __init__(self, target_host='localhost', target_port=20000):
        self.target_host = target_host
        self.target_port = target_port
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sequence_number = 0
        self.timestamp = int(time.time() * 8000)  # 8kHz sample rate
        self.ssrc = 12345
        
    def create_rtp_packet(self, payload):
        """Create RTP packet with proper header"""
        version = 2
        padding = 0
        extension = 0
        cc = 0
        marker = 0
        payload_type = 8  # PCMA/ulaw
        
        # RTP header (12 bytes)
        header = struct.pack('!BBHII',
            (version << 6) | (padding << 5) | (extension << 4) | cc,
            (marker << 7) | payload_type,
            self.sequence_number,
            self.timestamp,
            self.ssrc
        )
        
        self.sequence_number += 1
        self.timestamp += 160  # 20ms at 8kHz
        
        return header + payload
        
    async def inject_audio_stream(self, duration_seconds=10, packet_interval=0.02):
        """Inject continuous audio stream"""
        packets_sent = 0
        start_time = time.time()
        
        while time.time() - start_time < duration_seconds:
            # Generate test audio payload (silence/noise)
            audio_payload = b'\x00' * 160  # 20ms of silence in ulaw
            
            rtp_packet = self.create_rtp_packet(audio_payload)
            self.socket.sendto(rtp_packet, (self.target_host, self.target_port))
            
            packets_sent += 1
            await asyncio.sleep(packet_interval)
            
        return packets_sent

class RTPQualityTest(TestCase):
    """Test RTP packet quality and statistics"""
    
    @pytest.mark.asyncio
    async def test_rtp_packet_loss_detection(self):
        """Test detection of lost RTP packets"""
        rtp_server = CustomRTPServer(AsyncMock())
        injector = RTPPacketInjector()
        
        # Start RTP server
        server_task = asyncio.create_task(
            rtp_server.start_server('test-session-123')
        )
        
        # Inject packets with intentional gaps
        packets = [1, 2, 3, 5, 6, 8, 9, 10]  # Missing 4 and 7
        
        for seq_num in packets:
            payload = f"packet_{seq_num}".encode()
            packet = injector.create_rtp_packet(payload)
            # Manually set sequence number for testing
            packet = packet[:2] + struct.pack('!H', seq_num) + packet[4:]
            await injector.send_packet(packet)
            
        # Verify packet loss detection
        stats = await rtp_server.get_session_stats('test-session-123')
        assert stats['packets_lost'] == 2
        assert stats['loss_percentage'] == 20.0
```

### Load Testing & Performance

#### Concurrent Session Testing
```python
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

class LoadTestRunner:
    def __init__(self, session_manager, rtp_server):
        self.session_manager = session_manager
        self.rtp_server = rtp_server
        self.results = []
        
    async def simulate_concurrent_calls(self, num_calls=100):
        """Simulate multiple concurrent calls"""
        tasks = []
        start_time = time.time()
        
        for i in range(num_calls):
            task = asyncio.create_task(
                self.simulate_single_call(f'load-test-{i}')
            )
            tasks.append(task)
            
        results = await asyncio.gather(*tasks, return_exceptions=True)
        end_time = time.time()
        
        success_count = len([r for r in results if not isinstance(r, Exception)])
        failure_count = len([r for r in results if isinstance(r, Exception)])
        
        return {
            'total_calls': num_calls,
            'successful_calls': success_count,
            'failed_calls': failure_count,
            'duration': end_time - start_time,
            'calls_per_second': num_calls / (end_time - start_time)
        }
        
    async def simulate_single_call(self, session_id):
        """Simulate complete call lifecycle"""
        try:
            # Create session
            session = await self.session_manager.create_session(
                session_id, '1234567890', '100', 'load-tenant'
            )
            
            # Attach RTP
            rtp_port = await self.rtp_server.create_session_endpoint(
                session_id, 'load-tenant'
            )
            
            await self.session_manager.attach_rtp(session_id, rtp_port, 'bridge')
            
            # Simulate call duration
            await asyncio.sleep(0.1)  # 100ms call simulation
            
            # Clean up
            await self.session_manager.end_session(session_id)
            
            return {'status': 'success', 'session_id': session_id}
            
        except Exception as e:
            return {'status': 'error', 'session_id': session_id, 'error': str(e)}

class PerformanceTest(TestCase):
    @pytest.mark.asyncio
    async def test_high_call_volume(self):
        """Test system under high call volume"""
        load_tester = LoadTestRunner(
            SessionManager(redis.Redis()),
            CustomRTPServer(AsyncMock())
        )
        
        # Test with 1000 concurrent calls
        results = await load_tester.simulate_concurrent_calls(1000)
        
        # Performance assertions
        assert results['calls_per_second'] > 50  # Minimum performance requirement
        assert results['failed_calls'] < results['total_calls'] * 0.01  # <1% failure rate
```

### Test Automation & CI/CD

#### GitHub Actions Test Workflow
```yaml
name: Comprehensive Test Suite

on:
  push:
    branches: [ main, develop ]
  pull_request:
    branches: [ main ]

jobs:
  unit-tests:
    runs-on: ubuntu-latest
    services:
      redis:
        image: redis:6
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
      postgres:
        image: postgres:13
        env:
          POSTGRES_PASSWORD: test_password
          POSTGRES_DB: test_db
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5

    steps:
    - uses: actions/checkout@v3
    
    - name: Set up Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.11'
        
    - name: Install dependencies
      run: |
        pip install -r requirements.txt
        pip install pytest pytest-asyncio pytest-cov
        
    - name: Run unit tests
      run: |
        pytest tests/unit/ --cov=./ --cov-report=xml
        
    - name: Upload coverage
      uses: codecov/codecov-action@v3

  integration-tests:
    runs-on: ubuntu-latest
    needs: unit-tests
    
    steps:
    - uses: actions/checkout@v3
    
    - name: Start Asterisk container
      run: |
        docker run -d --name asterisk \
          -p 5038:5038 -p 8088:8088 \
          andrius/asterisk:latest
          
    - name: Run integration tests
      run: |
        pytest tests/integration/ --asterisk-host=localhost
        
  load-tests:
    runs-on: ubuntu-latest
    needs: integration-tests
    
    steps:
    - uses: actions/checkout@v3
    
    - name: Run load tests
      run: |
        pytest tests/load/ --max-calls=500
        
  security-tests:
    runs-on: ubuntu-latest
    needs: unit-tests
    
    steps:
    - uses: actions/checkout@v3
    
    - name: Run security scans
      run: |
        pip install safety bandit
        safety check
        bandit -r . -f json -o bandit-report.json
```

### Test Coverage Requirements

#### Minimum Coverage Targets
- **Unit Tests**: 90% code coverage
- **Integration Tests**: All major user flows
- **Security Tests**: All authentication/authorization paths
- **Performance Tests**: Load scenarios up to 1000 concurrent calls
- **RTP Tests**: Packet loss, jitter, and quality metrics

#### Critical Test Scenarios
1. **AMI Event Processing**: All event types with edge cases
2. **Session Management**: Creation, attachment, cleanup, and error handling
3. **RTP Processing**: Packet parsing, quality metrics, and error recovery
4. **Multi-Tenant Isolation**: Data segregation and access control
5. **Security**: Authentication, authorization, and encryption
6. **Performance**: High load, memory usage, and response times

## Deployment

### Recommended Production Stack

#### Application Server Layer
```yaml
# docker-compose.production.yml
version: '3.8'

services:
  # Django Channels Application with Daphne
  django-app:
    build:
      context: .
      dockerfile: Dockerfile.production
    command: ["daphne", "-b", "0.0.0.0", "-p", "8000", "aiMediaGateway.asgi:application"]
    environment:
      - DJANGO_SETTINGS_MODULE=aiMediaGateway.settings.production
      - DATABASE_URL=postgresql://user:password@postgres:5432/aimediagateway
      - REDIS_URL=redis://redis:6379/0
      - CHANNEL_LAYER_HOST=redis://redis:6379/1
    volumes:
      - ./logs:/app/logs
      - ./media:/app/media
    depends_on:
      - postgres
      - redis
    networks:
      - app-network
      - db-network
    deploy:
      replicas: 3
      resources:
        limits:
          cpus: '1.0'
          memory: 1G
        reservations:
          cpus: '0.5'
          memory: 512M

  # Alternative: Uvicorn deployment
  django-uvicorn:
    build:
      context: .
      dockerfile: Dockerfile.production
    command: [
      "uvicorn", 
      "aiMediaGateway.asgi:application",
      "--host", "0.0.0.0",
      "--port", "8000",
      "--workers", "4",
      "--loop", "asyncio"
    ]
    environment:
      - DJANGO_SETTINGS_MODULE=aiMediaGateway.settings.production
    # ... same as django-app configuration

  # Custom RTP Server (separate service)
  rtp-server:
    build:
      context: .
      dockerfile: Dockerfile.rtp
    command: ["python", "manage.py", "run_rtp_server"]
    environment:
      - RTP_BASE_PORT=20000
      - RTP_PORT_RANGE=10000
      - SESSION_MANAGER_REDIS_URL=redis://redis:6379/2
    ports:
      - "20000-30000:20000-30000/udp"
    networks:
      - app-network
      - rtp-network
    deploy:
      replicas: 2
      resources:
        limits:
          cpus: '2.0'
          memory: 2G

  # Background Task Workers (Celery)
  celery-worker:
    build:
      context: .
      dockerfile: Dockerfile.production
    command: ["celery", "-A", "aiMediaGateway", "worker", "-l", "info"]
    environment:
      - DJANGO_SETTINGS_MODULE=aiMediaGateway.settings.production
      - CELERY_BROKER_URL=redis://redis:6379/3
    depends_on:
      - redis
      - postgres
    networks:
      - app-network
      - db-network
    deploy:
      replicas: 2

  # Load Balancer (Nginx)
  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/nginx.conf
      - ./nginx/ssl:/etc/nginx/ssl
      - ./static:/var/www/static
    depends_on:
      - django-app
    networks:
      - app-network
    deploy:
      placement:
        constraints: [node.role == manager]

networks:
  app-network:
    driver: overlay
  db-network:
    driver: overlay
    internal: true
  rtp-network:
    driver: overlay

volumes:
  postgres_data:
  redis_data:
```

#### Database Layer (PostgreSQL)
```yaml
  # Primary PostgreSQL Database
  postgres:
    image: postgres:15-alpine
    environment:
      POSTGRES_DB: aimediagateway
      POSTGRES_USER: pbx_user
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_INITDB_ARGS: "--auth-host=scram-sha-256"
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./postgresql/postgresql.conf:/etc/postgresql/postgresql.conf
      - ./postgresql/init.sql:/docker-entrypoint-initdb.d/init.sql
    command: ["postgres", "-c", "config_file=/etc/postgresql/postgresql.conf"]
    networks:
      - db-network
    deploy:
      replicas: 1
      resources:
        limits:
          cpus: '2.0'
          memory: 4G
      placement:
        constraints: [node.labels.database == true]

  # PostgreSQL Read Replica (for scaling reads)
  postgres-replica:
    image: postgres:15-alpine
    environment:
      POSTGRES_DB: aimediagateway
      POSTGRES_USER: pbx_user
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      PGUSER: postgres
    volumes:
      - postgres_replica_data:/var/lib/postgresql/data
    command: |
      bash -c "
      until pg_basebackup -h postgres -D /var/lib/postgresql/data -U postgres -v -P; do
        echo 'Waiting for primary to connect...'
        sleep 1s
      done
      echo 'standby_mode = on' >> /var/lib/postgresql/data/recovery.conf
      echo 'primary_conninfo = host=postgres port=5432 user=postgres' >> /var/lib/postgresql/data/recovery.conf
      postgres
      "
    depends_on:
      - postgres
    networks:
      - db-network
```

#### Caching & Message Layer (Redis)
```yaml
  # Redis for Channel Layers and Caching
  redis:
    image: redis:7-alpine
    command: redis-server --appendonly yes --requirepass ${REDIS_PASSWORD}
    volumes:
      - redis_data:/data
      - ./redis/redis.conf:/usr/local/etc/redis/redis.conf
    networks:
      - app-network
      - db-network
    deploy:
      replicas: 1
      resources:
        limits:
          memory: 2G
      placement:
        constraints: [node.labels.cache == true]

  # Redis Cluster (for high availability)
  redis-cluster:
    image: redis:7-alpine
    command: redis-server /usr/local/etc/redis/redis.conf --cluster-enabled yes
    volumes:
      - ./redis/cluster.conf:/usr/local/etc/redis/redis.conf
    networks:
      - app-network
    deploy:
      replicas: 6  # 3 masters + 3 replicas
      endpoint_mode: dnsrr
```

### Load Balancing Configuration

#### Nginx Load Balancer
```nginx
# nginx/nginx.conf
upstream django_app {
    least_conn;  # Load balancing method
    server django-app:8000 max_fails=3 fail_timeout=30s;
    server django-app:8001 max_fails=3 fail_timeout=30s;
    server django-app:8002 max_fails=3 fail_timeout=30s;
    
    # Health check
    keepalive 32;
}

upstream websocket_app {
    ip_hash;  # Session affinity for WebSockets
    server django-app:8000;
    server django-app:8001;
    server django-app:8002;
}

server {
    listen 80;
    server_name aimediagateway.com;
    
    # Redirect HTTP to HTTPS
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name aimediagateway.com;
    
    # SSL Configuration
    ssl_certificate /etc/nginx/ssl/certificate.crt;
    ssl_certificate_key /etc/nginx/ssl/private.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-RSA-AES256-GCM-SHA512:DHE-RSA-AES256-GCM-SHA512;
    ssl_prefer_server_ciphers off;
    
    # Security Headers
    add_header X-Frame-Options DENY;
    add_header X-Content-Type-Options nosniff;
    add_header X-XSS-Protection "1; mode=block";
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    
    # Rate Limiting
    limit_req_zone $binary_remote_addr zone=api:10m rate=100r/s;
    limit_req_zone $binary_remote_addr zone=websocket:10m rate=10r/s;
    
    # Static Files
    location /static/ {
        alias /var/www/static/;
        expires 1y;
        add_header Cache-Control "public, immutable";
    }
    
    # WebSocket connections with session affinity
    location /ws/ {
        limit_req zone=websocket burst=20 nodelay;
        
        proxy_pass http://websocket_app;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # WebSocket timeouts
        proxy_read_timeout 86400;
        proxy_send_timeout 86400;
    }
    
    # API endpoints
    location /api/ {
        limit_req zone=api burst=200 nodelay;
        
        proxy_pass http://django_app;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # API timeouts
        proxy_read_timeout 60;
        proxy_send_timeout 60;
    }
    
    # Admin interface
    location /admin/ {
        # Restricted access
        allow 10.0.0.0/8;
        allow 192.168.0.0/16;
        deny all;
        
        proxy_pass http://django_app;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    
    # Health check endpoint
    location /health/ {
        access_log off;
        proxy_pass http://django_app;
    }
}
```

### Multi-Tenant & High Volume Scenarios

#### Database Sharding Strategy
```python
# settings/production.py
DATABASE_ROUTERS = ['aiMediaGateway.routers.TenantDatabaseRouter']

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'aimediagateway_default',
        'USER': 'pbx_user',
        'PASSWORD': os.environ.get('POSTGRES_PASSWORD'),
        'HOST': 'postgres',
        'PORT': '5432',
        'OPTIONS': {
            'MAX_CONNS': 20,
            'CONN_MAX_AGE': 600,
        }
    },
    'tenant_shard_1': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'aimediagateway_shard1',
        'USER': 'pbx_user',
        'PASSWORD': os.environ.get('POSTGRES_PASSWORD'),
        'HOST': 'postgres-shard1',
        'PORT': '5432',
    },
    'tenant_shard_2': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'aimediagateway_shard2',
        'USER': 'pbx_user',
        'PASSWORD': os.environ.get('POSTGRES_PASSWORD'),
        'HOST': 'postgres-shard2',
        'PORT': '5432',
    },
    # Additional shards as needed
}

# Redis Cluster Configuration
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [
                ("redis-node1", 6379),
                ("redis-node2", 6379),
                ("redis-node3", 6379),
                ("redis-node4", 6379),
                ("redis-node5", 6379),
                ("redis-node6", 6379),
            ],
            "symmetric_encryption_keys": [os.environ.get('CHANNEL_ENCRYPTION_KEY')],
        },
    },
}
```

#### Auto-Scaling Configuration (Kubernetes)
```yaml
# kubernetes/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: django-app
spec:
  replicas: 3
  selector:
    matchLabels:
      app: django-app
  template:
    metadata:
      labels:
        app: django-app
    spec:
      containers:
      - name: django
        image: aimediagateway:latest
        ports:
        - containerPort: 8000
        env:
        - name: DJANGO_SETTINGS_MODULE
          value: "aiMediaGateway.settings.production"
        resources:
          requests:
            cpu: 500m
            memory: 512Mi
          limits:
            cpu: 1000m
            memory: 1Gi
        livenessProbe:
          httpGet:
            path: /health/
            port: 8000
          initialDelaySeconds: 30
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /ready/
            port: 8000
          initialDelaySeconds: 5
          periodSeconds: 5

---
# Horizontal Pod Autoscaler
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: django-app-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: django-app
  minReplicas: 3
  maxReplicas: 10
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
  - type: Resource
    resource:
      name: memory
      target:
        type: Utilization
        averageUtilization: 80
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 300
      policies:
      - type: Percent
        value: 10
        periodSeconds: 60
    scaleUp:
      stabilizationWindowSeconds: 0
      policies:
      - type: Percent
        value: 100
        periodSeconds: 15
      - type: Pods
        value: 4
        periodSeconds: 15
      selectPolicy: Max
```

### CI/CD Pipeline

#### Complete GitLab CI/CD Pipeline
```yaml
# .gitlab-ci.yml
stages:
  - test
  - build
  - deploy-staging
  - deploy-production

variables:
  DOCKER_DRIVER: overlay2
  DOCKER_TLS_CERTDIR: "/certs"

# Test Stage
unit-tests:
  stage: test
  image: python:3.11
  services:
    - redis:7-alpine
    - postgres:15-alpine
  variables:
    POSTGRES_DB: test_db
    POSTGRES_USER: test_user
    POSTGRES_PASSWORD: test_pass
    REDIS_URL: redis://redis:6379/0
  before_script:
    - pip install -r requirements.txt
    - pip install pytest pytest-asyncio pytest-cov
  script:
    - python manage.py test
    - pytest tests/ --cov=./ --cov-report=xml --cov-report=term
  coverage: '/TOTAL.+?(\d+\%)$/'
  artifacts:
    reports:
      coverage_report:
        coverage_format: cobertura
        path: coverage.xml
    expire_in: 1 week

security-scan:
  stage: test
  image: python:3.11
  script:
    - pip install safety bandit
    - safety check --json --output safety-report.json || true
    - bandit -r . -f json -o bandit-report.json || true
  artifacts:
    reports:
      security: [safety-report.json, bandit-report.json]
    expire_in: 1 week

# Build Stage
build-app:
  stage: build
  image: docker:latest
  services:
    - docker:dind
  before_script:
    - docker login -u $CI_REGISTRY_USER -p $CI_REGISTRY_PASSWORD $CI_REGISTRY
  script:
    - docker build -t $CI_REGISTRY_IMAGE/app:$CI_COMMIT_SHA .
    - docker build -t $CI_REGISTRY_IMAGE/app:latest .
    - docker push $CI_REGISTRY_IMAGE/app:$CI_COMMIT_SHA
    - docker push $CI_REGISTRY_IMAGE/app:latest
  only:
    - main
    - develop

build-rtp-server:
  stage: build
  image: docker:latest
  services:
    - docker:dind
  script:
    - docker build -f Dockerfile.rtp -t $CI_REGISTRY_IMAGE/rtp-server:$CI_COMMIT_SHA .
    - docker build -f Dockerfile.rtp -t $CI_REGISTRY_IMAGE/rtp-server:latest .
    - docker push $CI_REGISTRY_IMAGE/rtp-server:$CI_COMMIT_SHA
    - docker push $CI_REGISTRY_IMAGE/rtp-server:latest
  only:
    - main
    - develop

# Staging Deployment
deploy-staging:
  stage: deploy-staging
  image: bitnami/kubectl:latest
  environment:
    name: staging
    url: https://staging.aimediagateway.com
  before_script:
    - kubectl config use-context staging
  script:
    - kubectl set image deployment/django-app django=$CI_REGISTRY_IMAGE/app:$CI_COMMIT_SHA
    - kubectl set image deployment/rtp-server rtp-server=$CI_REGISTRY_IMAGE/rtp-server:$CI_COMMIT_SHA
    - kubectl rollout status deployment/django-app
    - kubectl rollout status deployment/rtp-server
  only:
    - develop

integration-tests-staging:
  stage: deploy-staging
  image: python:3.11
  needs: ["deploy-staging"]
  script:
    - pip install requests pytest
    - pytest tests/integration/ --base-url=https://staging.aimediagateway.com
  only:
    - develop

# Production Deployment
deploy-production:
  stage: deploy-production
  image: bitnami/kubectl:latest
  environment:
    name: production
    url: https://aimediagateway.com
  before_script:
    - kubectl config use-context production
  script:
    # Blue-Green Deployment
    - |
      if kubectl get deployment django-app-green; then
        DEPLOYMENT_COLOR="blue"
        INACTIVE_COLOR="green"
      else
        DEPLOYMENT_COLOR="green"
        INACTIVE_COLOR="blue"
      fi
    
    - kubectl apply -f k8s/deployment-${DEPLOYMENT_COLOR}.yaml
    - kubectl set image deployment/django-app-${DEPLOYMENT_COLOR} django=$CI_REGISTRY_IMAGE/app:$CI_COMMIT_SHA
    - kubectl rollout status deployment/django-app-${DEPLOYMENT_COLOR}
    
    # Health check new deployment
    - kubectl run --rm -i --tty health-check --image=curlimages/curl --restart=Never -- curl -f http://django-app-${DEPLOYMENT_COLOR}:8000/health/
    
    # Switch traffic to new deployment
    - kubectl patch service django-app -p '{"spec":{"selector":{"version":"'${DEPLOYMENT_COLOR}'"}}}'
    
    # Clean up old deployment
    - kubectl delete deployment django-app-${INACTIVE_COLOR} --ignore-not-found=true
  when: manual
  only:
    - main

# Performance Testing
load-test-production:
  stage: deploy-production
  image: python:3.11
  needs: ["deploy-production"]
  script:
    - pip install locust
    - locust -f tests/load/locustfile.py --host=https://aimediagateway.com --users=100 --spawn-rate=10 --run-time=5m --html=load-test-report.html
  artifacts:
    reports:
      load_performance: load-test-report.html
    expire_in: 1 week
  when: manual
  only:
    - main
```

### Production Monitoring & Observability

#### Monitoring Stack
```yaml
# monitoring/docker-compose.monitoring.yml
version: '3.8'

services:
  # Prometheus for metrics collection
  prometheus:
    image: prom/prometheus:latest
    ports:
      - "9090:9090"
    volumes:
      - ./prometheus/prometheus.yml:/etc/prometheus/prometheus.yml
      - prometheus_data:/prometheus
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
      - '--storage.tsdb.path=/prometheus'
      - '--web.console.libraries=/etc/prometheus/console_libraries'
      - '--web.console.templates=/etc/prometheus/consoles'
      - '--web.enable-lifecycle'
      - '--storage.tsdb.retention.time=30d'

  # Grafana for visualization
  grafana:
    image: grafana/grafana:latest
    ports:
      - "3000:3000"
    volumes:
      - grafana_data:/var/lib/grafana
      - ./grafana/dashboards:/etc/grafana/provisioning/dashboards
      - ./grafana/datasources:/etc/grafana/provisioning/datasources
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_PASSWORD}

  # AlertManager for alerts
  alertmanager:
    image: prom/alertmanager:latest
    ports:
      - "9093:9093"
    volumes:
      - ./alertmanager/alertmanager.yml:/etc/alertmanager/alertmanager.yml

volumes:
  prometheus_data:
  grafana_data:
```

### Deployment Checklist

#### Pre-Deployment
- [ ] Environment variables configured and encrypted
- [ ] SSL certificates installed and configured
- [ ] Database migrations tested in staging
- [ ] Redis cluster configured and tested
- [ ] Load balancer configuration verified
- [ ] Monitoring and alerting configured
- [ ] Backup and recovery procedures tested

#### Post-Deployment
- [ ] Health checks passing
- [ ] WebSocket connections working
- [ ] RTP server accepting connections
- [ ] Multi-tenant isolation verified
- [ ] Performance metrics within acceptable range
- [ ] Security scans passing
- [ ] Disaster recovery procedures verified

---

*This development plan provides a comprehensive roadmap for building a robust, scalable, multi-tenant Asterisk PBX management system with real-time audio streaming capabilities. The plan emphasizes security, performance, and operational excellence while maintaining clear development milestones and deliverables.*

**Document Version**: 2.0  
**Creation Date**: September 19, 2025  
**Total Estimated Duration**: 12 weeks  
**Recommended Team Size**: 3-4 developers
