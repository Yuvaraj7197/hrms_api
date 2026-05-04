from rest_framework import status, views, permissions
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import authenticate
from django.utils import timezone
from decimal import Decimal
from .models import (
    User, Tenant, OTP, Department, Role, Employee, AttendanceRecord, PayrollRecord, 
    PayrollAuditLog, EmployeeDocument, AttendanceStatus, SalaryComponent, 
    SalaryStructure, SalaryStructureComponent, EmployeeSalaryStructure, PayrollSetting,
    LeaveType, LeaveBalance, LeaveApplication, HolidayCalendar,
    IndustryMaster, DepartmentMaster, RoleMaster, RolePermission
)
from .serializers import RegisterSerializer, OTPVerifySerializer, OnboardingSerializer, UserSerializer, DepartmentSerializer, RoleSerializer, EmployeeSerializer, AttendanceRecordSerializer, EmployeeDocumentSerializer, AttendanceStatusSerializer
from django.db import transaction, connection
from django.db.models import Q
from django.core.mail import send_mail
from django.conf import settings
import random
from rest_framework.permissions import AllowAny
import string

def safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


PAYROLL_PAID_STATUS_CODES = ('P', 'PRESENT', 'L', 'LATE', 'WO', 'WEEKLY OFF', 'H', 'HOLIDAY', 'PL', 'PAID LEAVE')
PAYROLL_PRESENT_STATUS_CODES = ('P', 'PRESENT', 'L', 'LATE')


def get_allowed_user_roles():
    role_field = Role._meta.get_field('system_role_category')
    choices = getattr(role_field, 'choices', []) or []
    return [str(code).upper() for code, _ in choices]


def ensure_role_permission_table_exists():
    """
    Creates `t_role_permission` table if missing.
    This repo's migrations are not aligned to current models, so we avoid migrations
    for this table and provision it safely at runtime.
    """
    vendor = getattr(connection, "vendor", "")
    with connection.cursor() as cursor:
        if vendor == "sqlite":
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='t_role_permission'")
            exists = cursor.fetchone()
            if exists:
                return
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS t_role_permission (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  role_id BIGINT NOT NULL UNIQUE,
                  allowed_routes TEXT NOT NULL,
                  created_at DATETIME NOT NULL,
                  updated_at DATETIME NOT NULL,
                  FOREIGN KEY(role_id) REFERENCES t_role(id) ON DELETE CASCADE
                )
                """
            )
            return

        # Default: MySQL/MariaDB
        cursor.execute("SHOW TABLES LIKE 't_role_permission'")
        exists = cursor.fetchone()
        if exists:
            return
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_role_permission (
              id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
              role_id BIGINT NOT NULL UNIQUE,
              allowed_routes JSON NOT NULL,
              created_at DATETIME(6) NOT NULL,
              updated_at DATETIME(6) NOT NULL,
              CONSTRAINT t_role_permission_role_fk
                FOREIGN KEY (role_id) REFERENCES t_role(id)
                ON DELETE CASCADE
            )
            """
        )


def ensure_master_tables_exist():
    """
    Creates master data tables (m_industry, m_department, m_role) if missing.
    Useful for local dev/demo environments where migrations might be skipped.
    """
    vendor = getattr(connection, "vendor", "")
    with connection.cursor() as cursor:
        if vendor == "sqlite":
            # Industry
            cursor.execute("CREATE TABLE IF NOT EXISTS m_industry (id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(255) NOT NULL, description TEXT)")
            # Department
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS m_department (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    industry_id INTEGER,
                    name VARCHAR(255) NOT NULL,
                    code VARCHAR(50) NOT NULL,
                    description TEXT,
                    is_active BOOLEAN DEFAULT 1,
                    FOREIGN KEY(industry_id) REFERENCES m_industry(id) ON DELETE SET NULL
                )
                """
            )
            # Role
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS m_role (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    department_id INTEGER NOT NULL,
                    name VARCHAR(255) NOT NULL,
                    level INTEGER DEFAULT 1,
                    category VARCHAR(50) DEFAULT 'General',
                    is_active BOOLEAN DEFAULT 1,
                    FOREIGN KEY(department_id) REFERENCES m_department(id) ON DELETE CASCADE
                )
                """
            )
            # Salary Structure Component
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS t_salary_structure_component (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    structure_id INTEGER NOT NULL,
                    component_id INTEGER NOT NULL,
                    calculation_type VARCHAR(20) NOT NULL,
                    value DECIMAL(12, 2) DEFAULT 0,
                    FOREIGN KEY(structure_id) REFERENCES t_salary_structure(id) ON DELETE CASCADE,
                    FOREIGN KEY(component_id) REFERENCES t_salary_component(id) ON DELETE CASCADE
                )
                """
            )
            return

        # MySQL / MariaDB
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS m_industry (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                description TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS m_department (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                industry_id BIGINT,
                name VARCHAR(255) NOT NULL,
                code VARCHAR(50) NOT NULL,
                description TEXT,
                is_active BOOLEAN DEFAULT 1,
                CONSTRAINT m_dept_industry_fk FOREIGN KEY (industry_id) REFERENCES m_industry(id) ON DELETE SET NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS m_role (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                department_id BIGINT NOT NULL,
                name VARCHAR(255) NOT NULL,
                level INT DEFAULT 1,
                category VARCHAR(50) DEFAULT 'General',
                is_active BOOLEAN DEFAULT 1,
                CONSTRAINT m_role_dept_fk FOREIGN KEY (department_id) REFERENCES m_department(id) ON DELETE CASCADE
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_employee_document (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                tenant_id CHAR(32) NOT NULL,
                employee_id BIGINT NOT NULL,
                document_type VARCHAR(50) NOT NULL,
                file_url TEXT NOT NULL,
                uploaded_at DATETIME(6) NOT NULL,
                CONSTRAINT t_emp_doc_tenant_fk FOREIGN KEY (tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE,
                CONSTRAINT t_emp_doc_emp_fk FOREIGN KEY (employee_id) REFERENCES t_employee(id) ON DELETE CASCADE
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_salary_component (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                tenant_id CHAR(32) NOT NULL,
                name VARCHAR(100) NOT NULL,
                code VARCHAR(20) NOT NULL,
                component_type VARCHAR(20) NOT NULL,
                is_statutory BOOLEAN DEFAULT 0,
                is_taxable BOOLEAN DEFAULT 1,
                CONSTRAINT t_sal_comp_tenant_fk FOREIGN KEY (tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_salary_structure (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                tenant_id CHAR(32) NOT NULL,
                name VARCHAR(100) NOT NULL,
                description TEXT,
                is_active BOOLEAN DEFAULT 1,
                CONSTRAINT t_sal_struct_tenant_fk FOREIGN KEY (tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_salary_structure_component (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                structure_id BIGINT NOT NULL,
                component_id BIGINT NOT NULL,
                calculation_type VARCHAR(20) NOT NULL,
                value DECIMAL(12, 2) DEFAULT 0,
                CONSTRAINT t_sal_struct_comp_struct_fk FOREIGN KEY (structure_id) REFERENCES t_salary_structure(id) ON DELETE CASCADE,
                CONSTRAINT t_sal_struct_comp_comp_fk FOREIGN KEY (component_id) REFERENCES t_salary_component(id) ON DELETE CASCADE
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_employee_salary_structure (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                tenant_id CHAR(32) NOT NULL,
                employee_id BIGINT NOT NULL UNIQUE,
                structure_id BIGINT,
                effective_from DATE,
                is_active BOOLEAN DEFAULT 1,
                CONSTRAINT t_emp_sal_tenant_fk FOREIGN KEY (tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE,
                CONSTRAINT t_emp_sal_emp_fk FOREIGN KEY (employee_id) REFERENCES t_employee(id) ON DELETE CASCADE,
                CONSTRAINT t_emp_sal_struct_fk FOREIGN KEY (structure_id) REFERENCES t_salary_structure(id) ON DELETE SET NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_payroll_setting (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                tenant_id CHAR(32) NOT NULL,
                pf_rate_employee DECIMAL(5, 2) DEFAULT 12.0,
                pf_rate_employer DECIMAL(5, 2) DEFAULT 12.0,
                esi_rate_employee DECIMAL(5, 2) DEFAULT 0.75,
                esi_rate_employer DECIMAL(5, 2) DEFAULT 3.25,
                tax_regime_default VARCHAR(20) DEFAULT 'New',
                loan_interest_rate_annual DECIMAL(5, 2) DEFAULT 8.5,
                CONSTRAINT t_pay_set_tenant_fk FOREIGN KEY (tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE
            )
            """
        )


DEFAULT_ADMIN_ROUTE_ALLOWLIST_BY_USER_ROLE = {
    # Fallback policy until role permissions are configured.
    'SUPER_ADMIN': ['*'],
    'ADMIN': ['*'],
    'HR': [
        'dashboard', 'pending', 'notifications',
        'employees/new', 'attendance',
        'leave-master', 'holiday-calendar',
        'payroll',
        'reports',
        'grievance', 'asset-register', 'recruitment', 'training', 'performance'
    ],
    'MANAGER': [
        'dashboard', 'pending', 'notifications',
        'employees/new', 'attendance',
        'reports',
        'grievance', 'asset-register', 'recruitment', 'training', 'performance'
    ],
}


ADMIN_ROUTE_KEYS = [
    'dashboard',
    'pending',
    'notifications',
    'employees/new',
    'attendance',
    'reports',
    'leave-master',
    'holiday-calendar',
    'asset-register',
    'recruitment',
    'setup',
    'role-permissions',
    'payroll',
    'performance',
    'training',
    'grievance',
    'settings',
]


def default_allowed_routes_for_role_name(role_name: str):
    """
    Best-effort mapping from designation (`t_role.name`) -> admin portal routes.
    Keep conservative defaults; grant broader access to managerial/executive roles.
    """
    name = (role_name or '').strip().lower()
    if not name:
        return ['dashboard', 'notifications']

    # Top leadership / owners
    if any(k in name for k in ['chief executive officer', 'ceo', 'director', 'vice president', 'vp', 'general manager']):
        return ['*']

    # C-level / architects / delivery/program/project leadership: strong visibility, no system settings
    if any(k in name for k in [
        'chief technology officer', 'cto',
        'chief financial officer', 'cfo',
        'technical architect', 'solution architect', 'architect',
        'delivery manager', 'program manager', 'project manager',
    ]):
        return [
            'dashboard', 'pending', 'notifications',
            'employees/new', 'attendance',
            'reports', 'performance', 'training',
            'asset-register', 'grievance',
            'payroll',
        ]

    # HR
    if 'talent acquisition' in name or 'recruiter' in name:
        return ['dashboard', 'notifications', 'recruitment', 'employees/new', 'reports']

    if 'hr' in name:
        # HR Intern -> limited
        if 'intern' in name:
            return ['dashboard', 'notifications', 'employees/new', 'recruitment']
        # HR roles -> full HR ops
        return [
            'dashboard', 'pending', 'notifications',
            'employees/new', 'attendance',
            'leave-master', 'holiday-calendar',
            'recruitment', 'training', 'performance',
            'grievance', 'asset-register',
            'reports', 'payroll',
        ]

    # Payroll / Finance / Accounts / Audit / Tax
    if any(k in name for k in ['payroll', 'accounts', 'accountant', 'finance', 'auditor', 'tax']):
        # Finance manager -> broader
        if 'manager' in name:
            return ['dashboard', 'pending', 'notifications', 'payroll', 'reports']
        return ['dashboard', 'notifications', 'payroll', 'reports']

    # Operations / plant / production / supply chain / quality
    if any(k in name for k in ['operations', 'plant', 'production', 'supply chain', 'quality']):
        if any(k in name for k in ['manager', 'plant manager']):
            return [
                'dashboard', 'pending', 'notifications',
                'employees/new', 'attendance',
                'asset-register', 'grievance',
                'reports',
            ]
        return ['dashboard', 'notifications', 'attendance', 'asset-register']

    # Sales / Marketing / BD
    if any(k in name for k in ['sales', 'business development', 'marketing', 'brand', 'digital marketing']):
        if any(k in name for k in ['manager', 'lead']):
            return ['dashboard', 'pending', 'notifications', 'reports']
        return ['dashboard', 'notifications', 'reports']

    # Admin / office / support
    if any(k in name for k in ['admin executive', 'office manager', 'office assistant', 'data entry', 'helpdesk', 'customer support', 'technical support']):
        if 'manager' in name:
            return ['dashboard', 'pending', 'notifications', 'attendance', 'reports']
        return ['dashboard', 'notifications', 'attendance']

    # Engineering / QA / Design / DevOps (typically should not be in admin portal; give minimal)
    if any(k in name for k in [
        'software engineer', 'developer', 'devops', 'qa', 'ui/ux', 'designer', 'full stack',
        'intern', 'trainee'
    ]):
        if any(k in name for k in ['lead', 'senior', 'architect']):
            return ['dashboard', 'pending', 'notifications', 'reports', 'attendance']
        return ['dashboard', 'notifications', 'attendance']

    # Default fallback
    return ['dashboard', 'notifications']


def has_route_access(user, required_route: str) -> bool:
    """
    Checks if a user has access to a specific portal route.
    Resolution order:
      1. SUPER_ADMIN / ADMIN (via system_role, which is now DB-driven) → full access
      2. t_role_permission mapping via user.role_id → check allowed_routes
      3. MANAGER fallback → their DEFAULT_ADMIN_ROUTE_ALLOWLIST_BY_USER_ROLE routes
    """
    sr = user.system_role  # reads system_role_category from DB (or fallback)
    if sr in ('SUPER_ADMIN', 'ADMIN'):
        return True

    if user.role_id:
        rp = RolePermission.objects.filter(role_id=user.role_id).first()
        if rp is not None:
            return required_route in rp.allowed_routes or '*' in rp.allowed_routes

    # Fallback: check the static allowlist for this system_role category
    fallback_routes = DEFAULT_ADMIN_ROUTE_ALLOWLIST_BY_USER_ROLE.get(sr, [])
    return required_route in fallback_routes or '*' in fallback_routes


