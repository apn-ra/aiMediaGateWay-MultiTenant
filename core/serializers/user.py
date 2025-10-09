# Author: RA
# Purpose: User Serializer
# Created: 08/10/2025
from typing import Optional

from rest_framework import serializers
from django.contrib.auth.models import User
from core.models import Tenant, UserProfile

class TenantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tenant
        fields = ['id', 'name']
        read_only_fields = ['id']

class UserProfileSerializer(serializers.ModelSerializer):
    tenant = TenantSerializer(read_only=True)
    class Meta:
        model = UserProfile
        fields = ['id', 'role', 'account_id', 'tenant']
        read_only_fields = ['id']

class UserSerializer(serializers.ModelSerializer):
    group = UserProfileSerializer(source='profile', read_only=True)

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name', 'group']
        read_only_fields = ['id']


class UserCreateSerializer(serializers.ModelSerializer):
    confirm_password = serializers.CharField(write_only=True)
    tenant_id = serializers.IntegerField(write_only=True)
    group = serializers.CharField(write_only=True)

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
            'group'
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

        if validated_data.get('group') == 'operator' or validated_data.get('group') == 'admin':
            user.is_staff = True
        user.save()

        user.profile.tenant_id = validated_data['tenant_id']
        user.profile.role = validated_data['group']
        user.profile.save()

        validated_data.pop('tenant_id')
        validated_data.pop('group')
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

        if not attrs['group'] in dict(UserProfile.ROLE_CHOICES).keys():
            raise serializers.ValidationError({"group": f"Invalid role: {attrs['group']}."})

        return attrs
