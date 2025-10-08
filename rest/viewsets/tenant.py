# Author: RA
# Purpose: Tenant View set
# Created: 07/10/2025

from rest_framework import viewsets
from core.models import Tenant
from core.serializers import TenantSerializer
from rest_framework.permissions import IsAuthenticated, DjangoModelPermissions

class TenantViewSet(viewsets.ModelViewSet):

    queryset = Tenant.objects.all()
    serializer_class = TenantSerializer
    permission_classes = [IsAuthenticated, DjangoModelPermissions]

    def get_queryset(self):
        user = self.request.user

        queryset = super().get_queryset()
        # if user.groups.filter(name='clients').exists():
        #     return Tenant.objects.filter(id=user.tenant_id)
        return queryset.none()

    def perform_create(self, serializer):
        serializer.save()