def build_payroll_record_payload(tenant, record, attendance_stats_by_employee_id=None):
    import calendar
    try:
        year, month = map(int, str(record.cycle_month).split('-'))
        days_in_month = calendar.monthrange(year, month)[1]
    except Exception:
        days_in_month = 30

    eid = str(record.employee.id)
    if attendance_stats_by_employee_id is not None:
        present_days = attendance_stats_by_employee_id[eid]['present']
        paid_days = attendance_stats_by_employee_id[eid]['paid']
    else:
        attendance_qs = AttendanceRecord.objects.filter(
            tenant=tenant,
            employee=record.employee,
            date__startswith=record.cycle_month
        ).select_related('status')
        present_days = 0
        paid_days = 0
        for row in attendance_qs:
            status_code = (row.status.code if row.status else row.status_str or "").upper()
            if status_code in PAYROLL_PAID_STATUS_CODES:
                paid_days += 1
            if status_code in PAYROLL_PRESENT_STATUS_CODES:
                present_days += 1

    return {
        'id': str(record.id),
        'employeeId': str(record.employee.id),
        'employeeCode': record.employee.employee_code or '',
        'name': record.employee.name,
        'departmentName': record.employee.department.name if record.employee.department else 'N/A',
        'baseSalary': float(record.base_salary),
        'allowances': float(record.allowances),
        'deductions': float(record.deductions),
        'loanEMI': float(record.loan_emi),
        'employerPf': float(getattr(record, 'employer_pf', 0) or 0),
        'esiAmount': float(getattr(record, 'esi_amount', 0) or 0),
        'tdsAmount': float(getattr(record, 'tds_amount', 0) or 0),
        'workingDays': days_in_month if record.status == 'Pending' else int(getattr(record, 'working_days', 30) or 30),
        'presentDays': present_days,
        'lopDays': max(0, days_in_month - paid_days) if record.status == 'Pending' else int(getattr(record, 'lop_days', 0) or 0),
        'breakdown': record.breakdown,
        'adjustments': record.one_time_adjustments,
        'taxStatus': record.tax_status,
        'netPay': float(record.net_pay),
        'status': record.status,
    }

