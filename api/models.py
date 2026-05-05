from django.db import models
from django.contrib.auth.models import AbstractUser
import uuid

def generate_uuid_hex():
    return uuid.uuid4().hex

class Tenant(models.Model):
    id = models.CharField(primary_key=True, max_length=32, default=generate_uuid_hex, editable=False)
    name = models.CharField(max_length=255)
    domain = models.CharField(max_length=255, unique=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    
    
    # Onboarding Progress
    onboarding_step = models.IntegerField(default=0)
    
    # Organization Details
    address = models.TextField(null=True, blank=True)
    phone = models.CharField(max_length=20, null=True, blank=True)
    gst_number = models.CharField(max_length=15, null=True, blank=True)
    pan_number = models.CharField(max_length=10, null=True, blank=True)
    
    # Operations
    shift_start = models.TimeField(null=True, blank=True)
    shift_end = models.TimeField(null=True, blank=True)
    auto_attendance = models.BooleanField(default=False)
    
    # Industry & Localization
    industry_type = models.CharField(max_length=100, null=True, blank=True)
    company_size = models.CharField(max_length=50, null=True, blank=True)
    country = models.CharField(max_length=100, default='India')
    currency = models.CharField(max_length=10, default='INR')
    timezone = models.CharField(max_length=100, default='Asia/Kolkata')
    
    class Meta:
        db_table = "t_tenant"
        
    def __str__(self):
        return self.name

class User(AbstractUser):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='users', null=True)
    role = models.ForeignKey('Role', on_delete=models.SET_NULL, null=True, blank=True, related_name='users')
    
    is_verified = models.BooleanField(default=False)

    class Meta:
        db_table = "t_user"
    
    def __str__(self):
        return f"{self.username} ({self.tenant.name if self.tenant else 'No Tenant'})"

    @property
    def system_role(self):
        """Returns the canonical system role for this user.

        Resolution order (DB-first, name-pattern as legacy fallback):
          1. is_superuser                           → SUPER_ADMIN
          2. no role FK                             → EMPLOYEE
          3. role.system_role_category (DB column)  → that value  ← primary / DB-driven
          4. role.name pattern-match                → legacy fallback for unset rows
        """
        if self.is_superuser:
            return 'SUPER_ADMIN'
        if not self.role:
            return 'EMPLOYEE'

        # ── 1. DB-driven (preferred) ───────────────────────────────────────
        db_category = (self.role.system_role_category or '').strip().upper()
        valid_categories = {'SUPER_ADMIN', 'ADMIN', 'HR', 'MANAGER', 'EMPLOYEE'}
        if db_category in valid_categories:
            return db_category

        # ── 2. Name-pattern fallback (for legacy / unset rows) ─────────────
        name = self.role.name.upper()
        if 'SUPER' in name and 'ADMIN' in name:
            return 'SUPER_ADMIN'
        if 'ADMIN' in name:
            return 'ADMIN'
        if 'HR' in name:
            return 'HR'
        if 'MANAGER' in name or 'LEAD' in name:
            return 'MANAGER'
        return 'EMPLOYEE'

