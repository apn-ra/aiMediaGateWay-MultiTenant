# Author: RA
# Purpose: Call Session ViewSet
# Created: 07/10/2025

from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.permissions import DjangoModelPermissions

from core.models import CallSession
from core.serializers import (
    CallSessionSerializer
)

class CallSessionViewSet(viewsets.ModelViewSet):
    queryset = CallSession.objects.all()
    serializer_class = CallSessionSerializer
    permission_classes = [IsAuthenticated, DjangoModelPermissions]