class RegisterView(views.APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        if serializer.is_valid():
            company_name = serializer.validated_data['company_name']
            email = serializer.validated_data['email']
            password = serializer.validated_data['password']

            if User.objects.filter(email=email).exists():
                return Response({"error": "Email already exists"}, status=status.HTTP_400_BAD_REQUEST)

            # Create Tenant
            tenant = Tenant.objects.create(name=company_name)

            # Auto-create an "Admin" Role for the tenant.
            admin_role, _ = Role.objects.get_or_create(
                tenant=tenant,
                name='Admin',
                defaults={
                    'description': 'Tenant Administrator',
                    'level': 10,
                    'system_role_category': 'ADMIN',
                }
            )
            
            # Create User using email as the username
            user = User.objects.create_user(
                username=email,
                email=email,
                password=password,
                tenant=tenant,
                role=admin_role,
            )

            # Generate OTP
            otp_code = str(random.randint(100000, 999999))
            OTP.objects.create(user=user, code=otp_code)

            # In a real app, send email here
            print(f"OTP for {email}: {otp_code}")

            return Response({
                "message": "Registration successful. Please verify OTP.",
                "email": email,
                "tenant_id": str(tenant.id)
            }, status=status.HTTP_201_CREATED)
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class VerifyOTPView(views.APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = OTPVerifySerializer(data=request.data)
        if serializer.is_valid():
            email = serializer.validated_data['email']
            code = serializer.validated_data['otp']

            try:
                user = User.objects.get(email=email)
                otp = OTP.objects.filter(user=user, code=code, is_used=False).latest('created_at')

                if otp.is_expired():
                    return Response({"error": "OTP expired"}, status=status.HTTP_400_BAD_REQUEST)

                user.is_verified = True
                user.save()
                
                tenant = user.tenant
                tenant.onboarding_step = 1
                tenant.save()

                otp.is_used = True
                otp.save()

                # Ensure Employee Profile exists
                Employee.objects.get_or_create(
                    user=user,
                    tenant=tenant,
                    defaults={
                        'name': user.username,
                        'email': user.email,
                        'status': 'Active'
                    }
                )

                # Seed all default master data for this new tenant
                try:
                    seed_tenant_defaults(tenant)
                except Exception as seed_err:
                    print(f'[SEED] Warning: {seed_err}')

                refresh = RefreshToken.for_user(user)
                return Response({
                    "message": "Email verified",
                    "access": str(refresh.access_token),
                    "refresh": str(refresh),
                    "user": UserSerializer(user).data
                })

            except (User.DoesNotExist, OTP.DoesNotExist):
                return Response({"error": "Invalid OTP"}, status=status.HTTP_400_BAD_REQUEST)
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


def seed_tenant_defaults(tenant):
    """
    Auto-provisions all master data for a new tenant after OTP verification.
    Seeds: AttendanceStatus, LeaveType, SalaryComponent, SalaryStructure, PayrollSetting.
    """
    # ── 1. Attendance Statuses ────────────────────────────────────────────
    default_statuses = [
        {'code': 'P',   'label': 'Present',     'color_code': '#22c55e', 'default_work_hours': 9.0},
        {'code': 'L',   'label': 'Late',        'color_code': '#eab308', 'default_work_hours': 8.0},
        {'code': 'A',   'label': 'Absent',      'color_code': '#ef4444', 'default_work_hours': 0.0},
        {'code': 'LV',  'label': 'Leave',       'color_code': '#8b5cf6', 'default_work_hours': 0.0},
        {'code': 'WO',  'label': 'Week Off',    'color_code': '#64748b', 'default_work_hours': 0.0},
        {'code': 'WFH', 'label': 'Work From Home','color_code': '#3b82f6','default_work_hours': 9.0},
        {'code': 'HD',  'label': 'Half Day',    'color_code': '#f97316', 'default_work_hours': 4.5},
        {'code': 'LOP', 'label': 'Loss of Pay', 'color_code': '#dc2626', 'default_work_hours': 0.0},
        {'code': 'H',   'label': 'Holiday',     'color_code': '#06b6d4', 'default_work_hours': 0.0},
    ]
    for s in default_statuses:
        AttendanceStatus.objects.get_or_create(
            code=s['code'],
            defaults={
                'label': s['label'],
                'color_code': s['color_code'],
                'default_work_hours': s['default_work_hours']
            }
        )

    # ── 2. Leave Types ────────────────────────────────────────────────────
    default_leave_types = [
        {'name': 'Casual Leave',  'code': 'CL', 'days_per_year': 12, 'is_paid': True,  'carry_forward': False},
        {'name': 'Sick Leave',    'code': 'SL', 'days_per_year': 12, 'is_paid': True,  'carry_forward': False},
        {'name': 'Earned Leave',  'code': 'EL', 'days_per_year': 18, 'is_paid': True,  'carry_forward': True,  'max_carry_forward': 30},
        {'name': 'Maternity Leave','code':'ML', 'days_per_year': 182,'is_paid': True,  'carry_forward': False},
        {'name': 'Loss of Pay',   'code': 'LOP','days_per_year': 0,  'is_paid': False, 'carry_forward': False},
    ]
    for lt in default_leave_types:
        LeaveType.objects.get_or_create(
            tenant=tenant,
            code=lt['code'],
            defaults={
                'name': lt['name'],
                'days_per_year': lt['days_per_year'],
                'is_paid': lt['is_paid'],
                'carry_forward': lt['carry_forward'],
                'max_carry_forward': lt.get('max_carry_forward', 0),
            }
        )

    # ── 3. Salary Components ──────────────────────────────────────────────
    default_components = [
        {'name': 'Basic Salary',       'code': 'BASIC',     'type': 'Earning',   'statutory': False, 'taxable': True},
        {'name': 'HRA',                'code': 'HRA',       'type': 'Earning',   'statutory': False, 'taxable': False},
        {'name': 'Conveyance',         'code': 'CONV',      'type': 'Earning',   'statutory': False, 'taxable': False},
        {'name': 'Special Allowance',  'code': 'SPEC_ALLOW','type': 'Earning',   'statutory': False, 'taxable': True},
        {'name': 'Medical Allowance',  'code': 'MED_ALLOW', 'type': 'Earning',   'statutory': False, 'taxable': False},
        {'name': 'PF - Employee',      'code': 'PF_EMP',    'type': 'Deduction', 'statutory': True,  'taxable': False},
        {'name': 'PF - Employer',      'code': 'PF_EMPLR',  'type': 'Deduction', 'statutory': True,  'taxable': False},
        {'name': 'ESI - Employee',     'code': 'ESI_EMP',   'type': 'Deduction', 'statutory': True,  'taxable': False},
        {'name': 'ESI - Employer',     'code': 'ESI_EMPLR', 'type': 'Deduction', 'statutory': True,  'taxable': False},
        {'name': 'Professional Tax',   'code': 'PTAX',      'type': 'Deduction', 'statutory': True,  'taxable': False},
        {'name': 'TDS / Income Tax',   'code': 'TDS',       'type': 'Deduction', 'statutory': True,  'taxable': False},
        {'name': 'Loss of Pay',        'code': 'LOP_DED',   'type': 'Deduction', 'statutory': False, 'taxable': False},
    ]
    component_map = {}
    for comp in default_components:
        obj, _ = SalaryComponent.objects.get_or_create(
            tenant=tenant,
            code=comp['code'],
            defaults={
                'name': comp['name'],
                'component_type': comp['type'],
                'is_statutory': comp['statutory'],
                'is_taxable': comp['taxable'],
            }
        )
        component_map[comp['code']] = obj

    # ── 4. Default Salary Structure ───────────────────────────────────────
    structure, created = SalaryStructure.objects.get_or_create(
        tenant=tenant,
        name='Standard Grade',
        defaults={'description': 'Default salary structure for all employees', 'is_active': True}
    )
    if created and component_map:
        structure_components = [
            {'code': 'BASIC',     'calc': 'Fixed',      'value': Decimal('30000')},
            {'code': 'HRA',       'calc': 'Percentage', 'value': Decimal('40')},   # 40% of basic
            {'code': 'CONV',      'calc': 'Fixed',      'value': Decimal('1600')},
            {'code': 'SPEC_ALLOW','calc': 'Percentage', 'value': Decimal('20')},   # 20% of basic
            {'code': 'PF_EMP',   'calc': 'Percentage', 'value': Decimal('12')},   # 12% of basic
            {'code': 'PTAX',     'calc': 'Fixed',       'value': Decimal('200')},
        ]
        for sc in structure_components:
            comp_obj = component_map.get(sc['code'])
            if comp_obj:
                SalaryStructureComponent.objects.get_or_create(
                    structure=structure,
                    component=comp_obj,
                    defaults={'calculation_type': sc['calc'], 'value': sc['value']}
                )

    # ── 5. Payroll Settings ───────────────────────────────────────────────
    PayrollSetting.objects.get_or_create(
        tenant=tenant,
        defaults={
            'pf_rate_employee': Decimal('12.0'),
            'pf_rate_employer': Decimal('12.0'),
            'esi_rate_employee': Decimal('0.75'),
            'esi_rate_employer': Decimal('3.25'),
            'tax_regime_default': 'New',
            'loan_interest_rate_annual': Decimal('8.5'),
        }
    )


class OnboardingRolesView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        tenant = request.user.tenant
        roles_data = request.data.get('roles', [])
        if not roles_data:
            # Do not wipe tenant designations if the UI is using DB-seeded roles.
            tenant.onboarding_step = max(int(getattr(tenant, 'onboarding_step', 0) or 0), 4)
            tenant.save(update_fields=['onboarding_step'])
            return Response({"message": "No role payload provided. Existing roles preserved."})
        
        # Clear existing and save new
        Role.objects.filter(tenant=tenant).delete()
        
        for role in roles_data:
            name = role.get('name')
            raw_category = str(role.get('system_role_category') or role.get('systemRoleCategory') or '').strip().upper()
            valid_cats = {'SUPER_ADMIN', 'ADMIN', 'HR', 'MANAGER', 'EMPLOYEE'}
            
            if raw_category in valid_cats:
                category = raw_category
            else:
                category = AdminSeedRolePermissionsView._derive_category(name)

            Role.objects.create(
                tenant=tenant,
                name=name,
                description=role.get('description'),
                level=role.get('level') or role.get('accessLevel') or 1,
                system_role_category=category,
            )

        tenant.onboarding_step = 4
        tenant.save(update_fields=['onboarding_step'])

        return Response({"message": "Roles saved successfully"})

class OnboardingEmployeesView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        tenant = request.user.tenant
        employees_data = request.data.get('employees', [])

        credentials_sent = 0
        emails_sent = []
        emails_failed = []

        with transaction.atomic():
            # 1. Create employees and user accounts first.
            for emp in employees_data:
                dept_raw = (
                    emp.get('department')
                    or emp.get('departmentId')
                    or emp.get('department_id')
                )
                role_raw = (
                    emp.get('role')
                    or emp.get('roleId')
                    or emp.get('role_id')
                    or emp.get('designation')
                    or emp.get('designationId')
                    or emp.get('designation_id')
                )
                email = emp.get('email')
                if not email:
                    continue

                dept_pk = safe_int(dept_raw)
                role_pk = safe_int(role_raw)
                dept = (
                    Department.objects.filter(tenant=tenant, id=dept_pk).first()
                    if dept_pk is not None
                    else Department.objects.filter(
                        tenant=tenant, name=str(dept_raw).strip()
                    ).first()
                    if dept_raw
                    else None
                )
                role = (
                    Role.objects.filter(tenant=tenant, id=role_pk).first()
                    if role_pk is not None
                    else Role.objects.filter(
                        tenant=tenant, name=str(role_raw).strip()
                    ).first()
                    if role_raw
                    else None
                )

                base_username = email.split('@')[0]
                generated_username = f"{base_username}_{random.randint(100, 999)}"
                temp_password = ''.join(random.choices(string.ascii_letters + string.digits, k=10))

                user, created = User.objects.get_or_create(
                    email=email,
                    defaults={
                        'username': generated_username,
                        'tenant': tenant,
                        # role FK left null; system_role resolves to 'EMPLOYEE' by default
                        'is_verified': True
                    }
                )

                # If user already exists without tenant, align it.
                if not created and user.tenant_id != tenant.id:
                    user.tenant = tenant

                # user.system_role assignment removed: 'EMPLOYEE'
                user.is_verified = True

                # New users get generated credentials.
                if created:
                    user.set_password(temp_password)
                    if role:
                        user.role = role
                    user.save()
                else:
                    if role and user.role_id != role.id:
                        user.role = role
                        user.save(update_fields=['role_id'])

                    try:
                        sent_count = send_mail(
                            subject='Your Login Credentials',
                            message=(
                                f"Hello {emp.get('name') or 'Employee'},\n\n"
                                f"Your account for {tenant.name} has been created.\n"
                                f"Username: {user.username}\n"
                                f"Temporary Password: {temp_password}\n\n"
                                "Please log in and change your password immediately."
                            ),
                            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'no-reply@hrms.local'),
                            recipient_list=[email],
                            fail_silently=False,
                        )
                        if sent_count > 0:
                            credentials_sent += 1
                            emails_sent.append(email)
                        else:
                            emails_failed.append({
                                "email": email,
                                "reason": "Mail backend returned zero sent emails"
                            })
                    except Exception as exc:
                        # Keep onboarding flow resilient if email setup is missing.
                        emails_failed.append({
                            "email": email,
                            "reason": str(exc)
                        })

                employee, _ = Employee.objects.update_or_create(
                    tenant=tenant,
                    email=email,
                    defaults={
                        'user': user,
                        'name': emp.get('name'),
                        # 'phone': emp.get('phone') or '',
                        'employee_code': emp.get('employeeCode') or emp.get('employee_code'),
                        'department': dept,
                        'designation': role,
                        'status': emp.get('status') or 'Active',
                        'joining_date': emp.get('joiningDate') or emp.get('joining_date') or None,
                    }
                )

            # 2. Setup reporting hierarchy.
            for emp in employees_data:
                manager_email = emp.get('reportingTo') or emp.get('reporting_to')
                if manager_email:
                    manager = Employee.objects.filter(tenant=tenant, email=manager_email).first()
                    if manager:
                        Employee.objects.filter(tenant=tenant, email=emp.get('email')).update(reporting_to=manager)

        tenant.onboarding_step = 5
        tenant.save(update_fields=['onboarding_step'])

        return Response({
            "message": "Employees and user accounts created successfully",
            "credentials_sent": credentials_sent,
            "emails_sent": emails_sent,
            "emails_failed": emails_failed
        })

class OnboardingSetupView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        tenant = request.user.tenant
        payload = request.data.copy()
        # Accept UI camelCase keys from legacy setup screens / onboarding.
        if 'companyName' in payload and 'name' not in payload:
            payload['name'] = payload.get('companyName')
        if 'phoneNumber' in payload and 'phone' not in payload:
            payload['phone'] = payload.get('phoneNumber')
        if 'industryType' in payload and 'industry_type' not in payload:
            payload['industry_type'] = payload.get('industryType')
        if 'companySize' in payload and 'company_size' not in payload:
            payload['company_size'] = payload.get('companySize')

        serializer = OnboardingSerializer(tenant, data=payload, partial=True)
        if serializer.is_valid():
            serializer.save()
            step = payload.get('onboarding_step', 2)
            tenant.onboarding_step = step
            tenant.save()
            return Response({
                "message": "Onboarding step updated",
                "tenant": serializer.data
            })
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class OnboardingDepartmentsView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        tenant = request.user.tenant
        departments_data = request.data.get('departments', [])
        
        # Clear existing and save new
        Department.objects.filter(tenant=tenant).delete()

        saved_departments = []
        for dept in departments_data:
            obj = Department.objects.create(
                tenant=tenant,
                name=dept.get('name'),
                head_count=dept.get('headCount', 0),
            )
            saved_departments.append(
                {
                    'id': str(obj.id),
                    'companyId': str(tenant.id),
                    'name': obj.name,
                    'headCount': obj.head_count,
                }
            )

        tenant.onboarding_step = 3
        tenant.save(update_fields=['onboarding_step'])

        return Response(
            {
                'message': 'Departments saved successfully',
                'departments': saved_departments,
            }
        )

class DashboardView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        tenant = request.user.tenant
        role = request.user.system_role
        
        data = {
            "stats": {},
            "recent_activities": [],
            "pending_tasks": 0
        }

        if role in ['ADMIN', 'SUPER_ADMIN']:
            data["stats"] = {
                "total_employees": Employee.objects.filter(tenant=tenant).count(),
                "departments": Department.objects.filter(tenant=tenant).count(),
                "active_payroll": PayrollRecord.objects.filter(tenant=tenant, status='Processed').count(),
                "pending_leaves": 5 # Placeholder until Leave model is fully implemented
            }
            data["pending_tasks"] = data["stats"]["pending_leaves"]
        
        elif role == 'MANAGER':
            # Managers see stats for their department
            try:
                emp_profile = request.user.employee_profile
                dept = emp_profile.department
                data["stats"] = {
                    "dept_employees": Employee.objects.filter(tenant=tenant, department=dept).count(),
                    "dept_attendance": AttendanceRecord.objects.filter(tenant=tenant, employee__department=dept, date=timezone.now().date()).count()
                }
            except: pass

        else: # EMPLOYEE
            try:
                emp_profile = request.user.employee_profile
                data["stats"] = {
                    "my_attendance_pct": 95, # Logic to be added
                    "remaining_leaves": 12
                }
            except: pass

        # Fetch real audit logs as activities
        activities = PayrollAuditLog.objects.filter(tenant=tenant).order_by('-created_at')[:5]
        data["recent_activities"] = [
            {
                "id": act.id,
                "action": act.action,
                "actor": act.performed_by.username if act.performed_by else "System",
                "occurred_at": act.created_at
            } for act in activities
        ]

        return Response(data)

class OnboardingDataView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ensure_master_tables_exist()
        tenant = request.user.tenant

        departments = list(
            Department.objects.filter(tenant=tenant).values('id', 'name', 'head_count')
        )
        roles = list(
            Role.objects.filter(tenant=tenant).values('id', 'name', 'description', 'level', 'system_role_category')
        )
        # Employee / job forms need tenant designations (t_role). New tenants may be empty until seeded.
        if not roles:
            sr = str(getattr(request.user, 'system_role', '') or '').upper()
            if sr in ('SUPER_ADMIN', 'ADMIN', 'HR', 'MANAGER'):
                AdminSeedRolePermissionsView.sync_catalog_for_tenant(tenant)
                roles = list(
                    Role.objects.filter(tenant=tenant).values('id', 'name', 'description', 'level', 'system_role_category')
                )
        employees_qs = Employee.objects.filter(tenant=tenant).select_related('department', 'designation', 'reporting_to')

        employees = []
        for employee in employees_qs:
            employees.append({
                'id': str(employee.id),
                'companyId': str(tenant.id),
                'employeeCode': employee.employee_code,
                'name': employee.name,
                'email': employee.email,
                'phone': employee.phone or '',
                'departmentId': str(employee.department_id) if employee.department_id else '',
                'roleId': str(employee.designation_id) if employee.designation_id else '',
                'reportingTo': str(employee.reporting_to_id) if employee.reporting_to_id else '',
                'joiningDate': employee.joining_date.isoformat() if employee.joining_date else '',
                'status': employee.status,
            })

        response_departments = [
            {
                'id': str(dept['id']),
                'companyId': str(tenant.id),
                'name': dept['name'],
                'headCount': dept['head_count'],
            }
            for dept in departments
        ]

        response_roles = [
            {
                'id': str(role['id']),
                'companyId': str(tenant.id),
                'name': role['name'],
                'accessLevel': role['level'],
                'permissions': [],
            }
            for role in roles
        ]

        return Response({
            'departments': response_departments,
            'roles': response_roles,
            'employees': employees,
            'user_role_options': get_allowed_user_roles(),
            'admin_route_keys': ADMIN_ROUTE_KEYS,
        })

class OnboardingEmployeeCreateView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        tenant = request.user.tenant
        payload = request.data

        department_id = payload.get('departmentId') or payload.get('department_id')
        role_id = payload.get('roleId') or payload.get('role_id')
        reporting_to_id = payload.get('reportingTo') or payload.get('reporting_to')

        department = Department.objects.filter(tenant=tenant, id=safe_int(department_id)).first() if department_id else None
        role = Role.objects.filter(tenant=tenant, id=safe_int(role_id)).first() if role_id else None
        manager = Employee.objects.filter(tenant=tenant, id=safe_int(reporting_to_id)).first() if reporting_to_id else None

        employee = Employee.objects.create(
            tenant=tenant,
            name=payload.get('name'),
            email=payload.get('email'),
            # phone=payload.get('phone') or '',
            employee_code=payload.get('employeeCode') or payload.get('employee_code'),
            department=department,
            designation=role,
            reporting_to=manager,
            status=payload.get('status') or 'Active',
            joining_date=payload.get('joiningDate') or payload.get('joining_date') or None
        )

        return Response({
            "message": "Employee created successfully",
            "id": employee.id
        }, status=status.HTTP_201_CREATED)

class OnboardingEmployeeDetailView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def put(self, request, employee_id):
        """Update an employee (full or partial).
        All fields are optional; if a field is omitted its current value is retained.
        Foreign‑key fields are only altered when the corresponding ID is supplied in the payload.
        """
        tenant = request.user.tenant
        employee = Employee.objects.filter(tenant=tenant, id=employee_id).first()
        if not employee:
            return Response({"error": "Employee not found"}, status=status.HTTP_404_NOT_FOUND)

        payload = request.data

        # Helper to fetch related objects safely
        def get_related(model, pk):
            return model.objects.filter(tenant=tenant, id=safe_int(pk)).first() if pk else None

        employee.name = payload.get('name', employee.name)
        employee.email = payload.get('email', employee.email)
        employee.employee_code = payload.get('employeeCode') or payload.get('employee_code') or employee.employee_code
        employee.status = payload.get('status', employee.status)
        employee.joining_date = payload.get('joiningDate') or payload.get('joining_date') or employee.joining_date
        # Onboarding fields – optional updates
        employee.dob = payload.get('dob') or employee.dob
        employee.gender = payload.get('gender') or employee.gender
        employee.address = payload.get('address') or employee.address
        employee.bank_name = payload.get('bank_name') or employee.bank_name
        employee.account_number = payload.get('account_number') or employee.account_number
        employee.ifsc_code = payload.get('ifsc_code') or employee.ifsc_code
        employee.emergency_contact_name = payload.get('emergency_contact_name') or employee.emergency_contact_name
        employee.emergency_contact_phone = payload.get('emergency_contact_phone') or employee.emergency_contact_phone
        employee.onboarding_status = payload.get('onboarding_status') or employee.onboarding_status

        # Update FK relationships only when IDs are present in the request
        employee.department = get_related(Department, payload.get('departmentId') or payload.get('department_id'))
        employee.designation = get_related(Role, payload.get('roleId') or payload.get('role_id'))
        employee.reporting_to = get_related(Employee, payload.get('reportingTo') or payload.get('reporting_to'))

        employee.save()
        return Response({"message": "Employee updated successfully"})

    def delete(self, request, employee_id):
        tenant = request.user.tenant
        employee = Employee.objects.filter(tenant=tenant, id=employee_id).first()
        if not employee:
            return Response({"error": "Employee not found"}, status=status.HTTP_404_NOT_FOUND)
        employee.delete()
        return Response({"message": "Employee deleted successfully"})

    def patch(self, request, employee_id):
        """Partial update – delegate to the PUT logic for consistency."""
        return self.put(request, employee_id)

class AttendanceDataView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        tenant = request.user.tenant
        target_date = request.query_params.get('date')
        target_month = request.query_params.get('month')

        if target_month:
            # Monthly view - Fetch all employees to ensure everyone is in the grid
            employees = Employee.objects.filter(tenant=tenant).select_related('department')
            records = AttendanceRecord.objects.filter(
                tenant=tenant, date__startswith=target_month
            )
            
            # Map records by employee ID
            record_map = {}
            for record in records:
                emp_id = str(record.employee_id)
                if emp_id not in record_map:
                    record_map[emp_id] = {}
                record_map[emp_id][record.date.isoformat()] = {
                    'id': str(record.id),
                    'status': record.status.code if record.status else record.status_str,
                    'checkIn': record.check_in.strftime('%H:%M') if record.check_in else '',
                    'checkOut': record.check_out.strftime('%H:%M') if record.check_out else '',
                    'workHours': float(record.work_hours)
                }

            grouped_data = []
            for emp in employees:
                emp_id = str(emp.id)
                grouped_data.append({
                    'employeeId': emp_id,
                    'employeeName': emp.name,
                    'employeeCode': emp.employee_code or '',
                    'departmentName': emp.department.name if emp.department else 'N/A',
                    'records': record_map.get(emp_id, {})
                })
            
            return Response({'monthly_data': grouped_data})

        else:
            target_date = target_date or timezone.localdate().isoformat()
            employees = Employee.objects.filter(tenant=tenant).select_related('department').distinct()
            records = AttendanceRecord.objects.filter(tenant=tenant, date=target_date).select_related('status')
            record_map = {str(r.employee_id): r for r in records}

            # Map for legacy conversion and ID lookup
            all_statuses = AttendanceStatus.objects.all()
            status_id_map = {s.code: str(s.id) for s in all_statuses}
            absent_id = status_id_map.get('A')

            LEGACY_MAP = {
                'Present': 'P', 'Late': 'L', 'Absent': 'A', 'LOP': 'LOP', 'WO': 'WO',
                'PRESENT': 'P', 'LATE': 'L', 'ABSENT': 'A'
            }

            response_data = []
            for employee in employees:
                record = record_map.get(str(employee.id))
                if record:
                    raw_status = record.status.code if record.status else record.status_str
                    status_code = LEGACY_MAP.get(raw_status, raw_status)
                    status_id = str(record.status_id) if record.status_id else status_id_map.get(status_code)
                    
                    response_data.append({
                        'id': str(record.id),
                        'employeeId': str(employee.id),
                        'employeeName': employee.name,
                        'employeeCode': employee.employee_code or '',
                        'departmentName': employee.department.name if employee.department else 'N/A',
                        'date': record.date.isoformat(),
                        'checkIn': record.check_in.strftime('%H:%M') if record.check_in else '',
                        'checkOut': record.check_out.strftime('%H:%M') if record.check_out else '',
                        'status': status_code,
                        'statusId': status_id,
                        'statusLabel': record.status.label if record.status else (record.status_str or 'Absent'),
                        'workHours': float(record.work_hours),
                        'location': record.location,
                    })
                else:
                    response_data.append({
                        'id': None,
                        'employeeId': str(employee.id),
                        'employeeName': employee.name,
                        'employeeCode': employee.employee_code or '',
                        'departmentName': employee.department.name if employee.department else 'N/A',
                        'date': target_date,
                        'checkIn': '',
                        'checkOut': '',
                        'status': 'A',
                        'statusId': absent_id,
                        'statusLabel': 'Absent',
                        'workHours': 0.0,
                        'location': 'N/A',
                    })

            return Response({'records': response_data})

class AttendanceStatusListView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        statuses = AttendanceStatus.objects.all()
        serializer = AttendanceStatusSerializer(statuses, many=True)
        return Response(serializer.data)

class AttendanceReportView(views.APIView):
    """
    GET /api/attendance/report/?month=YYYY-MM
    Returns per-employee monthly attendance summary:
      present, late, absent, lop, wo, effective_days, total_work_hours
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        tenant = request.user.tenant
        target_month = request.query_params.get('month') or timezone.localdate().strftime('%Y-%m')
        employee_id  = request.query_params.get('employee_id')  # optional filter

        qs = AttendanceRecord.objects.filter(tenant=tenant, date__startswith=target_month)
        if employee_id:
            qs = qs.filter(employee_id=employee_id)

        # Aggregate per employee
        from collections import defaultdict
        emp_stats = defaultdict(lambda: {
            'present': 0, 'late': 0, 'absent': 0, 'lop': 0, 'wo': 0,
            'total_work_hours': 0.0, 'records': []
        })
        for r in qs.select_related('employee__department', 'status'):
            eid = str(r.employee_id)
            s   = r.status.code if r.status else r.status_str
            if s in ['Present', 'P']:  emp_stats[eid]['present'] += 1
            elif s in ['Late', 'L']:   emp_stats[eid]['late'] += 1
            elif s in ['Absent', 'A']: emp_stats[eid]['absent'] += 1
            elif s in ['LOP']:         emp_stats[eid]['lop'] += 1
            elif s in ['WO']:          emp_stats[eid]['wo'] += 1
            emp_stats[eid]['total_work_hours'] += float(r.work_hours or 0)
            emp_stats[eid]['_emp'] = r.employee  # keep reference

        report = []
        for eid, stats in emp_stats.items():
            emp = stats.pop('_emp', None)
            if not emp:
                continue
            effective = stats['present'] + stats['late']
            report.append({
                'employeeId':    str(emp.id),
                'employeeName':  emp.name,
                'employeeCode':  emp.employee_code or '',
                'departmentName': emp.department.name if emp.department else 'N/A',
                'month':         target_month,
                'present':       stats['present'],
                'late':          stats['late'],
                'absent':        stats['absent'],
                'lop':           stats['lop'],
                'wo':            stats['wo'],
                'effectiveDays': effective,
                'totalWorkHours': round(stats['total_work_hours'], 2),
            })

        # Sort by employee name
        report.sort(key=lambda x: x['employeeName'])
        return Response({'report': report, 'month': target_month, 'total': len(report)})


class PayrollDataView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        tenant = request.user.tenant
        cycle_month = request.query_params.get('cycle') or timezone.localdate().strftime('%Y-%m')

        from collections import defaultdict
        attendance_qs = AttendanceRecord.objects.filter(tenant=tenant, date__startswith=cycle_month).select_related('status')
        att_stats = defaultdict(lambda: {'present': 0, 'paid': 0})
        for r in attendance_qs:
            eid = str(r.employee_id)
            s = (r.status.code if r.status else r.status_str or "").upper()
            if s in PAYROLL_PAID_STATUS_CODES:
                att_stats[eid]['paid'] += 1
            if s in PAYROLL_PRESENT_STATUS_CODES:
                att_stats[eid]['present'] += 1

        # Seed payroll records for all Active employees (regardless of onboarding status)
        employees = Employee.objects.filter(
            tenant=tenant,
            status='Active'
        ).select_related('department')

        for employee in employees:
            base_salary = employee.base_salary if employee.base_salary else Decimal('0')
            record, created = PayrollRecord.objects.get_or_create(
                tenant=tenant,
                employee=employee,
                cycle_month=cycle_month,
                defaults={
                    'base_salary': base_salary,
                    'allowances': base_salary * Decimal('0.2'),
                    'deductions': Decimal('0'),
                    'loan_emi': Decimal('5000') if employee.id % 4 == 0 else Decimal('0'),
                    'tax_status': 'Pending',
                    'net_pay': Decimal('0'),
                    'status': 'Pending',
                }
            )
            
            # Sync base salary if the record is still pending and doesn't match
            if not created and record.status == 'Pending' and record.base_salary != base_salary:
                record.base_salary = base_salary
                record.allowances = base_salary * Decimal('0.2')
                record.save(update_fields=['base_salary', 'allowances'])

        records = PayrollRecord.objects.filter(tenant=tenant, cycle_month=cycle_month).select_related('employee__department')
        response_data = [build_payroll_record_payload(tenant, record, att_stats) for record in records]
        return Response({'records': response_data, 'cycle': cycle_month})

class PayrollProcessView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        tenant = request.user.tenant
        action = request.data.get('action', 'process')
        cycle_month = request.data.get('cycle') or timezone.localdate().strftime('%Y-%m')

        records = PayrollRecord.objects.filter(tenant=tenant, cycle_month=cycle_month)
        
        # Check if cycle is locked
        if records.filter(status='Locked').exists():
             return Response({'error': 'This payroll cycle is locked and cannot be modified.'}, status=400)

        # Payroll settings used by the engine
        setting, _ = PayrollSetting.objects.get_or_create(
            tenant=tenant,
            defaults={
                'pf_rate_employee': Decimal('12.0'),
                'pf_rate_employer': Decimal('12.0'),
                'esi_rate_employee': Decimal('0.75'),
                'esi_rate_employer': Decimal('3.25'),
                'tax_regime_default': 'New',
                'loan_interest_rate_annual': Decimal('8.5'),
            }
        )

        # ── Terminal state actions (create audit logs) ─────────────────────────
        if action == 'pay':
            to_pay = list(records.filter(status='Approved'))
            for rec in to_pay:
                rec.status = 'Paid'
                rec.paid_at = timezone.now()
                rec.save(update_fields=['status', 'paid_at'])
                PayrollAuditLog.objects.create(
                    tenant=tenant,
                    payroll_record=rec,
                    action=f"Payroll disbursed (Paid) — {cycle_month}",
                    performed_by=request.user,
                )
            return Response({'message': 'Payroll payment completed', 'updated': len(to_pay)})
        
        if action == 'approve':
            to_approve = list(records.filter(status='Processed'))
            for rec in to_approve:
                rec.status = 'Approved'
                rec.save(update_fields=['status'])
                PayrollAuditLog.objects.create(
                    tenant=tenant,
                    payroll_record=rec,
                    action=f"Payroll approved (Approved) — {cycle_month}",
                    performed_by=request.user,
                )
            return Response({'message': 'Payroll batch approved', 'updated': len(to_approve)})
            
        if action == 'lock':
            to_lock = list(records.all())
            for rec in to_lock:
                rec.status = 'Locked'
                rec.save(update_fields=['status'])
                PayrollAuditLog.objects.create(
                    tenant=tenant,
                    payroll_record=rec,
                    action=f"Payroll cycle locked (Locked) — {cycle_month}",
                    performed_by=request.user,
                )
            return Response({'message': 'Payroll cycle locked', 'updated': len(to_lock)})

        # 1. Determine calendar days in the cycle month
        import calendar as _calendar
        try:
            _year, _month = map(int, cycle_month.split('-'))
            days_in_month = _calendar.monthrange(_year, _month)[1]
        except Exception:
            days_in_month = 30

        # 2. Fetch Attendance Report for LOP/present calculation
        paid_days_report = {}    # eid -> paid count (Present, WO, Holiday)
        present_report   = {}    # eid -> actual present count
        try:
            from collections import defaultdict
            qs = AttendanceRecord.objects.filter(tenant=tenant, date__startswith=cycle_month)
            for r in qs.select_related('status'):
                eid = str(r.employee_id)
                s = (r.status.code if r.status else r.status_str or "").upper()
                
                if eid not in paid_days_report:
                    paid_days_report[eid] = 0
                    present_report[eid] = 0
                
                # These statuses are considered "Paid"
                if s in ('P', 'PRESENT', 'L', 'LATE', 'WO', 'WEEKLY OFF', 'H', 'HOLIDAY', 'PL', 'PAID LEAVE'):
                    paid_days_report[eid] += 1
                    
                # These statuses are specifically "Present" for reporting
                if s in ('P', 'PRESENT', 'L', 'LATE'):
                    present_report[eid] += 1
        except Exception:
            pass
        except Exception:
            pass

        # ── Process Pending records ────────────────────────────────────────────
        updated = 0
        statutory_skip_codes = {'PF_EMP', 'PF_EMPLR', 'ESI_EMP', 'ESI_EMPLR', 'PTAX', 'TDS'}
        for record in records.filter(status='Pending'):
            base_salary = Decimal(record.base_salary or 0)
            breakdown = {"earnings": [], "deductions": []}

            total_earnings = Decimal('0')
            total_deductions = Decimal('0')

            employer_pf = Decimal('0')
            esi_emp = Decimal('0')
            tds_amount = Decimal('0')

            # 1) Earnings (and non-statutory deductions) from salary structure
            structure_link = EmployeeSalaryStructure.objects.filter(
                employee=record.employee, is_active=True
            ).first()

            if structure_link and structure_link.structure:
                # Always ensure Base Salary is the foundation of earnings
                total_earnings = base_salary
                breakdown["earnings"].append({"name": "Basic", "amount": float(base_salary), "code": "BASIC"})

                for sc in structure_link.structure.components.all():
                    # Skip if structure explicitly includes BASIC to avoid double-counting
                    if sc.component.code == 'BASIC':
                        continue
                        
                    amount = Decimal('0')
                    if sc.calculation_type == 'Fixed':
                        amount = sc.value
                    elif sc.calculation_type == 'Percentage':
                        amount = (base_salary * sc.value / Decimal('100')).quantize(Decimal('0.01'))

                    comp_data = {"name": sc.component.name, "amount": float(amount), "code": sc.component.code}

                    if sc.component.component_type == 'Earning':
                        total_earnings += amount
                        breakdown["earnings"].append(comp_data)
                    else:
                        # Skip statutory components; statutory deductions are computed below.
                        if sc.component.code in statutory_skip_codes:
                            continue
                        total_deductions += amount
                        breakdown["deductions"].append(comp_data)
            else:
                # Fallback to Basic + 20% Allowance logic
                allowance_amt = (base_salary * Decimal('0.2')).quantize(Decimal('0.01'))
                total_earnings = base_salary + allowance_amt
                breakdown["earnings"].append({"name": "Basic", "amount": float(base_salary), "code": "BASIC"})
                breakdown["earnings"].append({"name": "Standard Allowance", "amount": float(allowance_amt), "code": "SA"})

            # 2) Apply one-time adjustments (bonus/deduction) from the cycle
            adjustments = list(record.one_time_adjustments or [])
            for adj in adjustments:
                adj_type = adj.get('type')
                adj_label = (adj.get('label') or '').strip() or 'Adjustment'
                adj_amount = Decimal(str(adj.get('amount', 0) or 0))
                if adj_amount <= 0:
                    continue
                if adj_type == 'bonus':
                    total_earnings += adj_amount
                    breakdown["earnings"].append({
                        "name": adj_label,
                        "amount": float(adj_amount.quantize(Decimal('0.01'))),
                        "code": "BONUS",
                    })
                elif adj_type == 'deduction':
                    total_deductions += adj_amount
                    breakdown["deductions"].append({
                        "name": adj_label,
                        "amount": float(adj_amount.quantize(Decimal('0.01'))),
                        "code": "ADJ_DED",
                    })

            # 3) Statutory deductions computed by engine rules
            # PF: employee deduction affects net pay; employer contribution is stored separately for display.
            pf_emp = (base_salary * setting.pf_rate_employee / Decimal('100')).quantize(Decimal('0.01'))
            employer_pf = (base_salary * setting.pf_rate_employer / Decimal('100')).quantize(Decimal('0.01'))
            total_deductions += pf_emp
            breakdown["deductions"].append({"name": "Provident Fund", "amount": float(pf_emp), "code": "PF"})

            # TDS / Income tax slab (MVP rule). If you need full regime support, extend here.
            itax = base_salary * (
                Decimal('0.20') if base_salary > 100000 else (Decimal('0.10') if base_salary > 50000 else Decimal('0.05'))
            )
            tds_amount = itax.quantize(Decimal('0.01'))
            total_deductions += tds_amount
            breakdown["deductions"].append({"name": "Income Tax", "amount": float(tds_amount), "code": "ITAX"})

            ptax = Decimal('200') if base_salary > 15000 else Decimal('0')
            if ptax > 0:
                total_deductions += ptax
                breakdown["deductions"].append({"name": "Professional Tax", "amount": float(ptax), "code": "PTAX"})

            # ESI threshold logic (employee share only affects net pay)
            # If gross pay <= 21000, apply ESI (Employee % from tenant setting)
            if total_earnings <= Decimal('21000'):
                esi_emp = (total_earnings * setting.esi_rate_employee / Decimal('100')).quantize(Decimal('0.01'))
                total_deductions += esi_emp
                breakdown["deductions"].append({"name": "ESI (Employee)", "amount": float(esi_emp), "code": "ESI"})

            # LOP Deduction (Loss of Pay) — based on missing paid days
            paid_days = paid_days_report.get(str(record.employee_id), 0)
            lop_days = max(0, days_in_month - paid_days)
            
            per_day  = (base_salary / Decimal(days_in_month)).quantize(Decimal('0.01'))
            lop_deduction = per_day * Decimal(lop_days)
            if lop_deduction > 0:
                lop_ded_amt = lop_deduction.quantize(Decimal('0.01'))
                total_deductions += lop_ded_amt
                breakdown["deductions"].append({"name": "Loss of Pay", "amount": float(lop_ded_amt), "code": "LOP"})

            # Loan principal + interest
            loan_emi = Decimal(record.loan_emi or 0)
            loan_interest = Decimal('0.00')
            if loan_emi > 0:
                loan_interest = (loan_emi * setting.loan_interest_rate_annual / Decimal('100') / Decimal('12')).quantize(Decimal('0.01'))
                breakdown["deductions"].append({"name": "Loan EMI", "amount": float(loan_emi), "code": "EMI"})
                breakdown["deductions"].append({"name": "Loan Interest", "amount": float(loan_interest), "code": "INT"})

            # 4) Persist record fields
            record.lop_days    = int(lop_days)
            record.working_days = days_in_month
            record.gross_pay   = (total_earnings or Decimal('0')).quantize(Decimal('0.01'))
            record.allowances  = (total_earnings - base_salary).quantize(Decimal('0.01'))
            record.deductions  = total_deductions.quantize(Decimal('0.01'))
            record.employer_pf = employer_pf
            record.esi_amount  = esi_emp
            record.tds_amount  = tds_amount
            record.net_pay     = (total_earnings - total_deductions - loan_emi - loan_interest).quantize(Decimal('0.01'))
            record.breakdown   = breakdown
            record.status      = 'Processed'
            record.save()

            PayrollAuditLog.objects.create(
                tenant=tenant,
                payroll_record=record,
                action=f"Payroll processed (Processed) — {cycle_month}",
                performed_by=request.user,
            )
            updated += 1

        return Response({'message': 'Next-Level Payroll processing completed', 'updated': updated})


class PayrollTaxVerifyView(views.APIView):
    """
    Admin/HR: mark payroll tax proofs status for a specific employee payroll record.
    MVP: only updates PayrollRecord.tax_status (Pending/Verified/Rejected).
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, record_id: int):
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)

        tenant = request.user.tenant
        try:
            record = PayrollRecord.objects.get(tenant=tenant, id=record_id)
        except PayrollRecord.DoesNotExist:
            return Response({"error": "Record not found"}, status=404)

        new_status = request.data.get('taxStatus') or request.data.get('status') or 'Verified'
        if new_status not in ['Pending', 'Verified', 'Rejected']:
            return Response({"error": "Invalid tax status"}, status=400)

        record.tax_status = new_status
        record.save(update_fields=['tax_status'])

        PayrollAuditLog.objects.create(
            tenant=tenant,
            payroll_record=record,
            action=f"Tax proofs {new_status} — {record.employee.name} ({record.employee.employee_code})",
            performed_by=request.user,
        )

        return Response({"message": "Tax status updated", "taxStatus": record.tax_status})


