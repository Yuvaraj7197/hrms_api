from rest_framework import serializers
from .models import User, Tenant, OTP, Department, Role, Employee, EmployeeDocument, AttendanceRecord, PayrollRecord, PayrollAuditLog, AttendanceStatus

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
    employee_code = serializers.CharField(source='employee_profile.employee_code', read_only=True, default='')
    
    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'role', 'tenant', 'is_verified', 'employee_code']

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

class EmployeeDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = EmployeeDocument
        fields = ['id', 'document_type', 'file_url', 'uploaded_at']

class EmployeeSerializer(serializers.ModelSerializer):
    documents = EmployeeDocumentSerializer(many=True, read_only=True)
    
    class Meta:
        model = Employee
        fields = [
            'id', 'name', 'email', 'phone', 'employee_code', 
            'department', 'designation', 'reporting_to', 'joining_date', 
            'status', 'base_salary',
            'dob', 'gender', 'address',
            'bank_name', 'account_number', 'ifsc_code',
            'emergency_contact_name', 'emergency_contact_phone',
            'onboarding_status', 'onboarding_completed_at',
            'documents'
        ]

class AttendanceStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = AttendanceStatus
        fields = ['id', 'code', 'label', 'color_code', 'default_check_in', 'default_check_out', 'default_work_hours']

class AttendanceRecordSerializer(serializers.ModelSerializer):
    status_code = serializers.CharField(source='status.code', read_only=True)
    status_label = serializers.CharField(source='status.label', read_only=True)
    
    class Meta:
        model = AttendanceRecord
        fields = ['id', 'employee', 'date', 'check_in', 'check_out', 'status', 'status_code', 'status_label', 'work_hours', 'location']

class PayrollRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = PayrollRecord
        fields = ['id', 'employee', 'cycle_month', 'base_salary', 'allowances', 'deductions', 'loan_emi', 'tax_status', 'net_pay', 'status']

class PayrollAuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = PayrollAuditLog
        fields = ['id', 'payroll_record', 'action', 'performed_by', 'created_at', 'notes']
