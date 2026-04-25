from rest_framework import serializers
from .models import User, Tenant, OTP, Department, Role, Employee

class TenantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tenant
        fields = [
            'id', 'name', 'domain', 'onboarding_step', 
            'address', 'gst_number', 'pan_number', 
            'shift_start', 'shift_end', 'auto_attendance', 
            'industry_type', 'company_size', 'country', 'currency', 'timezone'
        ]

class UserSerializer(serializers.ModelSerializer):
    tenant = TenantSerializer(read_only=True)
    
    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'role', 'tenant', 'is_verified']

class RegisterSerializer(serializers.Serializer):
    company_name = serializers.CharField(max_length=255)
    username = serializers.CharField(max_length=255)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

class OTPVerifySerializer(serializers.Serializer):
    email = serializers.EmailField()
    otp = serializers.CharField(max_length=6)

class OnboardingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tenant
        fields = [
            'name', 'domain', 'address', 'gst_number', 'pan_number', 
            'shift_start', 'shift_end', 'auto_attendance', 
            'industry_type', 'company_size', 'country', 'currency', 'timezone',
            'onboarding_step'
        ]

class DepartmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = ['id', 'name', 'head_count']

class RoleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Role
        fields = ['id', 'name', 'description', 'level']

class EmployeeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Employee
        fields = ['id', 'name', 'email', 'employee_code', 'department', 'designation', 'reporting_to', 'status']