class PayrollAuditLogsView(views.APIView):
    """Admin/HR: fetch payroll lifecycle audit logs for a given cycle."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        tenant = request.user.tenant
        cycle_month = request.query_params.get('cycle') or timezone.localdate().strftime('%Y-%m')

        records_ids = PayrollRecord.objects.filter(tenant=tenant, cycle_month=cycle_month).values_list('id', flat=True)
        logs_qs = PayrollAuditLog.objects.filter(tenant=tenant, payroll_record_id__in=records_ids).order_by('-created_at')[:200]

        # Keep formatting compatible with existing Angular AuditEngineModal parsing.
        formatted = []
        for log in logs_qs:
            time_part = log.created_at.strftime('%H:%M:%S')
            formatted.append(f'[{time_part}] {log.action}')

        return Response({"cycle": cycle_month, "total": len(formatted), "logs": formatted})


class PayrollForm16DownloadView(views.APIView):
    """MVP: returns a downloadable text package for Form-16 generation."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from django.http import HttpResponse

        tenant = request.user.tenant
        cycle_month = request.query_params.get('cycle') or timezone.localdate().strftime('%Y-%m')

        records = PayrollRecord.objects.filter(tenant=tenant, cycle_month=cycle_month).select_related('employee').order_by('employee__name')

        lines = [
            'FORM-16 PACKAGE (MVP / SIMULATED)',
            f'Cycle: {cycle_month}',
            f'Generated At: {timezone.now().isoformat()}',
            f'Records: {records.count()}',
            '',
            'Employee-wise Net Pay:',
        ]
        for r in records:
            lines.append(f'- {r.employee.name} ({r.employee.employee_code}) => Net Pay: {r.net_pay}')

        content = '\n'.join(lines)
        resp = HttpResponse(content, content_type='text/plain')
        resp['Content-Disposition'] = f'attachment; filename="form16_{cycle_month}.txt"'
        resp['Access-Control-Expose-Headers'] = 'Content-Disposition'
        return resp


