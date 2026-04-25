from django.db import models
from django.contrib.auth.models import AbstractUser
import uuid

class Tenant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    domain = models.CharField(max_length=255, unique=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)
    
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
    
    def __str__(self):
        return f"{self.username} ({self.tenant.name if self.tenant else 'No Tenant'})"

class OTP(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    code = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    is_used = models.BooleanField(default=False)

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

    def __str__(self):
        return f"{self.name} ({self.tenant.name})"

class Role(TenantScopedModel):
    name = models.CharField(max_length=255)
    description = models.TextField(null=True, blank=True)
    level = models.IntegerField(default=1)

    def __str__(self):
        return f"{self.name} ({self.tenant.name})"

class Employee(TenantScopedModel):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='employee_profile', null=True, blank=True)
    name = models.CharField(max_length=255)
    email = models.EmailField()
    employee_code = models.CharField(max_length=50, null=True, blank=True)
    department = models.ForeignKey(Department, on_delete=models.SET_NULL, null=True, related_name='employees')
    designation = models.ForeignKey(Role, on_delete=models.SET_NULL, null=True, related_name='employees')
    reporting_to = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='subordinates')
    joining_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=50, default='Active')

    def __str__(self):
        return f"{self.name} - {self.employee_code}"