class OTP(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    code = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    is_used = models.BooleanField(default=False)

    class Meta:
        db_table = "t_otp"

    def is_expired(self):
        from django.utils import timezone
        import datetime
        return timezone.now() > self.created_at + datetime.timedelta(minutes=10)

class TenantScopedModel(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)

    class Meta:
        abstract = True

class Department(TenantScopedModel):
    name = models.CharField(max_length=255)
    head_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "t_department"

    def __str__(self):
        return f"{self.name} ({self.tenant.name})"

class Role(TenantScopedModel):
    SYSTEM_ROLE_CHOICES = [
        ('SUPER_ADMIN', 'Super Admin'),
        ('ADMIN',       'Admin'),
        ('HR',          'HR'),
        ('MANAGER',     'Manager'),
        ('EMPLOYEE',    'Employee'),
    ]

    name = models.CharField(max_length=255)
    description = models.TextField(null=True, blank=True)
    level = models.IntegerField(default=1)
    # DB-driven master: which system role category this designation belongs to.
    # If set, this value is used directly by User.system_role (no name-pattern matching).
    system_role_category = models.CharField(
        max_length=20,
        choices=SYSTEM_ROLE_CHOICES,
        default='EMPLOYEE',
        blank=True,
    )

    class Meta:
        db_table = "t_role"

    def __str__(self):
        return f"{self.name} ({self.tenant.name})"


class RolePermission(models.Model):
    """
    DB-driven portal permissions mapped to tenant `t_role` (designation roles).

    NOTE: We mark this as managed=False to avoid generating migrations in this repo
    (existing migrations are not aligned with current models). The permissions API
    will create the table if missing.
    """
    role = models.OneToOneField(Role, on_delete=models.CASCADE, related_name='portal_permission')
    # Example: ["dashboard", "attendance", "employees/new", "payroll", ...]
    allowed_routes = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "t_role_permission"

class Employee(TenantScopedModel):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='employee_profile', null=True, blank=True)
    name = models.CharField(max_length=255)
    email = models.EmailField()
    phone = models.CharField(max_length=20, null=True, blank=True)
    employee_code = models.CharField(max_length=50, null=True, blank=True)
    department = models.ForeignKey(Department, on_delete=models.SET_NULL, null=True, related_name='employees')
    designation = models.ForeignKey(Role, on_delete=models.SET_NULL, null=True, related_name='employees')
    reporting_to = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='subordinates')
    joining_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=50, default='Active')
    base_salary = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # Personal Details
    dob = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=20, null=True, blank=True)
    address = models.TextField(null=True, blank=True)  # Permanent address
    current_address = models.TextField(null=True, blank=True)  # Current/correspondence address
    father_name = models.CharField(max_length=255, null=True, blank=True)  # Required for PF
    marital_status = models.CharField(max_length=20, null=True, blank=True)  # Single/Married/Divorced
    blood_group = models.CharField(max_length=10, null=True, blank=True)  # A+/B-/O+ etc.
    nationality = models.CharField(max_length=100, default='Indian', blank=True)
    personal_email = models.EmailField(null=True, blank=True)  # For payslip delivery

    # Identity & Compliance (Indian Payroll Mandatory)
    pan_number = models.CharField(max_length=10, null=True, blank=True)   # ABCDE1234F — TDS/Form-16
    aadhar_number = models.CharField(max_length=12, null=True, blank=True) # 12-digit — PF/ESIC (stored masked)
    uan_number = models.CharField(max_length=12, null=True, blank=True)    # Universal Account No (PF)
    pf_applicable = models.BooleanField(default=True)                     # Employee can opt-out
    esi_applicable = models.BooleanField(default=False)                    # Applicable if gross < ₹21,000
    tax_regime = models.CharField(max_length=10, default='New')            # Old/New — affects IT slabs

    # Bank Details
    bank_name = models.CharField(max_length=255, null=True, blank=True)
    account_number = models.CharField(max_length=50, null=True, blank=True)
    ifsc_code = models.CharField(max_length=20, null=True, blank=True)
    account_type = models.CharField(max_length=20, default='Savings', blank=True)  # Savings/Current
    upi_id = models.CharField(max_length=100, null=True, blank=True)       # Backup payment

    # Emergency Contact
    emergency_contact_name = models.CharField(max_length=255, null=True, blank=True)
    emergency_contact_phone = models.CharField(max_length=20, null=True, blank=True)

    # Onboarding Status
    onboarding_status = models.CharField(max_length=20, default='Pending') # Pending, InProgress, Completed
    invite_token = models.CharField(max_length=64, null=True, blank=True, unique=True)
    last_invite_sent_at = models.DateTimeField(null=True, blank=True)
    onboarding_completed_at = models.DateTimeField(null=True, blank=True)

    def generate_invite_token(self):
        import secrets
        self.invite_token = secrets.token_urlsafe(32)
        self.save()
        return self.invite_token

    def save(self, *args, **kwargs):
        # Auto-sync User portal role with Employee designation
        if self.user and self.designation:
            if self.user.role != self.designation:
                self.user.role = self.designation
                self.user.save(update_fields=['role'])
        super().save(*args, **kwargs)

    class Meta:
        db_table = "t_employee"

    def __str__(self):
        return f"{self.name} - {self.employee_code}"