class SalaryComponentView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ensure_master_tables_exist()
        qs = SalaryComponent.objects.filter(tenant=request.user.tenant)
        return Response([{"id": c.id, "name": c.name, "code": c.code, "type": c.component_type, "is_statutory": c.is_statutory} for c in qs])

    def post(self, request):
        """Create a new salary component (e.g. Basic, HRA, PF, Conveyance)."""
        ensure_master_tables_exist()
        tenant = request.user.tenant
        data = request.data
        name = data.get('name', '').strip()
        code = data.get('code', '').strip().upper()
        component_type = data.get('type') or data.get('component_type', 'Earning')
        is_statutory = data.get('is_statutory', False)
        is_taxable = data.get('is_taxable', True)

        if not name or not code:
            return Response({"error": "name and code are required."}, status=400)

        if SalaryComponent.objects.filter(tenant=tenant, code=code).exists():
            return Response({"error": f"Component with code '{code}' already exists."}, status=400)

        comp = SalaryComponent.objects.create(
            tenant=tenant,
            name=name,
            code=code,
            component_type=component_type,
            is_statutory=is_statutory,
            is_taxable=is_taxable,
        )
        return Response({"id": comp.id, "name": comp.name, "code": comp.code, "type": comp.component_type}, status=201)


class SalaryStructureView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]
    def get(self, request):
        ensure_master_tables_exist()
        qs = SalaryStructure.objects.filter(tenant=request.user.tenant).prefetch_related('components__component')
        data = []
        for s in qs:
            comps = [{"name": c.component.name, "type": c.calculation_type, "value": float(c.value), "id": c.id} for c in s.components.all()]
            data.append({"id": s.id, "name": s.name, "description": s.description, "components": comps})
        return Response(data)

    def post(self, request):
        ensure_master_tables_exist()
        tenant = request.user.tenant
        data = request.data
        with transaction.atomic():
            structure = SalaryStructure.objects.create(
                tenant=tenant,
                name=data.get('name'),
                description=data.get('description')
            )
            for comp in data.get('components', []):
                SalaryStructureComponent.objects.create(
                    structure=structure,
                    component_id=comp.get('component_id'),
                    calculation_type=comp.get('calculation_type'),
                    value=comp.get('value')
                )
        return Response({"message": "Salary Structure created", "id": structure.id})

class EmployeeSalarySetupView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]
    def post(self, request):
        ensure_master_tables_exist()
        tenant = request.user.tenant
        emp_id = request.data.get('employee_id')
        struct_id = request.data.get('structure_id')
        
        # DEBUG LOG
        print(f"DEBUG: emp_id={repr(emp_id)}, type={type(emp_id)}")
        if emp_id is None or emp_id == '':
            return Response({"error": f"Invalid emp_id: {repr(emp_id)}"}, status=400)
            
        try:
            emp_exists = Employee.objects.filter(id=emp_id).exists()
            if not emp_exists:
                # FALLBACK: If id doesn't match Employee, check if it's a PayrollRecord ID
                # This handles cases where the UI mistakenly passes the record ID instead of employee ID
                from api.models import PayrollRecord
                record = PayrollRecord.objects.filter(id=emp_id).select_related('employee').first()
                if record:
                    emp_id = record.employee.id
                else:
                    return Response({"error": f"Employee with id {emp_id} does not exist in DB."}, status=400)
        except Exception as e:
            return Response({"error": f"Exception checking emp_id {emp_id}: {str(e)}"}, status=400)
        
        EmployeeSalaryStructure.objects.update_or_create(
            tenant=tenant,
            employee_id=emp_id,
            defaults={
                'structure_id': struct_id,
                'effective_from': timezone.localdate(),
                'is_active': True
            }
        )
        return Response({"message": "Employee salary structure updated"})

class PayslipView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]
    def get(self, request, record_id):
        try:
            record = PayrollRecord.objects.get(tenant=request.user.tenant, id=record_id)
            emp = record.employee
            return Response({
                "company": {
                    "name": request.user.tenant.name if hasattr(request.user, 'tenant') else "Company Name",
                },
                "employee": {
                    "name":           emp.name,
                    "code":           emp.employee_code,
                    "department":     emp.department.name if emp.department else 'N/A',
                    "designation":    emp.designation.name if getattr(emp, 'designation', None) else 'N/A',
                    "joining_date":   emp.joining_date.strftime('%Y-%m-%d') if emp.joining_date else None,
                    "bank_name":      emp.bank_name,
                    "account_number": emp.account_number,
                    "ifsc_code":      emp.ifsc_code,
                    "pan_number":     getattr(emp, 'pan_number', None),
                    "uan_number":     getattr(emp, 'uan_number', None),
                    "tax_regime":     getattr(emp, 'tax_regime', 'New'),
                },
                "attendance": {
                    "working_days":   getattr(record, 'working_days', 30),
                    "present_days":   AttendanceRecord.objects.filter(
                        employee=emp, 
                        date__startswith=record.cycle_month,
                        status__code__in=['P', 'PRESENT', 'L', 'LATE']
                    ).count(),
                    "lop_days":       getattr(record, 'lop_days', 0),
                    "paid_days":      getattr(record, 'working_days', 30) - getattr(record, 'lop_days', 0),
                },
                "salary": {
                    "id":             record.id,
                    "cycle":          record.cycle_month,
                    "status":         record.status,
                    "base_salary":    float(record.base_salary),
                    "gross_pay":      float(getattr(record, 'gross_pay', 0)),
                    "total_deductions": float(getattr(record, 'deductions', 0)) + float(getattr(record, 'loan_emi', 0)) + float(getattr(record, 'tds_amount', 0)) + float(getattr(record, 'esi_amount', 0)),
                    "net_pay":        float(record.net_pay),
                    "breakdown":      record.breakdown or {"earnings": [], "deductions": []},
                    "adjustments":    getattr(record, 'one_time_adjustments', []),
                    "payment_reference": getattr(record, 'payment_reference', None),
                }
            })
        except PayrollRecord.DoesNotExist:
            return Response({"error": "Record not found"}, status=404)

