# aiMediaGateway

A multi-tenant Asterisk PBX management system with real-time audio streaming capabilities. The system implements a session-first architecture using Django Channels (ASGI), Panoramisk (AMI), python-ari (ARI), and custom RTP server for zero-loss audio capture and real-time call management.

## Overview

aiMediaGateway leverages modern Python async frameworks to provide early call detection, programmatic call control, and real-time audio capture through a custom RTP server. The architecture implements a session-first approach where AMI events trigger session creation before RTP streaming begins, ensuring zero audio loss and seamless multi-tenant operation.

### Key Features

- **Multi-tenant Architecture**: Complete tenant isolation at database and session level
- **Real-time Call Management**: WebSocket-based dashboard with live call monitoring
- **Audio Processing**: Integration with NVIDIA RIVA for speech synthesis and transcription
- **Zero Audio Loss**: Session-first approach prevents packet loss during call establishment
- **Scalable Design**: Redis-backed session management and channel layers for horizontal scaling

### System Architecture

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

## Technology Stack

- **Backend Framework**: Django 5.2.6 with Django Channels 4.0.0
- **Database**: PostgreSQL (with SQLite for testing)
- **Cache & Sessions**: Redis 5.0.1 with django-redis
- **Asterisk Integration**: 
  - Panoramisk 1.4 (AMI events)
  - Custom ARI client (Asterisk REST Interface)
- **Audio Processing**: NVIDIA RIVA Client 2.19.0, SoundFile, NumPy
- **Async Support**: Native asyncio integration
- **Configuration**: python-decouple for environment variables
- **Security**: Cryptography 41.0.8
- **Testing**: pytest with Django integration

## Requirements

### System Requirements

- **Python 3.8+** (tested with Python 3.13)
- **PostgreSQL 12+** database server
- **Redis 6+** server for channel layers and caching
- **Asterisk PBX** with AMI and ARI enabled
- **NVIDIA RIVA Server** (optional, for audio transcription/synthesis)

### External Services

- **PostgreSQL**: Multi-tenant data storage
- **Redis**: Session management and WebSocket channel layers
- **Asterisk PBX**: Call control and media routing
- **NVIDIA RIVA**: Speech recognition and synthesis (optional)

## Installation & Setup

### 1. Clone Repository

```bash
git clone <repository-url>
cd aiMediaGatewayV2
```

### 2. Create Virtual Environment

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Linux/Mac
source venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Database Setup

#### PostgreSQL (Production)

```bash
# Create database and user
createdb aimediagateway_db
createuser aimediagateway_user

# Grant permissions in psql:
GRANT ALL PRIVILEGES ON DATABASE aimediagateway_db TO aimediagateway_user;
```

#### SQLite (Development)

For development without PostgreSQL, you can use the test settings which use SQLite.

### 5. Environment Configuration

Create `.env` file in project root:

```env
# Django Core Settings
SECRET_KEY=your-secret-key-here
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1

# Database Configuration
DATABASE_NAME=aimediagateway_db
DATABASE_USER=aimediagateway_user
DATABASE_PASSWORD=your-database-password
DATABASE_HOST=localhost
DATABASE_PORT=5432

# RIVA ASR Configuration (Optional)
RIVA_ASR_URI=localhost:50051
RIVA_USE_SSL=False
RIVA_SSL_CERT=
RIVA_SSL_KEY=
RIVA_SSL_ROOT_CERTS=
RIVA_AUTH_TOKEN=
RIVA_AUTH_METADATA=
RIVA_DEFAULT_LANGUAGE=en-US
RIVA_DEFAULT_SAMPLE_RATE=16000
RIVA_MAX_ALTERNATIVES=3
RIVA_ENABLE_PROFANITY_FILTER=True
RIVA_ENABLE_SPEAKER_DIARIZATION=True
RIVA_ENABLE_WORD_TIMESTAMPS=True
RIVA_ENABLE_AUTOMATIC_PUNCTUATION=True
RIVA_CONNECTION_TIMEOUT=30.0
RIVA_REQUEST_TIMEOUT=300.0
RIVA_STREAMING_CHUNK_SIZE=1024
RIVA_MAX_CONCURRENT_REQUESTS=10

# Audio Transcription Settings
TRANSCRIPTION_CACHE_TTL=3600
TRANSCRIPTION_MAX_AUDIO_SIZE=52428800
```

### 6. Database Migration

```bash
python manage.py makemigrations
python manage.py migrate
python manage.py createsuperuser
```

### 7. Verify Installation

```bash
python manage.py check
```

## Running the Application

### Development Server

```bash
# Basic Django development server
python manage.py runserver

# With test settings (SQLite, in-memory cache)
python manage.py runserver --settings=aiMediaGateway.test_settings
```

### Background Services

The application includes custom management commands for running AMI and ARI handlers:

#### AMI Event Handler

```bash
# Start AMI event listener for all tenants
python manage.py ami_handler

# Start for specific tenants
python manage.py ami_handler --tenant tenant1 tenant2
```

#### ARI Call Handler

```bash
# Start ARI call handler
python manage.py ari_handler

# With specific tenant ID
python manage.py ari_handler --tenantId your_tenant_id
```

### Production Deployment

For production deployment:

1. Use **Daphne** or **Uvicorn** for ASGI server
2. Configure reverse proxy (nginx) for WebSocket upgrades
3. Ensure Redis connectivity from all ASGI workers
4. Set up process management for web, WebSocket, and background tasks

