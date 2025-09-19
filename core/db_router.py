"""
Multi-tenant database router for aiMediaGateway.
This router handles tenant-specific database operations and routing.
"""

class MultiTenantRouter:
    """
    A router to control database operations for multi-tenant models
    """
    
    def db_for_read(self, model, **hints):
        """Suggest the database that should be used for reads of objects of Model type."""
        # For now, use default database
        # In production, this could route based on tenant schema
        return 'default'

    def db_for_write(self, model, **hints):
        """Suggest the database that should be used for writes of objects of Model type."""
        # For now, use default database
        # In production, this could route based on tenant schema
        return 'default'

    def allow_relation(self, obj1, obj2, **hints):
        """Return True if a relation between obj1 and obj2 should be allowed."""
        # Allow relations within the same tenant
        if hasattr(obj1, 'tenant') and hasattr(obj2, 'tenant'):
            if hasattr(obj1.tenant, 'id') and hasattr(obj2.tenant, 'id'):
                return obj1.tenant.id == obj2.tenant.id
        
        # Allow relations for non-tenant specific models
        return True

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        """Ensure that certain apps' models get created on the right database."""
        # Allow all migrations for now
        # In production, this could control schema-specific migrations
        return True


class TenantAwareRouter:
    """
    Context manager and utilities for tenant-aware database operations
    """
    
    @staticmethod
    def get_tenant_schema(tenant_id):
        """Get schema name for a given tenant"""
        from .models import Tenant
        try:
            tenant = Tenant.objects.get(id=tenant_id)
            return tenant.schema_name
        except Tenant.DoesNotExist:
            return 'public'  # Default schema
    
    @staticmethod
    def filter_by_tenant(queryset, tenant):
        """Filter queryset by tenant if model has tenant field"""
        if hasattr(queryset.model, 'tenant'):
            return queryset.filter(tenant=tenant)
        return queryset
    
    @classmethod
    def get_tenant_from_request(cls, request):
        """Extract tenant from request (to be implemented based on URL/domain routing)"""
        # Placeholder implementation
        # In production, this would extract tenant from:
        # - Subdomain (tenant.example.com)
        # - URL path (/tenant/app/)
        # - Custom header
        # - Session data
        return None