class LoginView(views.APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        username = request.data.get('username')
        password = request.data.get('password')
        user = authenticate(username=username, password=password)

        if not user and username and '@' in username:
            matched_user = User.objects.filter(email=username).first()
            if matched_user:
                user = authenticate(username=matched_user.username, password=password)

        if user:
            if not user.is_verified:
                return Response({"error": "Please verify your email first", "is_verified": False, "email": user.email}, status=status.HTTP_403_FORBIDDEN)
            if user.system_role not in ['SUPER_ADMIN', 'ADMIN', 'HR', 'MANAGER']:
                return Response({"error": "Unauthorized role for admin portal"}, status=status.HTTP_403_FORBIDDEN)
            
            refresh = RefreshToken.for_user(user)
            return Response({
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": UserSerializer(user).data
            })
        
        return Response({"error": "Invalid credentials"}, status=status.HTTP_401_UNAUTHORIZED)

class AttendanceMarkView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        tenant = request.user.tenant
        employee_id = request.data.get('employee_id')
        action = request.data.get('action') # 'in' or 'out'
        now = timezone.now()
        today = now.date()
        time_now = now.time()

        try:
            status_p = AttendanceStatus.objects.filter(code='P').first()
            employee = Employee.objects.get(tenant=tenant, id=employee_id)
            record, created = AttendanceRecord.objects.get_or_create(
                tenant=tenant, 
                employee=employee, 
                date=today,
                defaults={'status': status_p, 'location': request.data.get('location', 'Office')}
            )

            if action == 'in':
                record.check_in = time_now
            elif action == 'out':
                record.check_out = time_now
                if record.check_in:
                    # Calculate work hours roughly
                    from datetime import datetime, combine
                    dt_in = combine(today, record.check_in)
                    dt_out = combine(today, record.check_out)
                    diff = dt_out - dt_in
                    record.work_hours = diff.total_seconds() / 3600
            
            record.save()
            return Response({"message": f"Attendance marked {action} successfully", "record_id": record.id})
        except Employee.DoesNotExist:
            return Response({"error": "Employee not found"}, status=404)

class AttendanceRegularizeView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        tenant = request.user.tenant
        record_id   = request.data.get('record_id')
        employee_id = request.data.get('employee_id')
        target_date = request.data.get('date')
        status_val  = request.data.get('status')

        try:
            # Map status code/label/ID to AttendanceStatus object
            status_obj = None
            if status_val:
                status_obj = AttendanceStatus.objects.filter(code=status_val).first() or \
                             AttendanceStatus.objects.filter(label=status_val).first() or \
                             AttendanceStatus.objects.filter(pk=status_val if str(status_val).isdigit() else -1).first()

            # Derive sensible defaults from the status object (if found)
            check_in   = request.data.get('check_in')
            check_out  = request.data.get('check_out')
            work_hours = request.data.get('work_hours')

            if status_obj:
                check_in   = check_in or (status_obj.default_check_in.strftime('%H:%M:%S') if status_obj.default_check_in else None)
                check_out  = check_out or (status_obj.default_check_out.strftime('%H:%M:%S') if status_obj.default_check_out else None)
                work_hours = work_hours if work_hours is not None else status_obj.default_work_hours

            if record_id:
                # Update existing record by PK
                record = AttendanceRecord.objects.get(tenant=tenant, id=record_id)
                if status_obj: record.status = status_obj
                record.check_in    = check_in
                record.check_out   = check_out
                record.work_hours  = work_hours if work_hours is not None else 0.0
                record.save()

            elif employee_id and target_date:
                # Upsert by employee + date
                record, created = AttendanceRecord.objects.get_or_create(
                    tenant=tenant,
                    employee_id=employee_id,
                    date=target_date,
                    defaults={
                        'status':     status_obj,
                        'check_in':   check_in,
                        'check_out':  check_out,
                        'work_hours': work_hours if work_hours is not None else 9.0,
                        'location':   'Office'
                    }
                )
                if not created:
                    if status_obj: record.status = status_obj
                    record.check_in   = check_in
                    record.check_out  = check_out
                    record.work_hours = work_hours if work_hours is not None else 0.0
                    record.save()
            else:
                return Response({"error": "Provide record_id OR (employee_id + date)"}, status=400)

            return Response({"message": "Attendance regularized successfully"})
        except AttendanceRecord.DoesNotExist:
            return Response({"error": "Record not found"}, status=404)
        except Exception as e:
            return Response({"error": str(e)}, status=500)



# ─────────────────────────────────────────────
# ROLE PERMISSION HELPER
# ─────────────────────────────────────────────
def require_roles(*allowed_roles):
    """Returns 403 if the user's role is not in allowed_roles."""
    def decorator(view_func):
        def wrapper(self, request, *args, **kwargs):
            if request.user.system_role not in allowed_roles:
                return Response({"error": "Permission denied"}, status=403)
            return view_func(self, request, *args, **kwargs)
        return wrapper
    return decorator


# ─────────────────────────────────────────────
# TASK 2A — HR Employee Management
# ─────────────────────────────────────────────
class HREmployeeListView(views.APIView):
    """HR/Admin: list all employees or create new one."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ensure_master_tables_exist()
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR', 'MANAGER']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        qs = Employee.objects.filter(tenant=tenant).select_related('department', 'designation', 'reporting_to').prefetch_related('documents')
        
        # MANAGER sees only their department
        if request.user.system_role == 'MANAGER':
            try:
                mgr_emp = request.user.employee_profile
                qs = qs.filter(department=mgr_emp.department)
            except Exception:
                qs = qs.none()
        
        serializer = EmployeeSerializer(qs, many=True)
        return Response({"employees": serializer.data, "total": qs.count()})

    def post(self, request):
        """HR/Admin creates a new employee and optionally creates a User account."""
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR','MANAGER']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        payload = request.data

        # Auto-generate employee code if not provided
        emp_code = payload.get('employee_code') or self._generate_code(tenant)

        dept = Department.objects.filter(tenant=tenant, id=safe_int(payload.get('department_id'))).first()
        role = Role.objects.filter(tenant=tenant, id=safe_int(payload.get('designation_id'))).first()
        manager = Employee.objects.filter(tenant=tenant, id=safe_int(payload.get('reporting_to_id'))).first()

        with transaction.atomic():
            employee = Employee.objects.create(
                tenant=tenant,
                name=payload.get('name'),
                email=payload.get('email'),
                phone=payload.get('phone', ''),
                employee_code=emp_code,
                department=dept,
                designation=role,
                reporting_to=manager,
                joining_date=payload.get('joining_date') or None,
                status=payload.get('status', 'Active'),
                base_salary=payload.get('base_salary', 0),
                dob=payload.get('dob'),
                gender=payload.get('gender'),
                address=payload.get('address'),
                current_address=payload.get('current_address') or payload.get('address'),
                bank_name=payload.get('bank_name'),
                account_number=payload.get('account_number'),
                ifsc_code=payload.get('ifsc_code'),
                account_type=payload.get('account_type', 'Savings'),
                upi_id=payload.get('upi_id'),
                personal_email=payload.get('personal_email'),
                emergency_contact_name=payload.get('emergency_contact_name'),
                emergency_contact_phone=payload.get('emergency_contact_phone'),
                onboarding_status=payload.get('onboarding_status', 'Pending'),
                # Statutory / Compliance fields
                father_name=payload.get('father_name'),
                pan_number=payload.get('pan_number'),
                aadhar_number=payload.get('aadhar_number'),
                uan_number=payload.get('uan_number'),
                pf_applicable=bool(payload.get('pf_applicable', True)),
                esi_applicable=bool(payload.get('esi_applicable', False)),
                tax_regime=payload.get('tax_regime', 'New'),
                marital_status=payload.get('marital_status'),
                blood_group=payload.get('blood_group'),
                nationality=payload.get('nationality', 'Indian'),
            )

            # Create login account if requested
            create_account = payload.get('create_account')
            if str(create_account).lower() == 'true' or create_account is True:
                password = payload.get('password')
                temp_password = password if password else ''.join(random.choices(string.ascii_letters + string.digits, k=10))
                username = payload.get('email').split('@')[0] + "_" + str(random.randint(100, 999))
                
                user, created = User.objects.get_or_create(
                    email=payload.get('email'),
                    defaults={
                        'username': username,
                        'tenant': tenant,
                        'is_verified': True
                    }
                )
                if not created:
                    user.tenant = tenant
                    user.is_verified = True
                
                if role and user.role_id != role.id:
                    user.role = role

                if created or password:
                    user.set_password(temp_password)
                user.save()
                    
                employee.user = user
                employee.save()
                print(f"[HR] New/Updated employee account: {username} / {temp_password}")

        return Response({
            "message": "Employee created successfully",
            "id": employee.id,
            "employee_code": emp_code,
        }, status=201)

    def _generate_code(self, tenant):
        count = Employee.objects.filter(tenant=tenant).count() + 1
        prefix = tenant.name[:3].upper() if tenant.name else "EMP"
        return f"{prefix}{count:04d}"


class HREmployeeDetailView(views.APIView):
    """HR/Admin: update or deactivate an employee."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, employee_id):
        ensure_master_tables_exist()
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR', 'MANAGER']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        try:
            e = Employee.objects.select_related('department', 'designation', 'reporting_to').prefetch_related('documents').get(tenant=tenant, id=employee_id)
        except Employee.DoesNotExist:
            return Response({"error": "Employee not found"}, status=404)
        
        serializer = EmployeeSerializer(e)
        return Response(serializer.data)

    def put(self, request, employee_id):
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        try:
            e = Employee.objects.get(tenant=tenant, id=employee_id)
        except Employee.DoesNotExist:
            return Response({"error": "Not found"}, status=404)

        p = request.data
        e.name = p.get('name', e.name)
        e.email = p.get('email', e.email)
        e.phone = p.get('phone', e.phone)
        e.status = p.get('status', e.status)
        e.joining_date = p.get('joining_date', e.joining_date)
        e.dob = p.get('dob', e.dob)
        e.gender = p.get('gender', e.gender)
        e.address = p.get('address', e.address)
        e.current_address = p.get('current_address', e.current_address)
        e.personal_email = p.get('personal_email', e.personal_email)
        e.bank_name = p.get('bank_name', e.bank_name)
        e.account_number = p.get('account_number', e.account_number)
        e.ifsc_code = p.get('ifsc_code', e.ifsc_code)
        e.account_type = p.get('account_type', e.account_type)
        e.upi_id = p.get('upi_id', e.upi_id)
        e.emergency_contact_name = p.get('emergency_contact_name', e.emergency_contact_name)
        e.emergency_contact_phone = p.get('emergency_contact_phone', e.emergency_contact_phone)
        e.onboarding_status = p.get('onboarding_status', e.onboarding_status)
        # Statutory fields
        e.father_name = p.get('father_name', e.father_name)
        e.pan_number = p.get('pan_number', e.pan_number)
        e.aadhar_number = p.get('aadhar_number', e.aadhar_number)
        e.uan_number = p.get('uan_number', e.uan_number)
        e.pf_applicable = p.get('pf_applicable', e.pf_applicable)
        e.esi_applicable = p.get('esi_applicable', e.esi_applicable)
        e.tax_regime = p.get('tax_regime', e.tax_regime)
        e.marital_status = p.get('marital_status', e.marital_status)
        e.blood_group = p.get('blood_group', e.blood_group)
        e.nationality = p.get('nationality', e.nationality)

        if 'base_salary' in p:
            e.base_salary = p['base_salary']
        if p.get('department_id'):
            e.department = Department.objects.filter(tenant=tenant, id=safe_int(p['department_id'])).first()
        if p.get('designation_id'):
            e.designation = Role.objects.filter(tenant=tenant, id=safe_int(p['designation_id'])).first()
        if p.get('reporting_to_id'):
            e.reporting_to = Employee.objects.filter(tenant=tenant, id=safe_int(p['reporting_to_id'])).first()
        e.save()

        # Optional: update linked login role designation (t_user.role FK -> t_role)
        if e.user and p.get('designation_id') is not None:
            role_obj = Role.objects.filter(tenant=e.tenant, id=safe_int(p.get('designation_id'))).first()
            if role_obj:
                e.user.role = role_obj
                e.user.save(update_fields=['role_id'])

        return Response({"message": "Employee updated"})

    def delete(self, request, employee_id):
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        Employee.objects.filter(tenant=tenant, id=employee_id).update(status='Terminated')
        return Response({"message": "Employee deactivated"})


# ─────────────────────────────────────────────
# TASK 3A — ESS: Self Check-In / Check-Out
# ─────────────────────────────────────────────
class ESSAttendanceTodayView(views.APIView):
    """Employee sees their own today's attendance status."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            emp = request.user.employee_profile
        except Exception:
            return Response({"error": "Employee profile not found"}, status=404)

        today = timezone.localdate()
        record = AttendanceRecord.objects.filter(tenant=request.user.tenant, employee=emp, date=today).first()
        if record:
            return Response({
                "date": str(today),
                "checked_in": bool(record.check_in),
                "checked_out": bool(record.check_out),
                "check_in": record.check_in.strftime('%H:%M') if record.check_in else None,
                "check_out": record.check_out.strftime('%H:%M') if record.check_out else None,
                "work_hours": float(record.work_hours),
                "status": record.status.label if record.status else record.status_str or "Not Marked",
                "record_id": record.id,
            })
        return Response({
            "date": str(today), "checked_in": False, "checked_out": False,
            "check_in": None, "check_out": None, "work_hours": 0, "status": "Not Marked"
        })

    def post(self, request):
        """Employee marks their own check-in or check-out."""
        try:
            emp = request.user.employee_profile
        except Exception:
            return Response({"error": "Employee profile not found"}, status=404)

        action = request.data.get('action')  # 'in' or 'out'
        if action not in ('in', 'out'):
            return Response({"error": "action must be 'in' or 'out'"}, status=400)

        tenant = request.user.tenant
        now = timezone.localtime()
        today = now.date()
        time_now = now.time()

        record, _ = AttendanceRecord.objects.get_or_create(
            tenant=tenant, employee=emp, date=today,
            defaults={
                'status': AttendanceStatus.objects.filter(code='P').first(),
                'status_str': 'Present',
                'location': request.data.get('location', 'Office')
            }
        )

        if action == 'in':
            if record.check_in:
                return Response({"error": "Already checked in"}, status=400)
            record.check_in = time_now
            status_present = AttendanceStatus.objects.filter(code='P').first()
            if status_present:
                record.status = status_present
            record.status_str = 'Present'
        else:
            if not record.check_in:
                return Response({"error": "Must check in first"}, status=400)
            if record.check_out:
                return Response({"error": "Already checked out"}, status=400)
            record.check_out = time_now
            # Calculate work hours
            from datetime import datetime, timedelta
            dt_in = datetime.combine(today, record.check_in)
            dt_out = datetime.combine(today, time_now)
            diff = dt_out - dt_in
            record.work_hours = round(diff.total_seconds() / 3600, 2)

        record.save()
        return Response({
            "message": f"Check-{action} successful",
            "time": time_now.strftime('%H:%M'),
            "work_hours": float(record.work_hours),
        })


class ESSAttendanceHistoryView(views.APIView):
    """Employee: own attendance history."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            emp = request.user.employee_profile
        except Exception:
            return Response({"error": "Employee profile not found"}, status=404)

        month = request.query_params.get('month')  # YYYY-MM
        qs = AttendanceRecord.objects.filter(tenant=request.user.tenant, employee=emp)
        if month:
            # For a specific month, return chronological order.
            qs = qs.filter(date__startswith=month).order_by('date')
        else:
            # Latest 30 records, then present them in chronological order.
            latest_30_ids = list(qs.order_by('-date').values_list('id', flat=True)[:30])
            qs = AttendanceRecord.objects.filter(id__in=latest_30_ids).order_by('date')

        data = [
            {
                "date": str(r.date),
                "check_in": r.check_in.strftime('%H:%M') if r.check_in else None,
                "check_out": r.check_out.strftime('%H:%M') if r.check_out else None,
                "status": r.status.label if r.status else r.status_str or "Not Marked",
                "work_hours": float(r.work_hours),
                "location": r.location,
            }
            for r in qs
        ]
        return Response({"records": data})


# ─────────────────────────────────────────────
# TASK 4 — ESS Profile & Payslips
# ─────────────────────────────────────────────
class ESSProfileView(views.APIView):
    """Employee: view own profile."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            emp = request.user.employee_profile
        except Exception:
            return Response({"error": "Employee profile not found"}, status=404)
        return Response({
            "id": emp.id,
            "employee_code": emp.employee_code or "",
            "name": emp.name,
            "email": emp.email,
            "phone": emp.phone or "",
            "department": emp.department.name if emp.department else "",
            "designation": emp.designation.name if emp.designation else "",
            "reporting_to": emp.reporting_to.name if emp.reporting_to else "",
            "joining_date": str(emp.joining_date) if emp.joining_date else "",
            "status": emp.status,
            "salary_structure": getattr(emp.salary_structure.structure, 'name', 'Standard (Default)') if hasattr(emp, 'salary_structure') else 'Standard (Default)'
        })


class ESSPayslipsView(views.APIView):
    """Employee: own payslip history."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            emp = request.user.employee_profile
        except Exception:
            return Response({"error": "Employee profile not found"}, status=404)

        records = PayrollRecord.objects.filter(tenant=request.user.tenant, employee=emp).order_by('-cycle_month')[:12]
        data = [
            {
                "id": r.id,
                "cycle_month": r.cycle_month,
                "base_salary": float(r.base_salary),
                "allowances": float(r.allowances),
                "deductions": float(r.deductions),
                "loan_emi": float(r.loan_emi),
                "net_pay": float(r.net_pay),
                "status": r.status,
                "tax_status": r.tax_status,
                "breakdown": r.breakdown,
                "working_days": getattr(r, 'working_days', 30),
                "lop_days": getattr(r, 'lop_days', 0),
            }
            for r in records
        ]
        return Response({"payslips": data})


# ─────────────────────────────────────────────
# TASK 5 — Leave Management
# ─────────────────────────────────────────────
class LeaveTypeView(views.APIView):
    """Tenant leave type configuration."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .models import LeaveType
        types = LeaveType.objects.filter(tenant=request.user.tenant)
        return Response({"leave_types": [
            {"id": t.id, "name": t.name, "days_per_year": t.days_per_year, "is_paid": t.is_paid}
            for t in types
        ]})

    def post(self, request):
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)
        from .models import LeaveType
        t = LeaveType.objects.create(
            tenant=request.user.tenant,
            name=request.data.get('name'),
            days_per_year=request.data.get('days_per_year', 12),
            is_paid=request.data.get('is_paid', True),
        )
        return Response({"id": t.id, "name": t.name}, status=201)


class LeaveApplicationView(views.APIView):
    """Employee applies for leave; HR/Manager approves/rejects."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .models import LeaveApplication
        tenant = request.user.tenant
        if request.user.system_role in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            qs = LeaveApplication.objects.filter(tenant=tenant).select_related('employee', 'leave_type')
        elif request.user.system_role == 'MANAGER':
            try:
                mgr = request.user.employee_profile
                qs = LeaveApplication.objects.filter(tenant=tenant, employee__department=mgr.department).select_related('employee', 'leave_type')
            except Exception:
                qs = LeaveApplication.objects.none()
        else:
            try:
                emp = request.user.employee_profile
                qs = LeaveApplication.objects.filter(tenant=tenant, employee=emp).select_related('leave_type')
            except Exception:
                qs = LeaveApplication.objects.none()

        data = [
            {
                "id": a.id,
                "employee_name": a.employee.name,
                "leave_type": a.leave_type.name,
                "from_date": str(a.from_date),
                "to_date": str(a.to_date),
                "reason": a.reason,
                "status": a.status,
                "created_at": str(a.created_at),
            }
            for a in qs.order_by('-created_at')
        ]
        return Response({"applications": data})

    def post(self, request):
        """Employee submits leave application."""
        from .models import LeaveType, LeaveApplication
        try:
            emp = request.user.employee_profile
        except Exception:
            return Response({"error": "Employee profile not found"}, status=404)

        leave_type = LeaveType.objects.filter(tenant=request.user.tenant, id=request.data.get('leave_type_id')).first()
        if not leave_type:
            return Response({"error": "Invalid leave type"}, status=400)

        app = LeaveApplication.objects.create(
            tenant=request.user.tenant,
            employee=emp,
            leave_type=leave_type,
            from_date=request.data.get('from_date'),
            to_date=request.data.get('to_date'),
            reason=request.data.get('reason', ''),
            status='Pending',
        )
        return Response({"message": "Leave application submitted", "id": app.id}, status=201)


class LeaveApproveView(views.APIView):
    """HR/Manager: approve or reject leave."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from .models import LeaveApplication
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR', 'MANAGER']:
            return Response({"error": "Permission denied"}, status=403)

        app_id = request.data.get('application_id')
        action = request.data.get('action')  # 'approve' or 'reject'
        if action not in ('approve', 'reject'):
            return Response({"error": "action must be approve or reject"}, status=400)

        try:
            app = LeaveApplication.objects.get(tenant=request.user.tenant, id=app_id)
        except LeaveApplication.DoesNotExist:
            return Response({"error": "Application not found"}, status=404)

        app.status = 'Approved' if action == 'approve' else 'Rejected'
        app.reviewed_by = request.user
        app.reviewed_at = timezone.now()
        app.save()
        return Response({"message": f"Leave {app.status.lower()} successfully"})


# ESS Login — allows EMPLOYEE role (separate from admin LoginView)
class ESSLoginView(views.APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        username = request.data.get('username')
        password = request.data.get('password')
        user = authenticate(username=username, password=password)

        if not user and username and '@' in username:
            matched = User.objects.filter(email=username).last()
            if matched:
                user = authenticate(username=matched.username, password=password)

        if user:
            if not user.is_verified:
                return Response({"error": "Account not verified"}, status=403)
            refresh = RefreshToken.for_user(user)
            user_data = UserSerializer(user).data
            # Attach employee profile if exists
            try:
                emp = user.employee_profile
                user_data['employee_id'] = emp.id
                user_data['employee_code'] = emp.employee_code
                user_data['department'] = emp.department.name if emp.department else ""
                user_data['designation'] = emp.designation.name if emp.designation else ""
            except Exception:
                pass
            return Response({
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": user_data
            })
        return Response({"error": "Invalid credentials"}, status=401)


class SendOnboardingInviteView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)
        
        employee_id = request.data.get('employee_id')
        action = request.data.get('action', 'invite')
        try:
            employee = Employee.objects.get(tenant=request.user.tenant, id=employee_id)
            token = employee.generate_invite_token()
            frontend_base = getattr(settings, 'FRONTEND_BASE_URL', 'http://localhost:4200').rstrip('/')
            invite_link = f"{frontend_base}/onboarding/{token}"

            if action == 'resend_credentials':
                if not employee.user:
                    return Response({"error": "Employee login account does not exist"}, status=400)

                temp_password = ''.join(random.choices(string.ascii_letters + string.digits, k=10))
                employee.user.set_password(temp_password)
                employee.user.save(update_fields=['password'])

                try:
                    send_mail(
                        subject='Your Updated Login Credentials',
                        message=(
                            f"Hello {employee.name},\n\n"
                            f"Your login credentials for {employee.tenant.name} have been reset.\n"
                            f"Username: {employee.user.username}\n"
                            f"Temporary Password: {temp_password}\n\n"
                            "Please log in and change your password immediately.\n"
                            f"Login URL: {frontend_base}/login\n"
                        ),
                        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'no-reply@hrms.local'),
                        recipient_list=[employee.email],
                        fail_silently=False,
                    )
                    employee.last_invite_sent_at = timezone.now()
                    employee.save(update_fields=['last_invite_sent_at'])
                    return Response({
                        "message": "Credentials resent successfully",
                        "email_sent": True,
                        "employee_id": employee.id
                    })
                except Exception as exc:
                    return Response({
                        "message": "Credentials reset done, but email failed",
                        "email_sent": False,
                        "employee_id": employee.id,
                        "reason": str(exc)
                    }, status=502)

            try:
                send_mail(
                    subject='Complete Your Employee Onboarding',
                    message=(
                        f"Hello {employee.name},\n\n"
                        "Please complete your onboarding using the link below:\n"
                        f"{invite_link}\n\n"
                        "If you did not expect this email, please contact your HR/Admin."
                    ),
                    from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'no-reply@hrms.local'),
                    recipient_list=[employee.email],
                    fail_silently=False,
                )
                employee.last_invite_sent_at = timezone.now()
                if employee.onboarding_status == 'Pending':
                    employee.onboarding_status = 'InProgress'
                    employee.save(update_fields=['onboarding_status', 'last_invite_sent_at'])
                else:
                    employee.save(update_fields=['last_invite_sent_at'])
                return Response({
                    "message": "Invite sent successfully",
                    "invite_link": invite_link,
                    "email_sent": True,
                    "employee_id": employee.id
                })
            except Exception as exc:
                return Response({
                    "message": "Invite link generated, but email failed",
                    "invite_link": invite_link,
                    "email_sent": False,
                    "employee_id": employee.id,
                    "reason": str(exc)
                }, status=502)
        except Employee.DoesNotExist:
            return Response({"error": "Employee not found"}, status=404)

class EmployeeOnboardingPublicView(views.APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request, token):
        try:
            employee = Employee.objects.get(invite_token=token)
            if employee.onboarding_status == 'Completed':
                return Response({"error": "Onboarding already completed"}, status=400)
            
            serializer = EmployeeSerializer(employee)
            return Response(serializer.data)
        except Employee.DoesNotExist:
            return Response({"error": "Invalid token"}, status=404)

    def post(self, request, token):
        try:
            employee = Employee.objects.get(invite_token=token)
            if employee.onboarding_status == 'Completed':
                return Response({"error": "Onboarding already completed"}, status=400)

            data = request.data
            
            # Update employee details
            employee.dob = data.get('dob', employee.dob)
            employee.gender = data.get('gender', employee.gender)
            employee.address = data.get('address', employee.address)
            employee.phone = data.get('phone', employee.phone)
            employee.personal_email = data.get('personal_email', employee.personal_email)

            # Personal Details
            employee.father_name = data.get('father_name', employee.father_name)
            employee.marital_status = data.get('marital_status', employee.marital_status)
            employee.blood_group = data.get('blood_group', employee.blood_group)
            employee.nationality = data.get('nationality', employee.nationality)
            employee.current_address = data.get('current_address', employee.current_address)

            # Identity & Compliance
            employee.pan_number = data.get('pan_number', employee.pan_number)
            employee.aadhar_number = data.get('aadhar_number', employee.aadhar_number)
            employee.uan_number = data.get('uan_number', employee.uan_number)
            employee.tax_regime = data.get('tax_regime', employee.tax_regime or 'New')

            # Bank Details
            employee.bank_name = data.get('bank_name', employee.bank_name)
            employee.account_number = data.get('account_number', employee.account_number)
            employee.ifsc_code = data.get('ifsc_code', employee.ifsc_code)
            employee.account_type = data.get('account_type', employee.account_type or 'Savings')
            employee.upi_id = data.get('upi_id', employee.upi_id)

            # Emergency Contact
            employee.emergency_contact_name = data.get('emergency_contact_name', employee.emergency_contact_name)
            employee.emergency_contact_phone = data.get('emergency_contact_phone', employee.emergency_contact_phone)

            # Documents
            docs = data.get('documents', [])
            for doc in docs:
                EmployeeDocument.objects.update_or_create(
                    tenant=employee.tenant,
                    employee=employee,
                    document_type=doc.get('document_type'),
                    defaults={'file_url': doc.get('file_url')}
                )

            employee.onboarding_status = 'Completed'
            employee.onboarding_completed_at = timezone.now()
            employee.status = 'Active'
            employee.save()

            return Response({"message": "Onboarding completed successfully"})
        except Employee.DoesNotExist:
            return Response({"error": "Invalid token"}, status=404)


class PayrollAdjustmentView(views.APIView):
    """One-time bonus or deduction for a specific payroll cycle."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, record_id):
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)
        try:
            record = PayrollRecord.objects.get(tenant=request.user.tenant, id=record_id)
            if record.status in ['Locked', 'Paid']:
                return Response({"error": f"Cannot adjust a {record.status} payroll record."}, status=400)

            adj_type  = request.data.get('type')   # 'bonus' or 'deduction'
            label     = request.data.get('label', '').strip()
            amount    = float(request.data.get('amount', 0))

            if adj_type not in ('bonus', 'deduction'):
                return Response({"error": "type must be 'bonus' or 'deduction'"}, status=400)
            if not label:
                return Response({"error": "label is required"}, status=400)
            if amount <= 0:
                return Response({"error": "amount must be > 0"}, status=400)

            adjustments = list(record.one_time_adjustments or [])
            adjustments.append({"type": adj_type, "label": label, "amount": amount})
            record.one_time_adjustments = adjustments
            record.save(update_fields=['one_time_adjustments'])

            PayrollAuditLog.objects.create(
                tenant=request.user.tenant,
                payroll_record=record,
                action=f"Adjustment Added: {adj_type.title()} — {label} — ₹{amount:,.2f}",
                performed_by=request.user,
            )
            return Response({
                "message": "Adjustment saved.",
                "adjustments": record.one_time_adjustments,
                "updated_record": build_payroll_record_payload(request.user.tenant, record)
            })
        except PayrollRecord.DoesNotExist:
            return Response({"error": "Record not found"}, status=404)

    def delete(self, request, record_id):
        """Remove an adjustment by index."""
        try:
            record = PayrollRecord.objects.get(tenant=request.user.tenant, id=record_id)
            if record.status in ['Locked', 'Paid']:
                return Response({"error": f"Cannot modify a {record.status} record."}, status=400)
            idx = int(request.data.get('index', -1))
            adjustments = list(record.one_time_adjustments or [])
            if 0 <= idx < len(adjustments):
                removed = adjustments.pop(idx)
                record.one_time_adjustments = adjustments
                record.save(update_fields=['one_time_adjustments'])
                return Response({
                    "message": f"Removed: {removed['label']}",
                    "adjustments": adjustments,
                    "updated_record": build_payroll_record_payload(request.user.tenant, record)
                })
            return Response({"error": "Invalid index"}, status=400)
        except PayrollRecord.DoesNotExist:
            return Response({"error": "Record not found"}, status=404)


