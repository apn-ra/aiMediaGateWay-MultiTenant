# Author: RA
# Purpose: 
# Created: 08/10/2025

from rest_framework import viewsets
from django.contrib.auth.models import User
from core.serializers.user import UserSerializer, UserCreateSerializer
from core.junie_codes.permissions import TenantResourcePermission
from rest_framework.permissions import IsAuthenticated, DjangoModelPermissions
from core.mixins import CsrfExemptMixin


class UserViewSet(CsrfExemptMixin, viewsets.ModelViewSet):
    queryset = User.objects.all().order_by('id')
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated, TenantResourcePermission]

    def get_queryset(self):
        user = self.request.user
        queryset = super().get_queryset()

        if user.is_superuser:
            return queryset.all()
        elif user.is_staff:
            return queryset.filter(is_superuser=False)
        return queryset.none()

    def get_serializer_class(self):
        if self.action == 'create':
            return UserCreateSerializer
        return UserSerializer
