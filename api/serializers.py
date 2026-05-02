from rest_framework import serializers
from .models import User, Tenant, OTP, Department, Role, Employee, EmployeeDocument, AttendanceRecord, PayrollRecord, PayrollAuditLog, AttendanceStatus

class TenantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tenant
        fields = [
            'id', 'name', 'domain', 'onboarding_step', 
            'address', 'phone', 'gst_number', 'pan_number', 
            'shift_start', 'shift_end', 'auto_attendance', 
            'industry_type', 'company_size', 'country', 'currency', 'timezone'
        ]

class UserSerializer(serializers.ModelSerializer):
    tenant = TenantSerializer(read_only=True)
    employee_code = serializers.CharField(source='employee_profile.employee_code', read_only=True, default='')
    role = serializers.CharField(source='system_role', read_only=True)

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
            'name', 'domain', 'address', 'phone', 'gst_number', 'pan_number', 
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
        fields = ['id', 'name', 'description', 'level', 'system_role_category']

class EmployeeDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = EmployeeDocument
        fields = ['id', 'document_type', 'file_url', 'uploaded_at']

class EmployeeSerializer(serializers.ModelSerializer):
    documents = EmployeeDocumentSerializer(many=True, read_only=True)

    # Resolved name fields
    department_name   = serializers.CharField(source='department.name',       read_only=True, default='')
    designation_name  = serializers.CharField(source='designation.name',      read_only=True, default='')
    reporting_to_name = serializers.CharField(source='reporting_to.name',     read_only=True, default='')
    user_role = serializers.CharField(source='user.system_role', read_only=True, default='EMPLOYEE')
    salary_structure_name = serializers.CharField(source='salary_structure.structure.name', read_only=True, default='')
    salary_structure_id = serializers.IntegerField(source='salary_structure.structure.id', read_only=True, default=None)
    reporting_to_code = serializers.CharField(source='reporting_to.employee_code', read_only=True, default='')

    class Meta:
        model = Employee
        fields = [
            # Identity
            'id', 'employee_code', 'name', 'email', 'phone', 'personal_email',

            # Job Info
            'department', 'department_name',
            'designation', 'designation_name',
            'user_role',
            'reporting_to', 'reporting_to_name', 'reporting_to_code',
            'joining_date', 'status', 'base_salary',
            'salary_structure_name', 'salary_structure_id',

            # Personal
            'dob', 'gender', 'address', 'current_address',
            'father_name', 'marital_status', 'blood_group', 'nationality',

            # Bank & Compliance
            'bank_name', 'account_number', 'account_type', 'ifsc_code', 'upi_id',
            'pan_number', 'aadhar_number', 'uan_number',
            'pf_applicable', 'esi_applicable', 'tax_regime',

            # Emergency
            'emergency_contact_name', 'emergency_contact_phone',

            # Onboarding
            'onboarding_status', 'last_invite_sent_at', 'onboarding_completed_at',

            # Documents
            'documents',
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
