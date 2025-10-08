# Author: RA
# Purpose: 
# Created: 08/10/2025

from rest_framework import viewsets
from django.contrib.auth.models import User
from core.serializers.user import UserSerializer, UserCreateSerializer
from rest_framework.permissions import IsAuthenticated, DjangoModelPermissions

class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all().order_by('id')
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated, DjangoModelPermissions]

    def get_queryset(self):
        user = self.request.user
        queryset = super().get_queryset()

        if user.is_superuser or user.groups.filter(name='admins').exists():
            return queryset.all()
        return queryset.none()

    def get_serializer_class(self):
        if self.action == 'create':
            return UserCreateSerializer
        return UserSerializer