```bash
# Example with Daphne
daphne -b 0.0.0.0 -p 8000 aiMediaGateway.asgi:application
```

## Testing

The project uses pytest with Django integration for testing.

### Run Tests

```bash
# Run all tests with test settings (SQLite)
python manage.py test --settings=aiMediaGateway.test_settings

# Run with pytest
pytest

# Run specific test files
pytest core/tests.py
pytest ari/tests/

# Run with coverage
pip install coverage
coverage run --source='.' manage.py test --settings=aiMediaGateway.test_settings
coverage report
coverage html
```

### Test Configuration

- **Test Settings**: `aiMediaGateway/test_settings.py` provides SQLite and in-memory backends
- **Test Files**: Located in `*/tests/` directories and `test_*.py` files
- **pytest.ini**: Configures Django settings and test discovery patterns

## Project Structure

```
aiMediaGatewayV2/
├── aiMediaGateway/          # Django project configuration
│   ├── __init__.py
│   ├── asgi.py             # ASGI configuration with WebSocket routing
│   ├── settings.py         # Main Django settings
│   ├── urls.py             # URL routing
│   └── wsgi.py             # WSGI configuration
├── ari/                    # ARI client application
│   ├── client.py           # Custom ARI client implementation
│   ├── config.py           # ARI configuration
│   ├── tests/              # ARI-specific tests
│   └── ...
├── core/                   # Core application logic
│   ├── ami/                # AMI (Asterisk Manager Interface) integration
│   │   ├── client.py       # AMI client implementation
│   │   ├── events.py       # AMI event handlers
│   │   └── ...
│   ├── ari/                # ARI integration
│   │   └── manager.py      # ARI manager
│   ├── audio/              # Audio processing modules
│   │   ├── audio_conversion.py
│   │   ├── audio_streaming.py
│   │   ├── audio_transcription.py
│   │   └── ...
│   ├── junie_codes/        # Custom implementations
│   │   ├── ami/            # AMI handlers and commands
│   │   ├── ari/            # ARI events and management
│   │   ├── rtp_server.py   # Custom RTP server
│   │   └── ...
│   ├── management/         # Django management commands
│   │   └── commands/
│   │       ├── ami_handler.py    # AMI event listener command
│   │       └── ari_handler.py    # ARI call handler command
│   ├── session/            # Session management
│   │   ├── session_manager.py
│   │   └── session_lifecycle.py
│   ├── models.py           # Django models (Tenant, etc.)
│   └── tests.py            # Core application tests
├── rtp_collector/          # RTP data collection
├── docs/                   # Documentation
│   ├── plan.md            # Comprehensive development plan
│   └── ...
├── logs/                   # Application logs
├── requirements.txt        # Python dependencies
├── pytest.ini            # pytest configuration
├── manage.py              # Django management script
└── README.md              # This file
```

## Environment Variables

The application uses `python-decouple` for configuration management. All sensitive settings should be defined in a `.env` file:

### Required Variables

- `SECRET_KEY`: Django secret key
- `DATABASE_NAME`: PostgreSQL database name
- `DATABASE_USER`: PostgreSQL username
- `DATABASE_PASSWORD`: PostgreSQL password

### Optional Variables

- `DEBUG`: Enable debug mode (default: False)
- `ALLOWED_HOSTS`: Comma-separated list of allowed hosts
- `DATABASE_HOST`: Database host (default: localhost)
- `DATABASE_PORT`: Database port (default: 5432)
- `RIVA_*`: NVIDIA RIVA configuration for speech services

## Development

### Adding WebSocket Consumers

```python
# In consumers.py
from channels.generic.websocket import WebsocketConsumer

class CallStatusConsumer(WebsocketConsumer):
    def connect(self):
        self.accept()
    
    def receive(self, text_data):
        # Handle incoming WebSocket messages
        pass
```

### Adding AMI Event Handlers

```python
# Using Panoramisk
from panoramisk import Manager

async def handle_newchannel(manager, message):
    # Process new channel events for session creation
    pass

manager = Manager()
manager.register_event('Newchannel', handle_newchannel)
```

## Scripts & Commands

### Django Management Commands

- `python manage.py runserver` - Start development server
- `python manage.py migrate` - Run database migrations
- `python manage.py createsuperuser` - Create admin user
- `python manage.py ami_handler` - Start AMI event listener
- `python manage.py ari_handler` - Start ARI call handler
- `python manage.py test` - Run tests

### Custom Scripts

- `test_audio_processing.py` - Audio processing tests
- `test_riva_transcription.py` - RIVA transcription tests

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests for new functionality
5. Run the test suite
6. Submit a pull request

## License

<!-- TODO: Add proper LICENSE file -->
This project uses the MIT License (referenced in `ari/__init__.py`). A formal LICENSE file should be added to the repository root.

## TODO

- [ ] Add formal LICENSE file to repository root
- [ ] Add ARI connection credentials to environment variables (currently hardcoded)
- [ ] Complete WebSocket routing implementation in ASGI configuration
- [ ] Add multi-tenant middleware implementation
- [ ] Document Asterisk configuration requirements
- [ ] Add Docker deployment configuration
- [ ] Implement comprehensive error handling and monitoring
- [ ] Add API documentation
