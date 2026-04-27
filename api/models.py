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

    class Meta:
        db_table = "t_tenant"
    
    # Onboarding Progress
    onboarding_step = models.IntegerField(default=0)
    
    # Organization Details
    address = models.TextField(null=True, blank=True)
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
    
    def __str__(self):
        return self.name

class User(AbstractUser):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='users', null=True)
    role = models.CharField(max_length=50, choices=[
        ('SUPER_ADMIN', 'Super Admin'),
        ('ADMIN', 'Admin'),
        ('HR', 'HR'),
        ('MANAGER', 'Manager'),
        ('EMPLOYEE', 'Employee')
    ], default='EMPLOYEE')
    
    is_verified = models.BooleanField(default=False)

    class Meta:
        db_table = "t_user"
    
    def __str__(self):
        return f"{self.username} ({self.tenant.name if self.tenant else 'No Tenant'})"

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
    name = models.CharField(max_length=255)
    description = models.TextField(null=True, blank=True)
    level = models.IntegerField(default=1)

    class Meta:
        db_table = "t_role"

    def __str__(self):
        return f"{self.name} ({self.tenant.name})"

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
    address = models.TextField(null=True, blank=True)

    # Bank Details
    bank_name = models.CharField(max_length=255, null=True, blank=True)
    account_number = models.CharField(max_length=50, null=True, blank=True)
    ifsc_code = models.CharField(max_length=20, null=True, blank=True)

    # Emergency Contact
    emergency_contact_name = models.CharField(max_length=255, null=True, blank=True)
    emergency_contact_phone = models.CharField(max_length=20, null=True, blank=True)

    # Onboarding Status
    onboarding_status = models.CharField(max_length=20, default='Pending') # Pending, InProgress, Completed
    invite_token = models.CharField(max_length=64, null=True, blank=True, unique=True)
    onboarding_completed_at = models.DateTimeField(null=True, blank=True)

    def generate_invite_token(self):
        import secrets
        self.invite_token = secrets.token_urlsafe(32)
        self.save()
        return self.invite_token

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


class LeaveType(TenantScopedModel):
    name = models.CharField(max_length=100)
    days_per_year = models.IntegerField(default=12)
    is_paid = models.BooleanField(default=True)
    carry_forward = models.BooleanField(default=False)

    class Meta:
        db_table = "t_leave_type"

    def __str__(self):
        return f"{self.name} ({self.tenant.name})"


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
