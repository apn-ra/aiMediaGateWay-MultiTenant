"""
SystemConfiguration ViewSet for aiMediaGateway

Provides CRUD operations for SystemConfiguration management with key-based access.
Includes tenant isolation and configuration type validation.
"""

from rest_framework import viewsets, filters, status, serializers
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.db.models import Q
from django.utils import timezone
from django.core.exceptions import ValidationError

from core.models import SystemConfiguration
from core.serializers import SystemConfigurationSerializer
from core.junie_codes.permissions import TenantAdminPermission, TenantResourcePermission


class SystemConfigurationViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing SystemConfiguration with key-based access and tenant isolation.
    
    Provides:
    - CRUD operations for system configurations
    - Key-based configuration access
    - Type-safe configuration validation
    - Category-based organization
    - Tenant-scoped configuration management
    """
    
    serializer_class = SystemConfigurationSerializer
    permission_classes = [IsAuthenticated, TenantResourcePermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['category', 'value_type', 'is_system_default', 'created_at']
    search_fields = ['key', 'description', 'category']
    ordering_fields = ['key', 'category', 'created_at', 'updated_at']
    ordering = ['category', 'key']

    def get_queryset(self):
        """
        Filter configurations by tenant context and user permissions.
        """
        if not hasattr(self.request, 'tenant') or not self.request.tenant:
            return SystemConfiguration.objects.none()

        queryset = SystemConfiguration.objects.filter(tenant=self.request.tenant)
        
        # Non-admin users can only see non-sensitive configurations
        user_profile = getattr(self.request.user, 'userprofile', None)
        if user_profile and user_profile.role not in ['tenant_admin', 'super_admin']:
            # Filter out sensitive system configurations
            sensitive_categories = ['security', 'database', 'api_keys', 'credentials']
            queryset = queryset.exclude(category__in=sensitive_categories)
        
        return queryset

    def get_permissions(self):
        """
        Dynamic permission assignment based on action.
        Configuration write operations require admin permissions.
        """
        if self.action in ['create', 'update', 'partial_update', 'destroy', 'bulk_update', 'reset_to_default']:
            permission_classes = [IsAuthenticated, TenantAdminPermission]
        else:
            permission_classes = [IsAuthenticated, TenantResourcePermission]
        
        return [permission() for permission in permission_classes]

    def perform_create(self, serializer):
        """
        Create a new configuration with tenant context.
        """
        if not hasattr(self.request, 'tenant') or not self.request.tenant:
            raise serializers.ValidationError("Tenant context is required")
        
        # Check for duplicate keys within tenant
        key = serializer.validated_data['key']
        if SystemConfiguration.objects.filter(tenant=self.request.tenant, key=key).exists():
            raise serializers.ValidationError(f"Configuration key '{key}' already exists for this tenant")
        
        serializer.save(
            tenant=self.request.tenant,
            created_by=self.request.user
        )

    def perform_update(self, serializer):
        """
        Update configuration with audit trail.
        """
        old_value = self.get_object().value
        new_instance = serializer.save(
            updated_by=self.request.user,
            updated_at=timezone.now()
        )
        
        # Log configuration changes for audit
        if old_value != new_instance.value:
            print(f"Configuration '{new_instance.key}' changed from '{old_value}' to '{new_instance.value}' by {self.request.user.username}")

    @action(detail=False, methods=['get'])
    def by_category(self, request):
        """
        Get configurations grouped by category.
        """
        category_filter = request.query_params.get('category', None)
        queryset = self.get_queryset()
        
        if category_filter:
            queryset = queryset.filter(category=category_filter)
        
        # Group configurations by category
        categories_data = {}
        for config in queryset:
            category = config.category
            if category not in categories_data:
                categories_data[category] = []
            
            serializer = self.get_serializer(config)
            categories_data[category].append(serializer.data)
        
        return Response(categories_data)

    @action(detail=False, methods=['get'])
    def by_key(self, request):
        """
        Get configuration by key with type-safe value conversion.
        """
        key = request.query_params.get('key', None)
        if not key:
            return Response({
                'error': 'Key parameter is required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            config = self.get_queryset().get(key=key)
            serializer = self.get_serializer(config)
            data = serializer.data
            
            # Add typed value for convenience
            data['typed_value'] = config.get_typed_value()
            
            return Response(data)
        except SystemConfiguration.DoesNotExist:
            return Response({
                'error': f'Configuration with key "{key}" not found'
            }, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=['post'])
    def bulk_update(self, request):
        """
        Update multiple configurations in a single request.
        """
        configurations = request.data.get('configurations', [])
        if not configurations:
            return Response({
                'error': 'No configurations provided'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        updated_configs = []
        errors = []
        
        for config_data in configurations:
            key = config_data.get('key')
            value = config_data.get('value')
            
            if not key:
                errors.append({'error': 'Key is required', 'data': config_data})
                continue
            
            try:
                config = self.get_queryset().get(key=key)
                config.value = value
                config.updated_by = request.user
                config.updated_at = timezone.now()
                config.save()
                
                serializer = self.get_serializer(config)
                updated_configs.append(serializer.data)
                
            except SystemConfiguration.DoesNotExist:
                errors.append({'error': f'Configuration with key "{key}" not found', 'key': key})
            except ValidationError as e:
                errors.append({'error': str(e), 'key': key})
        
        return Response({
            'updated': updated_configs,
            'errors': errors,
            'total_updated': len(updated_configs),
            'total_errors': len(errors)
        })

    @action(detail=True, methods=['post'])
    def reset_to_default(self, request, pk=None):
        """
        Reset configuration to its default value.
        """
        config = self.get_object()
        
        if not hasattr(config, 'default_value') or config.default_value is None:
            return Response({
                'error': 'No default value available for this configuration'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        old_value = config.value
        config.value = config.default_value
        config.updated_by = request.user
        config.updated_at = timezone.now()
        config.save()
        
        return Response({
            'message': f'Configuration "{config.key}" reset to default value',
            'key': config.key,
            'old_value': old_value,
            'new_value': config.value
        })

    @action(detail=False, methods=['get'])
    def categories(self, request):
        """
        Get list of all available configuration categories.
        """
        categories = self.get_queryset().values_list('category', flat=True).distinct()
        category_counts = {}
        
        for category in categories:
            count = self.get_queryset().filter(category=category).count()
            category_counts[category] = count
        
        return Response({
            'categories': sorted(categories),
            'category_counts': category_counts,
            'total_categories': len(categories)
        })

    @action(detail=False, methods=['get'])
    def defaults(self, request):
        """
        Get all system default configurations.
        """
        queryset = self.get_queryset().filter(is_system_default=True)
        serializer = self.get_serializer(queryset, many=True)
        return Response({
            'defaults': serializer.data,
            'count': queryset.count()
        })

    @action(detail=False, methods=['get'])
    def validate_key(self, request):
        """
        Validate if a configuration key is available.
        """
        key = request.query_params.get('key', None)
        if not key:
            return Response({
                'error': 'Key parameter is required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        exists = self.get_queryset().filter(key=key).exists()
        
        return Response({
            'key': key,
            'available': not exists,
            'exists': exists
        })

    @action(detail=False, methods=['get'])
    def search_by_value(self, request):
        """
        Search configurations by value content.
        """
        search_term = request.query_params.get('search', None)
        if not search_term:
            return Response({
                'error': 'Search parameter is required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        queryset = self.get_queryset().filter(
            Q(value__icontains=search_term) |
            Q(description__icontains=search_term)
        )
        
        serializer = self.get_serializer(queryset, many=True)
        return Response({
            'results': serializer.data,
            'count': queryset.count(),
            'search_term': search_term
        })

    @action(detail=False, methods=['get'])
    def statistics(self, request):
        """
        Get configuration statistics for the tenant.
        """
        queryset = self.get_queryset()
        
        total_configs = queryset.count()
        system_defaults = queryset.filter(is_system_default=True).count()
        custom_configs = total_configs - system_defaults
        
        # Count by type
        type_counts = {}
        for value_type in ['string', 'integer', 'float', 'boolean', 'json']:
            type_counts[value_type] = queryset.filter(value_type=value_type).count()
        
        # Count by category
        category_counts = {}
        categories = queryset.values_list('category', flat=True).distinct()
        for category in categories:
            category_counts[category] = queryset.filter(category=category).count()
        
        # Recent changes (last 30 days)
        thirty_days_ago = timezone.now() - timezone.timedelta(days=30)
        recent_changes = queryset.filter(updated_at__gte=thirty_days_ago).count()
        
        statistics_data = {
            'total_configurations': total_configs,
            'system_defaults': system_defaults,
            'custom_configurations': custom_configs,
            'type_distribution': type_counts,
            'category_distribution': category_counts,
            'recent_changes_30_days': recent_changes,
            'tenant_name': self.request.tenant.name if self.request.tenant else 'Unknown',
            'generated_at': timezone.now()
        }
        
        return Response(statistics_data)
