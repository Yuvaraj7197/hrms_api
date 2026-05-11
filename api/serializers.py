from rest_framework import serializers
from .models import (
    User, Tenant, OTP, Department, Role, Employee, EmployeeDocument, AttendanceRecord, PayrollRecord, PayrollAuditLog, AttendanceStatus,
    PayrollCycleLock, PayrollVariableInput, EmployeeLoan, EmployeeLoanLedger, ReimbursementCategory, ReimbursementClaim, PayrollArrear
    , Branch, Shift
)

class TenantSerializer(serializers.ModelSerializer):
    logo_url = serializers.SerializerMethodField()

    class Meta:
        model = Tenant
        fields = [
            'id', 'name', 'domain', 'onboarding_step', 
            'address', 'phone', 'gst_number', 'pan_number', 
            'shift_start', 'shift_end', 'auto_attendance', 
            'industry_type', 'company_size', 'country', 'currency', 'timezone',
            'logo', 'logo_url'
        ]

    def get_logo_url(self, obj):
        request = self.context.get('request') if hasattr(self, 'context') else None
        if not obj.logo:
            return None
        try:
            url = obj.logo.url
        except Exception:
            return None
        if request is not None:
            return request.build_absolute_uri(url)
        return url

class UserSerializer(serializers.ModelSerializer):
    tenant = TenantSerializer(read_only=True)
    employee_code = serializers.CharField(source='employee_profile.employee_code', read_only=True, default='')
    role = serializers.CharField(source='system_role', read_only=True)

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'role', 'tenant', 'is_verified', 'employee_code', 'must_change_password']

class RegisterSerializer(serializers.Serializer):
    company_name = serializers.CharField(max_length=255)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)

class OTPVerifySerializer(serializers.Serializer):
    email = serializers.EmailField()
    otp = serializers.CharField(max_length=6)

class OnboardingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tenant
        fields = [
            'name', 'domain', 'address', 'phone', 'gst_number', 'pan_number', 'logo',
            'shift_start', 'shift_end', 'auto_attendance', 
            'industry_type', 'company_size', 'country', 'currency', 'timezone',
            'onboarding_step'
        ]

class DepartmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = ['id', 'name', 'head_count']

class BranchSerializer(serializers.ModelSerializer):
    class Meta:
        model = Branch
        fields = ['id', 'code', 'name', 'address', 'city', 'state', 'country', 'is_active', 'created_at']

class ShiftSerializer(serializers.ModelSerializer):
    class Meta:
        model = Shift
        fields = ['id', 'code', 'name', 'start_time', 'end_time', 'grace_minutes', 'is_night_shift', 'is_active', 'created_at']

class RoleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Role
        fields = ['id', 'name', 'description', 'level', 'system_role_category']

class EmployeeDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = EmployeeDocument
        fields = ['id', 'document_type', 'file', 'uploaded_at', 'is_verified']