# ─────────────────────────────────────────────
# PAYROLL SETTINGS — GET/PUT
# ─────────────────────────────────────────────
class PayrollSettingView(views.APIView):
    """Get or update the tenant's payroll configuration (PF%, ESI%, tax regime)."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ensure_master_tables_exist()
        tenant = request.user.tenant
        setting, _ = PayrollSetting.objects.get_or_create(
            tenant=tenant,
            defaults={
                'pf_rate_employee': Decimal('12.0'),
                'pf_rate_employer': Decimal('12.0'),
                'esi_rate_employee': Decimal('0.75'),
                'esi_rate_employer': Decimal('3.25'),
                'tax_regime_default': 'New',
                'loan_interest_rate_annual': Decimal('8.5'),
            }
        )
        return Response({
            "pf_rate_employee":  float(setting.pf_rate_employee),
            "pf_rate_employer":  float(setting.pf_rate_employer),
            "esi_rate_employee": float(setting.esi_rate_employee),
            "esi_rate_employer": float(setting.esi_rate_employer),
            "tax_regime_default": setting.tax_regime_default,
            "loan_interest_rate_annual": float(getattr(setting, "loan_interest_rate_annual", Decimal("8.5"))),
        })

    def put(self, request):
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        setting, _ = PayrollSetting.objects.get_or_create(tenant=tenant)
        p = request.data
        if 'pf_rate_employee' in p:
            setting.pf_rate_employee = Decimal(str(p['pf_rate_employee']))
        if 'pf_rate_employer' in p:
            setting.pf_rate_employer = Decimal(str(p['pf_rate_employer']))
        if 'esi_rate_employee' in p:
            setting.esi_rate_employee = Decimal(str(p['esi_rate_employee']))
        if 'esi_rate_employer' in p:
            setting.esi_rate_employer = Decimal(str(p['esi_rate_employer']))
        if 'tax_regime_default' in p:
            setting.tax_regime_default = p['tax_regime_default']
        if 'loan_interest_rate_annual' in p:
            setting.loan_interest_rate_annual = Decimal(str(p['loan_interest_rate_annual']))
        setting.save()
        return Response({"message": "Payroll settings updated successfully"})


# ─────────────────────────────────────────────
# HOLIDAY CALENDAR — CRUD
# ─────────────────────────────────────────────
class HolidayCalendarView(views.APIView):
    """List, create and delete company holidays."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        tenant = request.user.tenant
        year = request.query_params.get('year', str(timezone.localdate().year))
        holidays = HolidayCalendar.objects.filter(
            tenant=tenant, date__year=year
        ).order_by('date')
        data = [
            {
                "id": h.id, "name": h.name,
                "date": h.date.isoformat(),
                "holiday_type": h.holiday_type,
                "description": h.description or "",
            }
            for h in holidays
        ]
        return Response({"holidays": data, "year": year, "total": len(data)})

    def post(self, request):
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        p = request.data
        name = p.get('name', '').strip()
        date_val = p.get('date')
        if not name or not date_val:
            return Response({"error": "name and date are required"}, status=400)
        holiday, created = HolidayCalendar.objects.get_or_create(
            tenant=tenant, date=date_val,
            defaults={
                'name': name,
                'holiday_type': p.get('holiday_type', 'Company'),
                'description': p.get('description', ''),
            }
        )
        if not created:
            holiday.name = name
            holiday.holiday_type = p.get('holiday_type', holiday.holiday_type)
            holiday.description = p.get('description', holiday.description)
            holiday.save()
        return Response({"message": "Holiday saved", "id": holiday.id}, status=201)

    def delete(self, request):
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        holiday_id = request.data.get('id') or request.query_params.get('id')
        HolidayCalendar.objects.filter(tenant=tenant, id=holiday_id).delete()
        return Response({"message": "Holiday deleted"})


# ─────────────────────────────────────────────
# LEAVE TYPES — GET/POST (settings page)
# ─────────────────────────────────────────────
class LeaveTypeMasterView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        tenant = request.user.tenant
        leave_types = LeaveType.objects.filter(tenant=tenant)
        data = [
            {
                "id": lt.id, "name": lt.name, "code": lt.code,
                "days_per_year": lt.days_per_year, "is_paid": lt.is_paid,
                "carry_forward": lt.carry_forward,
                "max_carry_forward": lt.max_carry_forward,
            }
            for lt in leave_types
        ]
        return Response({"leave_types": data})

    def post(self, request):
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        p = request.data
        lt = LeaveType.objects.create(
            tenant=tenant,
            name=p.get('name'),
            code=p.get('code', 'CL').upper(),
            days_per_year=p.get('days_per_year', 12),
            is_paid=p.get('is_paid', True),
            carry_forward=p.get('carry_forward', False),
            max_carry_forward=p.get('max_carry_forward', 0),
        )
        return Response({"message": "Leave type created", "id": lt.id}, status=201)


# ─────────────────────────────────────────────
# LEAVE BALANCE — per employee
# ─────────────────────────────────────────────
class LeaveBalanceView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        tenant = request.user.tenant
        employee_id = request.query_params.get('employee_id')
        year = request.query_params.get('year', str(timezone.localdate().year))
        qs = LeaveBalance.objects.filter(tenant=tenant, year=year).select_related('leave_type', 'employee')
        if employee_id:
            qs = qs.filter(employee_id=employee_id)
        data = [
            {
                "id": lb.id,
                "employee_id": str(lb.employee_id),
                "employee_name": lb.employee.name,
                "leave_type": lb.leave_type.name,
                "leave_code": lb.leave_type.code,
                "allocated": float(lb.allocated),
                "used": float(lb.used),
                "carried_forward": float(lb.carried_forward),
                "remaining": float(lb.remaining),
            }
            for lb in qs
        ]
        return Response({"balances": data, "year": year})


# ─────────────────────────────────────────────
# SEED DEFAULTS — Manual trigger
# ─────────────────────────────────────────────
class SeedDefaultsView(views.APIView):
    """Admin can manually re-seed defaults (safe: uses get_or_create)."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN']:
            return Response({"error": "Permission denied"}, status=403)
        try:
            seed_tenant_defaults(request.user.tenant)
            return Response({"message": "Master data seeded successfully for your tenant."})
        except Exception as e:
            return Response({"error": str(e)}, status=500)


class AdminSetupWizardView(views.APIView):
    """
    ONE-CLICK SETUP:
    1. Provisions missing tables (t_role_permission, m_role, etc).
    2. Seeds tenant defaults (Attendance, Leaves, Payroll).
    3. Seeds Role Catalog & Permissions.
    4. Ensures current user has an 'ADMIN' role link.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        tenant = request.user.tenant
        user = request.user

        try:
            # 1. Provision Tables
            ensure_role_permission_table_exists()
            ensure_master_tables_exist()

            # 2. Seed Tenant Defaults (Attendance/Leave/Payroll)
            seed_tenant_defaults(tenant)

            # 3. Seed Role Catalog & Route Permissions
            # This creates 'Admin', 'HR Manager', etc. in t_role
            role_stats = AdminSeedRolePermissionsView.sync_catalog_for_tenant(tenant)

            # 4. Ensure Current User has Admin Link
            admin_role = Role.objects.filter(tenant=tenant, system_role_category='ADMIN').first()
            if admin_role:
                if user.role != admin_role:
                    user.role = admin_role
                    user.save(update_fields=['role'])
            
            # 5. Mark Onboarding as complete (Optional)
            tenant.onboarding_step = 5
            tenant.save(update_fields=['onboarding_step'])

            return Response({
                "status": "success",
                "message": "HRMS Setup Wizard completed successfully.",
                "details": {
                    "tenant": tenant.name,
                    "user": user.username,
                    "assigned_role": admin_role.name if admin_role else "None",
                    "role_stats": role_stats
                }
            })
        except Exception as e:
            return Response({"status": "error", "message": str(e)}, status=500)