class AttendanceStatus(models.Model):
    code = models.CharField(max_length=10, unique=True) # P, L, A, LV, WFH, etc.
    label = models.CharField(max_length=50)
    color_code = models.CharField(max_length=7, default='#64748b')
    
    # Defaults for regularization
    default_check_in = models.TimeField(null=True, blank=True)
    default_check_out = models.TimeField(null=True, blank=True)
    default_work_hours = models.FloatField(default=0)

    class Meta:
        db_table = "t_attendance_status"
    
    def __str__(self):
        return self.label

class AttendanceRecord(TenantScopedModel):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='attendance')
    date = models.DateField()
    check_in = models.TimeField(null=True, blank=True)
    check_out = models.TimeField(null=True, blank=True)
    status = models.ForeignKey(AttendanceStatus, on_delete=models.PROTECT, related_name='records', null=True)
    status_str = models.CharField(max_length=20, default='Present', db_column='status') # For legacy support/transition
    work_hours = models.FloatField(default=0)
    location = models.CharField(max_length=100, default='Office')

    class Meta:
        db_table = "t_attendance_record"
        unique_together = ('tenant', 'employee', 'date')

class PayrollSetting(TenantScopedModel):
    pf_rate_employee = models.DecimalField(max_digits=5, decimal_places=2, default=12.0)
    pf_rate_employer = models.DecimalField(max_digits=5, decimal_places=2, default=12.0)
    esi_rate_employee = models.DecimalField(max_digits=5, decimal_places=2, default=0.75)
    esi_rate_employer = models.DecimalField(max_digits=5, decimal_places=2, default=3.25)
    tax_regime_default = models.CharField(max_length=20, default='New') # Old/New
    # Annual interest rate (%) used for loan interest computation during payroll run.
    loan_interest_rate_annual = models.DecimalField(max_digits=5, decimal_places=2, default=8.5)
    
    class Meta:
        db_table = "t_payroll_setting"

class SalaryComponent(TenantScopedModel):
    name = models.CharField(max_length=100) # Basic, HRA, Conveyance, PF
    code = models.CharField(max_length=20) # BASIC, HRA, CONV, PF
    component_type = models.CharField(max_length=20, choices=[('Earning', 'Earning'), ('Deduction', 'Deduction')])
    is_statutory = models.BooleanField(default=False)
    is_taxable = models.BooleanField(default=True)
    
    class Meta:
        db_table = "t_salary_component"
        unique_together = ('tenant', 'code')

    def __str__(self):
        return self.name

class SalaryStructure(TenantScopedModel):
    name = models.CharField(max_length=100) # e.g. "Standard Grade A"
    description = models.TextField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    
    class Meta:
        db_table = "t_salary_structure"

    def __str__(self):
        return self.name

class SalaryStructureComponent(models.Model):
    structure = models.ForeignKey(SalaryStructure, on_delete=models.CASCADE, related_name='components')
    component = models.ForeignKey(SalaryComponent, on_delete=models.CASCADE)
    calculation_type = models.CharField(max_length=20, choices=[('Fixed', 'Fixed Amount'), ('Percentage', 'Percentage of Basic')])
    value = models.DecimalField(max_digits=12, decimal_places=2, default=0) # amount or percentage
    
    class Meta:
        db_table = "t_salary_structure_component"

class EmployeeSalaryStructure(TenantScopedModel):
    employee = models.OneToOneField(Employee, on_delete=models.CASCADE, related_name='salary_structure')
    structure = models.ForeignKey(SalaryStructure, on_delete=models.SET_NULL, null=True)
    effective_from = models.DateField()
    is_active = models.BooleanField(default=True)
    
    class Meta:
        db_table = "t_employee_salary_structure"

class PayrollRecord(TenantScopedModel):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='payroll')
    cycle_month = models.CharField(max_length=7) # YYYY-MM
    base_salary = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    allowances = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    deductions = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    loan_emi = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_status = models.CharField(max_length=20, default='Pending')
    net_pay = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=20, default='Pending')

    # Payroll Compliance Fields (MVP+)
    gross_pay = models.DecimalField(max_digits=12, decimal_places=2, default=0)         # base + allowances
    employer_pf = models.DecimalField(max_digits=12, decimal_places=2, default=0)       # employer 12% PF
    esi_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)        # ESI if applicable
    tds_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)        # TDS deducted
    working_days = models.IntegerField(default=30)                                      # calendar days in month
    lop_days = models.IntegerField(default=0)                                           # Loss of Pay days

    # One-Time Adjustments for this cycle only
    # [{ "type": "bonus"|"deduction", "label": "Diwali Bonus", "amount": 5000 }]  
    one_time_adjustments = models.JSONField(default=list, blank=True)

    # Payment Tracking
    payment_reference = models.CharField(max_length=100, null=True, blank=True)  # Bank TXN ref
    paid_at = models.DateTimeField(null=True, blank=True)                         # Actual disbursal timestamp

    # Detailed breakdown for payslip
    # { "earnings": [{"name": "Basic", "amount": 25000}, ...], "deductions": [...] }
    breakdown = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "t_payroll_record"
        unique_together = ('tenant', 'employee', 'cycle_month')