class EmployeeSerializer(serializers.ModelSerializer):
    documents = EmployeeDocumentSerializer(many=True, read_only=True)

    # Resolved name fields
    department_name   = serializers.CharField(source='department.name',       read_only=True, default='')
    designation_name  = serializers.CharField(source='designation.name',      read_only=True, default='')
    reporting_to_name = serializers.CharField(source='reporting_to.name',     read_only=True, default='')
    branch_name       = serializers.CharField(source='branch.name',           read_only=True, default='')
    user_role = serializers.CharField(source='user.system_role', read_only=True, default='EMPLOYEE')
    salary_structure_name = serializers.CharField(source='salary_structure.structure.name', read_only=True, default='')
    salary_structure_id = serializers.IntegerField(source='salary_structure.structure.id', read_only=True, default=None)
    reporting_to_code = serializers.CharField(source='reporting_to.employee_code', read_only=True, default='')
    reporting_hr_name = serializers.CharField(source='reporting_hr.name',     read_only=True, default='')
    reporting_hr_code = serializers.CharField(source='reporting_hr.employee_code', read_only=True, default='')

    shift_id = serializers.SerializerMethodField()
    shift_name = serializers.SerializerMethodField()

    class Meta:
        model = Employee
        fields = [
            # Identity
            'id', 'employee_code', 'name', 'email', 'phone', 'personal_email',

            # Job Info
            'branch', 'branch_name',
            'department', 'department_name',
            'designation', 'designation_name',
            'user_role',
            'reporting_to', 'reporting_to_name', 'reporting_to_code',
            'reporting_hr', 'reporting_hr_name', 'reporting_hr_code',
            'joining_date', 'status', 'base_salary',
            'shift_id', 'shift_name',
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

            # Extended Profile
            'extended_profile',
        ]

    def validate_department(self, value):
        user = self.context['request'].user
        if value and value.tenant != user.tenant:
            raise serializers.ValidationError("Department does not belong to your organization.")
        return value

    def _latest_shift_row(self, obj):
        """
        Fetch latest active shift assignment for the employee.
        Uses raw SQL to avoid requiring migrations in all environments.
        """
        from django.db import connection
        try:
            with connection.cursor() as cursor:
                vendor = getattr(connection, "vendor", "")
                if vendor == "sqlite":
                    cursor.execute(
                        """
                        SELECT esa.shift_id, s.name
                        FROM t_employee_shift_assignment esa
                        LEFT JOIN t_shift s ON s.id = esa.shift_id
                        WHERE esa.tenant_id = ? AND esa.employee_id = ? AND esa.is_active = 1
                        ORDER BY esa.effective_from DESC, esa.created_at DESC
                        LIMIT 1
                        """,
                        [str(obj.tenant_id), int(obj.id)],
                    )
                else:
                    cursor.execute(
                        """
                        SELECT esa.shift_id, s.name
                        FROM t_employee_shift_assignment esa
                        LEFT JOIN t_shift s ON s.id = esa.shift_id
                        WHERE esa.tenant_id = %s AND esa.employee_id = %s AND esa.is_active = 1
                        ORDER BY esa.effective_from DESC, esa.created_at DESC
                        LIMIT 1
                        """,
                        [obj.tenant_id, obj.id],
                    )
                return cursor.fetchone()
        except Exception:
            return None

    def get_shift_id(self, obj):
        row = self._latest_shift_row(obj)
        return row[0] if row else None

    def get_shift_name(self, obj):
        row = self._latest_shift_row(obj)
        return row[1] if row else ''

    def validate_designation(self, value):
        user = self.context['request'].user
        if value and value.tenant != user.tenant:
            raise serializers.ValidationError("Designation does not belong to your organization.")
        return value


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


class PayrollCycleLockSerializer(serializers.ModelSerializer):
    class Meta:
        model = PayrollCycleLock
        fields = ['id', 'cycle_month', 'attendance_locked', 'leave_locked', 'payroll_locked', 'locked_by', 'locked_at']


class PayrollVariableInputSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.name', read_only=True, default='')
    employee_code = serializers.CharField(source='employee.employee_code', read_only=True, default='')

    class Meta:
        model = PayrollVariableInput
        fields = [
            'id', 'employee', 'employee_name', 'employee_code',
            'cycle_month', 'input_type', 'label', 'amount', 'meta',
            'status', 'created_by', 'approved_by', 'created_at', 'updated_at',
        ]

    def validate_cycle_month(self, value):
        value = str(value or '').strip()
        if not value or len(value) != 7 or value[4] != '-':
            raise serializers.ValidationError('cycle_month must be in YYYY-MM format.')
        try:
            year, month = value.split('-')
            year_i = int(year)
            month_i = int(month)
            if year_i < 2000 or month_i < 1 or month_i > 12:
                raise ValueError
        except Exception:
            raise serializers.ValidationError('cycle_month must be in YYYY-MM format.')
        return value

    def validate_amount(self, value):
        if value is None:
            return value
        if value < 0:
            raise serializers.ValidationError('amount cannot be negative.')
        return value

    def validate(self, attrs):
        input_type = str(attrs.get('input_type') or '').upper()
        amount = attrs.get('amount')
        meta = attrs.get('meta') or {}
        if input_type == 'OVERTIME' and not isinstance(meta, dict):
            raise serializers.ValidationError({'meta': 'meta must be a JSON object.'})
        if input_type in {'INCENTIVE', 'BONUS', 'EARNING', 'ADJUSTMENT', 'REIMBURSEMENT'} and (amount is None or amount == 0):
            raise serializers.ValidationError({'amount': 'amount is required for incentive/bonus/earning entries.'})
        return attrs


class EmployeeLoanSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.name', read_only=True, default='')
    employee_code = serializers.CharField(source='employee.employee_code', read_only=True, default='')

    class Meta:
        model = EmployeeLoan
        fields = [
            'id', 'employee', 'employee_name', 'employee_code',
            'loan_code', 'principal_amount', 'annual_interest_rate',
            'tenure_months', 'emi_amount', 'start_cycle_month',
            'status', 'remarks', 'approved_by', 'approved_at', 'created_at',
        ]

    def validate_principal_amount(self, value):
        if value is None or value <= 0:
            raise serializers.ValidationError('principal_amount must be greater than zero.')
        return value

    def validate_annual_interest_rate(self, value):
        if value is None:
            return value
        if value < 0 or value > 100:
            raise serializers.ValidationError('annual_interest_rate must be between 0 and 100.')
        return value

    def validate_tenure_months(self, value):
        if value is None or value <= 0:
            raise serializers.ValidationError('tenure_months must be greater than zero.')
        return value

    def validate_emi_amount(self, value):
        if value is None or value < 0:
            raise serializers.ValidationError('emi_amount cannot be negative.')
        return value

    def validate_start_cycle_month(self, value):
        value = str(value or '').strip()
        if value and (len(value) != 7 or value[4] != '-'):
            raise serializers.ValidationError('start_cycle_month must be in YYYY-MM format.')
        return value

    def validate(self, attrs):
        principal = attrs.get('principal_amount')
        emi = attrs.get('emi_amount')
        if principal and emi and emi > principal:
            raise serializers.ValidationError({'emi_amount': 'emi_amount cannot exceed principal_amount.'})
        return attrs


class EmployeeLoanLedgerSerializer(serializers.ModelSerializer):
    class Meta:
        model = EmployeeLoanLedger
        fields = [
            'id', 'loan', 'cycle_month', 'opening_balance', 'emi_due', 'interest_due',
            'amount_paid', 'closing_balance', 'status', 'created_at',
        ]


class ReimbursementCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ReimbursementCategory
        fields = ['id', 'code', 'name', 'is_active', 'taxable', 'max_amount_per_month']


class ReimbursementClaimSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.name', read_only=True, default='')
    employee_code = serializers.CharField(source='employee.employee_code', read_only=True, default='')
    category_name = serializers.CharField(source='category.name', read_only=True, default='')
    category_code = serializers.CharField(source='category.code', read_only=True, default='')

    class Meta:
        model = ReimbursementClaim
        fields = [
            'id',
            'employee', 'employee_name', 'employee_code',
            'category', 'category_name', 'category_code',
            'cycle_month', 'claim_amount', 'description', 'attachments',
            'status', 'submitted_at', 'hr_approved_by', 'finance_approved_by',
            'approved_at', 'paid_at', 'payout_reference', 'created_at',
        ]


class PayrollArrearSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.name', read_only=True, default='')
    employee_code = serializers.CharField(source='employee.employee_code', read_only=True, default='')

    class Meta:
        model = PayrollArrear
        fields = [
            'id', 'employee', 'employee_name', 'employee_code',
            'from_cycle_month', 'to_cycle_month', 'arrear_amount',
            'reason', 'status', 'created_at'
        ]