# ─────────────────────────────────────────────
# ADMIN PORTAL PERMISSIONS (DB-driven via t_role)
# ─────────────────────────────────────────────
class AdminPermissionsView(views.APIView):
    """
    Returns effective admin portal permissions for current user.
    Resolution order:
    - SUPER_ADMIN/ADMIN => full access
    - If user has employee_profile.designation and role permission exists => use t_role_permission.allowed_routes
    - Else => fallback allowlist by t_user.role (DEFAULT_ADMIN_ROUTE_ALLOWLIST_BY_USER_ROLE)
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ensure_role_permission_table_exists()

        # Use the canonical system_role property (derived from t_user.role FK + is_superuser)
        system_role = request.user.system_role  # 'SUPER_ADMIN' | 'ADMIN' | 'HR' | 'MANAGER' | 'EMPLOYEE'

        if system_role in ('SUPER_ADMIN', 'ADMIN'):
            return Response({
                "full_access": True,
                "source": "system_role",
                "system_role": system_role,
                "allowed_routes": ["*"],
            })

        # Resolve DB-driven permission from employee designation -> t_role_permission
        employee = getattr(request.user, 'employee_profile', None)
        designation = getattr(employee, 'designation', None) if employee else None

        if designation:
            rp = RolePermission.objects.filter(role_id=designation.id).first()
            if rp:
                allowed_routes = rp.allowed_routes or []
                return Response({
                    "full_access": False,
                    "source": "t_role_permission",
                    "system_role": system_role,
                    "role_id": designation.id,
                    "role_name": designation.name,
                    "allowed_routes": allowed_routes,
                })

        # Fallback: use system_role to pick a conservative allowlist
        fallback = DEFAULT_ADMIN_ROUTE_ALLOWLIST_BY_USER_ROLE.get(system_role, [])
        return Response({
            "full_access": False,
            "source": "fallback",
            "system_role": system_role,
            "allowed_routes": fallback,
        })


class MetaRolesMenusView(views.APIView):
    """Shared metadata for admin screens: user roles and admin menu keys."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if str(request.user.system_role).upper() not in ('SUPER_ADMIN', 'ADMIN', 'HR', 'MANAGER'):
            return Response({"error": "Permission denied"}, status=403)
        return Response({
            "user_role_options": get_allowed_user_roles(),
            "admin_route_keys": ADMIN_ROUTE_KEYS,
        })


class AdminRolePermissionListView(views.APIView):
    """List all tenant roles with their configured admin routes."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ensure_role_permission_table_exists()

        if str(request.user.system_role).upper() not in ('SUPER_ADMIN', 'ADMIN', 'HR'):
            return Response({"error": "Permission denied"}, status=403)

        tenant = request.user.tenant
        roles = Role.objects.filter(tenant=tenant).order_by('level', 'name')

        role_ids = [r.id for r in roles]
        perms = {p.role_id: (p.allowed_routes or []) for p in RolePermission.objects.filter(role_id__in=role_ids)}

        data = [
            {
                "role_id": r.id,
                "role_name": r.name,
                "level": r.level,
                "system_role_category": r.system_role_category or 'EMPLOYEE',
                "allowed_routes": perms.get(r.id, []),
            }
            for r in roles
        ]
        return Response({
            "roles": data,
            "admin_route_keys": ADMIN_ROUTE_KEYS,
            "user_role_options": get_allowed_user_roles(),
        })


class AdminRolePermissionUpdateView(views.APIView):
    """Update routes for a given tenant role."""
    permission_classes = [permissions.IsAuthenticated]

    def put(self, request, role_id: int):
        ensure_role_permission_table_exists()

        if str(request.user.system_role).upper() not in ('SUPER_ADMIN', 'ADMIN'):
            return Response({"error": "Permission denied"}, status=403)

        tenant = request.user.tenant
        role = Role.objects.filter(id=role_id, tenant=tenant).first()
        if not role:
            return Response({"error": "Role not found"}, status=404)

        allowed_routes = request.data.get('allowed_routes', [])
        if allowed_routes is None:
            allowed_routes = []
        if not isinstance(allowed_routes, list) or not all(isinstance(x, str) for x in allowed_routes):
            return Response({"error": "allowed_routes must be a list of strings"}, status=400)

        # Optional: update the DB-driven system role category
        valid_cats = {'SUPER_ADMIN', 'ADMIN', 'HR', 'MANAGER', 'EMPLOYEE'}
        new_category = str(request.data.get('system_role_category') or '').strip().upper()
        if new_category in valid_cats and role.system_role_category != new_category:
            role.system_role_category = new_category
            role.save(update_fields=['system_role_category'])

        rp = RolePermission.objects.filter(role_id=role.id).first()
        if rp:
            rp.allowed_routes = allowed_routes
            rp.save(update_fields=['allowed_routes', 'updated_at'])
        else:
            RolePermission.objects.create(role=role, allowed_routes=allowed_routes)

        return Response({
            "role_id": role.id,
            "role_name": role.name,
            "system_role_category": role.system_role_category,
            "allowed_routes": allowed_routes,
        })


class AdminSeedRolePermissionsView(views.APIView):
    """
    Seeds tenant `t_role` with a role catalog and assigns default admin routes.

    POST body (optional):
      { "roles": ["HR Executive", "Payroll Executive", ...] }

    If not provided, uses a built-in catalog (safe to call multiple times).
    """
    permission_classes = [permissions.IsAuthenticated]

    ROLE_CATALOG = [
        # Engineering / Tech
        "Intern / Trainee",
        "Junior Software Engineer",
        "Software Engineer",
        "Senior Software Engineer",
        "Lead Developer",
        "Technical Architect",
        "Solution Architect",
        "DevOps Engineer",
        "QA Engineer",
        "Senior QA Engineer",
        "UI/UX Designer",
        "Full Stack Developer",
        # HR
        "HR Intern",
        "HR Executive",
        "Senior HR Executive",
        "HR Manager",
        "Talent Acquisition Specialist",
        "Recruiter",
        # Admin / Office
        "Admin Executive",
        "Office Manager",
        # Finance
        "Accounts Executive",
        "Senior Accountant",
        "Payroll Executive",
        "Finance Analyst",
        "Finance Manager",
        "Auditor",
        "Tax Consultant",
        # Operations / Plant / Quality
        "Operations Executive",
        "Operations Manager",
        "Production Supervisor",
        "Plant Manager",
        "Quality Inspector",
        "Supply Chain Executive",
        # Sales / BD / Marketing
        "Sales Executive",
        "Senior Sales Executive",
        "Business Development Executive",
        "Business Development Manager",
        "Marketing Executive",
        "Digital Marketing Specialist",
        "Brand Manager",
        # Management
        "Team Lead",
        "Project Manager",
        "Program Manager",
        "Delivery Manager",
        "General Manager",
        "Director",
        "Vice President",
        # C-suite
        "Chief Executive Officer",
        "Chief Technology Officer",
        "Chief Financial Officer",
        # Support / backoffice
        "Customer Support Executive",
        "Technical Support Engineer",
        "Helpdesk Executive",
        "Data Entry Operator",
        "Office Assistant",
    ]

    @classmethod
    def sync_catalog_for_tenant(cls, tenant, incoming_role_names=None):
        """
        Ensures ROLE_CATALOG (or incoming list) exists in tenant t_role plus default RolePermission rows.
        Safe and idempotent: may be called when onboarding/data finds no roles.
        """
        ensure_role_permission_table_exists()
        if incoming_role_names is None:
            role_names_src = cls.ROLE_CATALOG
        else:
            role_names_src = incoming_role_names
        role_names = [str(x).strip() for x in role_names_src if str(x).strip()]

        created_roles = 0
        created_perms = 0
        updated_perms = 0

        for name in role_names:
            derived_category = cls._derive_category(name)

            role_obj, created = Role.objects.get_or_create(
                tenant=tenant,
                name=name,
                defaults={'level': 1, 'system_role_category': derived_category}
            )
            if created:
                created_roles += 1
            elif role_obj.system_role_category != derived_category:
                role_obj.system_role_category = derived_category
                role_obj.save(update_fields=['system_role_category'])

            allowed = default_allowed_routes_for_role_name(name)

            rp = RolePermission.objects.filter(role_id=role_obj.id).first()
            if rp:
                rp.allowed_routes = allowed
                rp.save(update_fields=['allowed_routes', 'updated_at'])
                updated_perms += 1
            else:
                RolePermission.objects.create(role=role_obj, allowed_routes=allowed)
                created_perms += 1

        return {
            "tenant_id": str(getattr(tenant, 'id', '')),
            "created_roles": created_roles,
            "created_permissions": created_perms,
            "updated_permissions": updated_perms,
            "total_roles_processed": len(role_names),
        }

    def post(self, request):
        if str(request.user.system_role).upper() not in ('SUPER_ADMIN', 'ADMIN', 'HR', 'MANAGER'):
            return Response({"error": "Permission denied"}, status=403)

        tenant = request.user.tenant
        incoming = request.data.get('roles', None)
        role_names_payload = incoming if isinstance(incoming, list) else None
        stats = self.sync_catalog_for_tenant(tenant, incoming_role_names=role_names_payload)

        return Response({
            "message": "Seeded roles and default admin permissions",
            "admin_route_keys": ADMIN_ROUTE_KEYS,
            **stats,
        })

    @staticmethod
    def _derive_category(role_name: str) -> str:
        """
        Derives the canonical system_role_category for a given role name.
        This runs ONCE at seed/create time and the result is stored in
        t_role.system_role_category, making all subsequent lookups DB-driven.
        """
        name = (role_name or '').strip().upper()
        if 'SUPER' in name and 'ADMIN' in name:
            return 'SUPER_ADMIN'
        if 'ADMIN' in name:
            return 'ADMIN'
        if any(k in name for k in ['HR MANAGER', 'HR EXECUTIVE', 'HR INTERN', 'TALENT ACQUISITION', 'RECRUITER', 'HR']):
            return 'HR'
        if any(k in name for k in [
            'MANAGER', 'LEAD', 'DIRECTOR', 'VICE PRESIDENT', 'VP',
            'GENERAL MANAGER', 'CEO', 'CTO', 'CFO',
            'CHIEF EXECUTIVE', 'CHIEF TECHNOLOGY', 'CHIEF FINANCIAL',
            'DELIVERY MANAGER', 'PROGRAM MANAGER', 'PROJECT MANAGER',
            'TECHNICAL ARCHITECT', 'SOLUTION ARCHITECT', 'PLANT MANAGER',
        ]):
            return 'MANAGER'
        return 'EMPLOYEE'

# ─────────────────────────────────────────────
# ATTENDANCE CSV EXPORT
# ─────────────────────────────────────────────
class AttendanceExportView(views.APIView):
    """Export attendance records as CSV for a given month."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        import csv
        from django.http import HttpResponse

        tenant = request.user.tenant
        month = request.query_params.get('month', timezone.localdate().strftime('%Y-%m'))

        records = AttendanceRecord.objects.filter(
            tenant=tenant,
            date__startswith=month
        ).select_related('employee__department', 'status').order_by('employee__name', 'date')

        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="attendance_{month}.csv"'
        response['Access-Control-Expose-Headers'] = 'Content-Disposition'

        writer = csv.writer(response)
        writer.writerow([
            'Employee Code', 'Employee Name', 'Department',
            'Date', 'Status', 'Check In', 'Check Out', 'Work Hours'
        ])
        for r in records:
            writer.writerow([
                r.employee.employee_code or '',
                r.employee.name,
                r.employee.department.name if r.employee.department else '',
                r.date.isoformat(),
                r.status.code if r.status else r.status_str,
                r.check_in.strftime('%H:%M') if r.check_in else '',
                r.check_out.strftime('%H:%M') if r.check_out else '',
                round(r.work_hours, 2),
            ])
        return response


# ── Master Data Views (Public — used during onboarding setup) ─────────────────

class MasterIndustryView(views.APIView):
    """GET /api/master/industries/ — Returns the full list of industry types."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ensure_master_tables_exist()
        industries = IndustryMaster.objects.all().order_by('name')
        data = [{'id': i.id, 'name': i.name} for i in industries]
        return Response(data)


class MasterDepartmentView(views.APIView):
    """GET /api/master/departments/?industry_id=<id> — Departments for a given industry."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ensure_master_tables_exist()
        industry_id = request.query_params.get('industry_id')
        qs = DepartmentMaster.objects.filter(is_active=True)
        # Include shared masters (merged catalog) where industry_id is NULL.
        if industry_id:
            qs = qs.filter(Q(industry_id=industry_id) | Q(industry_id__isnull=True))
        qs = qs.order_by('name')
        data = [
            {'id': d.id, 'name': d.name, 'code': d.code, 'industry_id': d.industry_id}
            for d in qs
        ]
        return Response(data)


class MasterRoleView(views.APIView):
    """GET /api/master/roles/?department_id=<id> — Roles for a given department."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ensure_master_tables_exist()
        department_id = request.query_params.get('department_id')
        qs = RoleMaster.objects.filter(is_active=True)
        if department_id:
            qs = qs.filter(department_id=department_id)
        qs = qs.order_by('level', 'name')
        data = [
            {'id': r.id, 'name': r.name, 'level': r.level, 'category': r.category, 'department_id': r.department_id}
            for r in qs
        ]
        return Response(data)


class UserRoleUpdateView(views.APIView):
    """
    POST /api/admin/user-role-update/
    Allows an Admin to change another user's role.
    Payload: { "user_id": 123, "role_id": 456 }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if str(request.user.system_role).upper() not in ('SUPER_ADMIN', 'ADMIN'):
            return Response({"error": "Only Admins can change user roles"}, status=403)

        user_id = request.data.get('user_id')
        role_id = request.data.get('role_id')
        tenant = request.user.tenant

        if not user_id or not role_id:
            return Response({"error": "user_id and role_id are required"}, status=400)

        target_user = User.objects.filter(id=user_id, tenant=tenant).first()
        if not target_user:
            return Response({"error": "User not found in your tenant"}, status=404)

        new_role = Role.objects.filter(id=role_id, tenant=tenant).first()
        if not new_role:
            return Response({"error": "Role not found in your tenant"}, status=404)

        target_user.role = new_role
        target_user.save(update_fields=['role'])

        return Response({
            "message": f"Updated role for {target_user.username} to {new_role.name}",
            "username": target_user.username,
            "new_role": new_role.name,
            "system_role": target_user.system_role
        })