class PayrollAuditLog(TenantScopedModel):
    payroll_record = models.ForeignKey(PayrollRecord, on_delete=models.CASCADE, related_name='audit_logs')
    action = models.CharField(max_length=255)
    performed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "t_payroll_audit_log"

class HolidayCalendar(TenantScopedModel):
    name = models.CharField(max_length=255)                     # e.g. "Independence Day"
    date = models.DateField()
    holiday_type = models.CharField(
        max_length=20,
        choices=[('National', 'National'), ('Optional', 'Optional'), ('Company', 'Company')],
        default='National'
    )
    description = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "t_holiday_calendar"
        unique_together = ('tenant', 'date')

    def __str__(self):
        return f"{self.name} ({self.date})"


class LeaveType(TenantScopedModel):
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=10, default='CL')         # CL, SL, EL, ML, etc.
    days_per_year = models.IntegerField(default=12)
    is_paid = models.BooleanField(default=True)
    carry_forward = models.BooleanField(default=False)
    max_carry_forward = models.IntegerField(default=0)           # max days carried over

    class Meta:
        db_table = "t_leave_type"

    def __str__(self):
        return f"{self.name} ({self.tenant.name})"


class LeaveBalance(TenantScopedModel):
    """Per-employee annual leave credit tracker."""
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='leave_balances')
    leave_type = models.ForeignKey(LeaveType, on_delete=models.CASCADE)
    year = models.IntegerField(default=2026)
    allocated = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    used = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    carried_forward = models.DecimalField(max_digits=5, decimal_places=1, default=0)

    class Meta:
        db_table = "t_leave_balance"
        unique_together = ('tenant', 'employee', 'leave_type', 'year')

    @property
    def remaining(self):
        return self.allocated + self.carried_forward - self.used

class LeaveApplication(TenantScopedModel):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='leave_applications')
    leave_type = models.ForeignKey(LeaveType, on_delete=models.CASCADE)
    from_date = models.DateField()
    to_date = models.DateField()
    reason = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=20, default='Pending')  # Pending/Approved/Rejected
    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='reviewed_leaves')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "t_leave_application"

    def days_count(self):
        return (self.to_date - self.from_date).days + 1

class EmployeeDocument(TenantScopedModel):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='documents')
    document_type = models.CharField(max_length=50) # Aadhar, PAN, Resume, Certificate
    file_url = models.TextField() # In real app, use FileField
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "t_employee_document"

# ── Master Data (Global / Not Tenant Scoped) ──────────────────────────────────

class IndustryMaster(models.Model):
    name = models.CharField(max_length=255)
    description = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "m_industry"
    
    def __str__(self):
        return self.name

class DepartmentMaster(models.Model):
    industry = models.ForeignKey(IndustryMaster, on_delete=models.CASCADE, related_name='departments', null=True, blank=True)
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=50)
    description = models.TextField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "m_department"
    
    def __str__(self):
        return f"{self.name} ({self.industry.name if self.industry else 'Global'})"

class RoleMaster(models.Model):
    department = models.ForeignKey(DepartmentMaster, on_delete=models.CASCADE, related_name='roles')
    name = models.CharField(max_length=255)
    level = models.IntegerField(default=1) # 1: Junior, 2: Mid, 3: Senior
    category = models.CharField(max_length=50, default='General') # Junior/Mid/Senior
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "m_role"
    
    def __str__(self):
        return f"{self.name} ({self.department.name})"
class Notification(TenantScopedModel):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    title = models.CharField(max_length=255)
    message = models.TextField()
    notify_type = models.CharField(max_length=20, default='info') # info, warning, success, task
    is_read = models.BooleanField(default=False)
    action_url = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "t_notification"
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} for {self.user.username}"
