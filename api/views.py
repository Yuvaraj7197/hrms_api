from rest_framework import status, views, permissions
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import authenticate
from django.utils import timezone
from decimal import Decimal
from .models import User, Tenant, OTP, Department, Role, Employee, AttendanceRecord, PayrollRecord, PayrollAuditLog
from .serializers import RegisterSerializer, OTPVerifySerializer, OnboardingSerializer, UserSerializer, DepartmentSerializer, RoleSerializer, EmployeeSerializer, AttendanceRecordSerializer
from django.db import transaction
import random
from rest_framework.permissions import AllowAny
import string

def safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

class RegisterView(views.APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        if serializer.is_valid():
            company_name = serializer.validated_data['company_name']
            username = serializer.validated_data['username']
            email = serializer.validated_data['email']
            password = serializer.validated_data['password']

            if User.objects.filter(email=email).exists():
                return Response({"error": "Email already exists"}, status=status.HTTP_400_BAD_REQUEST)

            # Create Tenant
            tenant = Tenant.objects.create(name=company_name)
            
            # Create User
            user = User.objects.create_user(
                username=username,
                email=email,
                password=password,
                tenant=tenant,
                role='ADMIN'
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

class OnboardingRolesView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        tenant = request.user.tenant
        roles_data = request.data.get('roles', [])
        
        # Clear existing and save new
        Role.objects.filter(tenant=tenant).delete()
        
        for role in roles_data:
            Role.objects.create(
                tenant=tenant,
                name=role.get('name'),
                description=role.get('description'),
                level=role.get('level') or role.get('accessLevel') or 1
            )

        tenant.onboarding_step = 4
        tenant.save(update_fields=['onboarding_step'])

        return Response({"message": "Roles saved successfully"})

class OnboardingEmployeesView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        tenant = request.user.tenant
        employees_data = request.data.get('employees', [])
        
        # We handle employee creation and hierarchy
        # 1. Create employees and their User accounts first
        for emp in employees_data:
            dept_name = emp.get('department') or emp.get('departmentId')
            role_name = emp.get('role') or emp.get('roleId')
            email = emp.get('email')
            password = emp.get('password')
            
            dept = Department.objects.filter(tenant=tenant, name=dept_name).first()
            role = Role.objects.filter(tenant=tenant, name=role_name).first()
            
            # Create/Update User account if password is provided
            user = None
            if email and password:
                username = email.split('@')[0] + "_" + str(random.randint(100, 999))
                user, created = User.objects.get_or_create(
                    email=email,
                    defaults={
                        'username': username,
                        'tenant': tenant,
                        'role': 'EMPLOYEE',
                        'is_verified': True
                    }
                )
                user.set_password(password)
                user.save()

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
        
        # 2. Setup Reporting Hierarchy
        for emp in employees_data:
            manager_email = emp.get('reportingTo') or emp.get('reporting_to')
            if manager_email:
                manager = Employee.objects.filter(tenant=tenant, email=manager_email).first()
                if manager:
                    Employee.objects.filter(tenant=tenant, email=emp.get('email')).update(reporting_to=manager)

        tenant.onboarding_step = 5
        tenant.save(update_fields=['onboarding_step'])

        return Response({"message": "Employees and User accounts created successfully"})

class OnboardingSetupView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        tenant = request.user.tenant
        payload = request.data.copy()
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
        
        for dept in departments_data:
            Department.objects.create(
                tenant=tenant,
                name=dept.get('name'),
                head_count=dept.get('headCount', 0)
            )
        
        tenant.onboarding_step = 3
        tenant.save(update_fields=['onboarding_step'])
        
        return Response({"message": "Departments saved successfully"})

class DashboardView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        tenant = request.user.tenant
        role = request.user.role
        
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
        tenant = request.user.tenant

        departments = list(
            Department.objects.filter(tenant=tenant).values('id', 'name', 'head_count')
        )
        roles = list(
            Role.objects.filter(tenant=tenant).values('id', 'name', 'description', 'level')
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
                # 'phone': employee.phone or '',
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
            'employees': employees
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
        tenant = request.user.tenant
        employee = Employee.objects.filter(tenant=tenant, id=employee_id).first()
        if not employee:
            return Response({"error": "Employee not found"}, status=status.HTTP_404_NOT_FOUND)

        payload = request.data

        department_id = payload.get('departmentId') or payload.get('department_id')
        role_id = payload.get('roleId') or payload.get('role_id')
        reporting_to_id = payload.get('reportingTo') or payload.get('reporting_to')

        employee.name = payload.get('name', employee.name)
        employee.email = payload.get('email', employee.email)
        # employee.phone = payload.get('phone', employee.phone)
        employee.employee_code = payload.get('employeeCode') or payload.get('employee_code') or employee.employee_code
        employee.status = payload.get('status', employee.status)
        employee.joining_date = payload.get('joiningDate') or payload.get('joining_date') or employee.joining_date
        employee.department = Department.objects.filter(tenant=tenant, id=safe_int(department_id)).first() if department_id else None
        employee.designation = Role.objects.filter(tenant=tenant, id=safe_int(role_id)).first() if role_id else None
        employee.reporting_to = Employee.objects.filter(tenant=tenant, id=safe_int(reporting_to_id)).first() if reporting_to_id else None
        employee.save()

        return Response({"message": "Employee updated successfully"})

    def delete(self, request, employee_id):
        tenant = request.user.tenant
        employee = Employee.objects.filter(tenant=tenant, id=employee_id).first()
        if not employee:
            return Response({"error": "Employee not found"}, status=status.HTTP_404_NOT_FOUND)

        employee.delete()
        return Response({"message": "Employee deleted successfully"})

class AttendanceDataView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        tenant = request.user.tenant
        target_date = request.query_params.get('date') or timezone.localdate().isoformat()

        employees = Employee.objects.filter(tenant=tenant).select_related('department')
        for idx, employee in enumerate(employees):
            default_status = 'Absent' if idx % 5 == 0 else ('Late' if idx % 3 == 0 else 'Present')
            default_check_in = '09:45:00' if default_status == 'Late' else '09:00:00'
            default_check_out = '18:00:00' if default_status != 'Absent' else None
            default_work_hours = 0 if default_status == 'Absent' else (8.25 if default_status == 'Late' else 9.0)

            AttendanceRecord.objects.get_or_create(
                tenant=tenant,
                employee=employee,
                date=target_date,
                defaults={
                    'check_in': default_check_in,
                    'check_out': default_check_out,
                    'status': default_status,
                    'work_hours': default_work_hours,
                    'location': 'Remote/Office',
                }
            )

        records = AttendanceRecord.objects.filter(tenant=tenant, date=target_date).select_related('employee__department')
        response_data = [
            {
                'id': str(record.id),
                'employeeId': str(record.employee_id),
                'employeeName': record.employee.name,
                'employeeCode': record.employee.employee_code or '',
                'departmentName': record.employee.department.name if record.employee.department else 'N/A',
                'date': record.date.isoformat(),
                'checkIn': record.check_in.strftime('%H:%M') if record.check_in else '',
                'checkOut': record.check_out.strftime('%H:%M') if record.check_out else '',
                'status': record.status,
                'workHours': float(record.work_hours),
                'location': record.location,
            }
            for record in records
        ]

        return Response({'records': response_data})

class PayrollDataView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        tenant = request.user.tenant
        cycle_month = request.query_params.get('cycle') or timezone.localdate().strftime('%Y-%m')

        employees = Employee.objects.filter(tenant=tenant).select_related('department')
        for employee in employees:
            base_salary = employee.base_salary if employee.base_salary else Decimal('0')
            PayrollRecord.objects.get_or_create(
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

        records = PayrollRecord.objects.filter(tenant=tenant, cycle_month=cycle_month).select_related('employee__department')
        response_data = [
            {
                'id': str(record.id),
                'employeeCode': record.employee.employee_code or '',
                'name': record.employee.name,
                'departmentName': record.employee.department.name if record.employee.department else 'N/A',
                'baseSalary': float(record.base_salary),
                'allowances': float(record.allowances),
                'deductions': float(record.deductions),
                'loanEMI': float(record.loan_emi),
                'taxStatus': record.tax_status,
                'netPay': float(record.net_pay),
                'status': record.status,
            }
            for record in records
        ]
        return Response({'records': response_data, 'cycle': cycle_month})

class PayrollProcessView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        tenant = request.user.tenant
        action = request.data.get('action', 'process')
        cycle_month = request.data.get('cycle') or timezone.localdate().strftime('%Y-%m')

        records = PayrollRecord.objects.filter(tenant=tenant, cycle_month=cycle_month)
        if action == 'pay':
            updated = records.filter(status='Processed').update(status='Paid')
            return Response({'message': 'Payroll payment completed', 'updated': updated})

        updated = 0
        for record in records:
            if record.status == 'Pending':
                base_salary = Decimal(record.base_salary)
                itax = base_salary * (Decimal('0.20') if base_salary > 100000 else (Decimal('0.10') if base_salary > 50000 else Decimal('0.05')))
                pf = base_salary * Decimal('0.12')
                ptax = Decimal('200') if base_salary > 15000 else Decimal('0')
                deductions = (itax + pf + ptax).quantize(Decimal('0.01'))
                loan_interest = (Decimal(record.loan_emi) * Decimal('0.085') / Decimal('12')).quantize(Decimal('0.01'))
                record.deductions = deductions
                record.net_pay = (Decimal(record.base_salary) + Decimal(record.allowances) - deductions - Decimal(record.loan_emi) - loan_interest).quantize(Decimal('0.01'))
                record.status = 'Processed'
                record.save(update_fields=['deductions', 'net_pay', 'status'])
                updated += 1

        return Response({'message': 'Payroll processing completed', 'updated': updated})

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
            if user.role not in ['SUPER_ADMIN', 'ADMIN', 'HR', 'MANAGER']:
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
            employee = Employee.objects.get(tenant=tenant, id=employee_id)
            record, created = AttendanceRecord.objects.get_or_create(
                tenant=tenant, 
                employee=employee, 
                date=today,
                defaults={'status': 'Present', 'location': request.data.get('location', 'Office')}
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
        record_id = request.data.get('record_id')
        check_in = request.data.get('check_in')
        check_out = request.data.get('check_out')
        status_val = request.data.get('status')

        try:
            record = AttendanceRecord.objects.get(tenant=tenant, id=record_id)
            if check_in: record.check_in = check_in
            if check_out: record.check_out = check_out
            if status_val: record.status = status_val
            record.save()
            return Response({"message": "Attendance regularized successfully"})
        except AttendanceRecord.DoesNotExist:
            return Response({"error": "Record not found"}, status=404)

class PayrollProcessView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        tenant = request.user.tenant
        employee_id = request.data.get('employee_id')
        cycle = request.data.get('cycle_month')
        
        try:
            employee = Employee.objects.get(tenant=tenant, id=employee_id)
            record, created = PayrollRecord.objects.update_or_create(
                tenant=tenant,
                employee=employee,
                cycle_month=cycle,
                defaults={
                    'base_salary': request.data.get('base_salary', 0),
                    'allowances': request.data.get('allowances', 0),
                    'deductions': request.data.get('deductions', 0),
                    'status': 'Processed'
                }
            )
            
            # Create Audit Log
            PayrollAuditLog.objects.create(
                tenant=tenant,
                payroll_record=record,
                action="Payroll Processed/Updated",
                performed_by=request.user,
                notes=request.data.get('notes', 'Automated processing')
            )

            return Response({"message": "Payroll processed and logged successfully", "record_id": record.id})
        except Employee.DoesNotExist:
            return Response({"error": "Employee not found"}, status=404)


# ─────────────────────────────────────────────
# ROLE PERMISSION HELPER
# ─────────────────────────────────────────────
def require_roles(*allowed_roles):
    """Returns 403 if the user's role is not in allowed_roles."""
    def decorator(view_func):
        def wrapper(self, request, *args, **kwargs):
            if request.user.role not in allowed_roles:
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
        if request.user.role not in ['ADMIN', 'SUPER_ADMIN', 'HR', 'MANAGER']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        qs = Employee.objects.filter(tenant=tenant).select_related('department', 'designation', 'reporting_to')
        # MANAGER sees only their department
        if request.user.role == 'MANAGER':
            try:
                mgr_emp = request.user.employee_profile
                qs = qs.filter(department=mgr_emp.department)
            except Exception:
                qs = qs.none()
        data = [
            {
                "id": e.id,
                "employee_code": e.employee_code or "",
                "name": e.name,
                "email": e.email,
                "phone": e.phone or "",
                "department_id": e.department_id,
                "department_name": e.department.name if e.department else "",
                "designation_id": e.designation_id,
                "designation_name": e.designation.name if e.designation else "",
                "reporting_to_id": e.reporting_to_id,
                "reporting_to_name": e.reporting_to.name if e.reporting_to else "",
                "joining_date": str(e.joining_date) if e.joining_date else "",
                "status": e.status,
                "invite_sent": getattr(e, 'invite_sent', False),
                "base_salary": float(e.base_salary) if e.base_salary else 0,
            }
            for e in qs
        ]
        return Response({"employees": data, "total": len(data)})

    def post(self, request):
        """HR/Admin creates a new employee and optionally creates a User account."""
        if request.user.role not in ['ADMIN', 'SUPER_ADMIN', 'HR','MANAGER']:
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
                        'role': 'EMPLOYEE',
                        'is_verified': True
                    }
                )
                
                # Update password if newly created or if admin explicitly provided one
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
        if request.user.role not in ['ADMIN', 'SUPER_ADMIN', 'HR', 'MANAGER']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        try:
            e = Employee.objects.select_related('department', 'designation', 'reporting_to').get(tenant=tenant, id=employee_id)
        except Employee.DoesNotExist:
            return Response({"error": "Employee not found"}, status=404)
        return Response({
            "id": e.id, "employee_code": e.employee_code, "name": e.name,
            "email": e.email, "phone": e.phone or "", "status": e.status,
            "department_id": e.department_id, "department_name": e.department.name if e.department else "",
            "designation_id": e.designation_id, "designation_name": e.designation.name if e.designation else "",
            "reporting_to_id": e.reporting_to_id, "joining_date": str(e.joining_date) if e.joining_date else "",
            "base_salary": float(e.base_salary) if e.base_salary else 0,
        })

    def put(self, request, employee_id):
        if request.user.role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        try:
            e = Employee.objects.get(tenant=tenant, id=employee_id)
        except Employee.DoesNotExist:
            return Response({"error": "Not found"}, status=404)

        p = request.data
        e.name = p.get('name', e.name)
        e.phone = p.get('phone', e.phone)
        e.status = p.get('status', e.status)
        e.joining_date = p.get('joining_date', e.joining_date)
        if 'base_salary' in p:
            e.base_salary = p['base_salary']
        if p.get('department_id'):
            e.department = Department.objects.filter(tenant=tenant, id=safe_int(p['department_id'])).first()
        if p.get('designation_id'):
            e.designation = Role.objects.filter(tenant=tenant, id=safe_int(p['designation_id'])).first()
        if p.get('reporting_to_id'):
            e.reporting_to = Employee.objects.filter(tenant=tenant, id=safe_int(p['reporting_to_id'])).first()
        e.save()
        return Response({"message": "Employee updated"})

    def delete(self, request, employee_id):
        if request.user.role not in ['ADMIN', 'SUPER_ADMIN']:
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
                "status": record.status,
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
            defaults={'status': 'Present', 'location': request.data.get('location', 'Office')}
        )

        if action == 'in':
            if record.check_in:
                return Response({"error": "Already checked in"}, status=400)
            record.check_in = time_now
            record.status = 'Present'
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
            qs = qs.filter(date__startswith=month)
        else:
            qs = qs.order_by('-date')[:30]

        data = [
            {
                "date": str(r.date),
                "check_in": r.check_in.strftime('%H:%M') if r.check_in else None,
                "check_out": r.check_out.strftime('%H:%M') if r.check_out else None,
                "status": r.status,
                "work_hours": float(r.work_hours),
                "location": r.location,
            }
            for r in qs.order_by('date')
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
                "cycle_month": r.cycle_month,
                "base_salary": float(r.base_salary),
                "allowances": float(r.allowances),
                "deductions": float(r.deductions),
                "loan_emi": float(r.loan_emi),
                "net_pay": float(r.net_pay),
                "status": r.status,
                "tax_status": r.tax_status,
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
        if request.user.role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
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
        if request.user.role in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            qs = LeaveApplication.objects.filter(tenant=tenant).select_related('employee', 'leave_type')
        elif request.user.role == 'MANAGER':
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
        if request.user.role not in ['ADMIN', 'SUPER_ADMIN', 'HR', 'MANAGER']:
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
