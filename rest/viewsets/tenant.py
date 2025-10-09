# Author: RA
# Purpose: Tenant View set
# Created: 07/10/2025

from rest_framework import viewsets, status
from core.models import Tenant, CallSession
from rest_framework.decorators import action
from rest_framework.response import Response
from core.serializers import TenantSerializer
from core.junie_codes.permissions import TenantResourcePermission
from rest_framework.permissions import IsAuthenticated, DjangoModelPermissions

class TenantViewSet(viewsets.ModelViewSet):

    queryset = Tenant.objects.all()
    serializer_class = TenantSerializer
    permission_classes = [IsAuthenticated, TenantResourcePermission]

    def get_queryset(self):
        user = self.request.user
        queryset = super().get_queryset()

        if user.is_superuser or user.is_staff:
            return queryset.all()
        return queryset.filter(tenant_id=user.profile.tenant_id).distinct()

    def perform_create(self, serializer):
        serializer.created_by = self.request.user
        serializer.save()

    @action(detail=True, methods=['post'])
    def activate(self, request, pk=None):
        """
            Activate a tenant.

            Args:
                request: HTTP request
                pk: Tenant primary key

            Returns:
                Response: Success or error message
        """
        tenant = self.get_object()

        if tenant.is_active:
            return Response(
                {'message': 'Tenant is already active'},
                status=status.HTTP_400_BAD_REQUEST
            )

        tenant.is_active = True
        tenant.save()

        tenant.logs.create(
            level='info',
            message=f'Tenant {tenant.name} activated by {request.user.username}',
            created_by=request.user
        )

        return Response({'message': 'Tenant activated successfully'})

    @action(detail=True, methods=['post'])
    def deactivate(self, request, pk=None):
        """
            Deactivate a tenant.

            Args:
                request: HTTP request
                pk: Tenant primary key

            Returns:
                Response: Success or error message
        """

        tenant = self.get_object()

        if not tenant.is_active:
            return Response(
                {'message': 'Tenant is already inactive'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check if tenant has active sessions
        active_sessions = CallSession.objects.filter(
            tenant=tenant,
            status__in=['detected', 'answered', 'bridged']
        ).count()

        if active_sessions > 0:
            return Response(
                {
                    'message': f'Cannot deactivate tenant with {active_sessions} active sessions',
                    'active_sessions': active_sessions
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        tenant.is_active = False
        tenant.save()

        tenant.logs.create(
            level='info',
            message=f'Tenant {tenant.name} deactivated by {request.user.username}',
            created_by=request.user
        )

        return Response({'message': 'Tenant deactivated successfully'})
