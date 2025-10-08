# Author: RA
# Purpose: User Serializer
# Created: 08/10/2025
from typing import Optional

from rest_framework import serializers
from django.contrib.auth.models import User
from core.models import Tenant, UserProfile

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name']
        read_only_fields = ['id']





class UserCreateSerializer(serializers.ModelSerializer):
    confirm_password = serializers.CharField(write_only=True)
    tenant_id = serializers.IntegerField(write_only=True)

    class Meta:
        model = User
        fields = [
            'id',
            'username',
            'email',
            'first_name',
            'last_name',
            'password',
            'confirm_password',
            'tenant_id',
        ]
        extra_kwargs = {
            'password': {'write_only': True}
        }
        read_only_fields = ['id']

    def create(self, validated_data):
        validated_data.pop('confirm_password')

        user = User.objects.create_user(
            username=validated_data['username'],
            email=validated_data.get('email'),
            password=validated_data['password'],
            first_name=validated_data.get('first_name', ''),
            last_name=validated_data.get('last_name', '')
        )

        user.profile.tenant_id = validated_data['tenant_id']
        user.profile.save()
        validated_data.pop('tenant_id')
        return user

    @staticmethod
    def _validate_tenant(tenant_id: int) -> Optional[bool]:
        if Tenant.objects.filter(id=tenant_id).exists():
            return True
        else:
            return False

    def validate(self, attrs):
        if attrs['password'] != attrs['confirm_password']:
            raise serializers.ValidationError({"password": "Passwords do not match."})

        if not self._validate_tenant(int(attrs['tenant_id'])):
            raise serializers.ValidationError({"tenant_id": "Tenant does not exist."})

        return attrs
