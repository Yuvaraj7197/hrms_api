from django.urls import path
from .views import (
    # Auth
    RegisterView, VerifyOTPView, LoginView, ESSLoginView,
    # Onboarding
    OnboardingSetupView, OnboardingDepartmentsView,
    OnboardingRolesView, OnboardingEmployeesView, OnboardingDataView,
    OnboardingEmployeeCreateView, OnboardingEmployeeDetailView,
    # Dashboard
    DashboardView,
    # HR — Employee Management
    HREmployeeListView, HREmployeeDetailView,
    # Attendance — Admin/HR
    AttendanceDataView, AttendanceMarkView, AttendanceRegularizeView,
    # Payroll
    PayrollDataView, PayrollProcessView,
    # ESS — Employee Self-Service
    ESSAttendanceTodayView, ESSAttendanceHistoryView,
    ESSProfileView, ESSPayslipsView,
    # Leave
    LeaveTypeView, LeaveApplicationView, LeaveApproveView,
)

urlpatterns = [
    # ── Auth ──────────────────────────────────────────
    path('register/',        RegisterView.as_view(),  name='register'),
    path('verify-otp/',      VerifyOTPView.as_view(), name='verify_otp'),
    path('login/',           LoginView.as_view(),     name='login'),         # Admin/HR/Manager
    path('ess/login/',       ESSLoginView.as_view(),  name='ess_login'),     # Employee portal

    # ── Dashboard ─────────────────────────────────────
    path('dashboard/',       DashboardView.as_view(), name='dashboard'),

    # ── Onboarding (Admin Wizard) ──────────────────────
    path('onboarding/setup/',        OnboardingSetupView.as_view(),         name='onboarding_setup'),
    path('onboarding/departments/',  OnboardingDepartmentsView.as_view(),   name='onboarding_departments'),
    path('onboarding/roles/',        OnboardingRolesView.as_view(),         name='onboarding_roles'),
    path('onboarding/employees/',    OnboardingEmployeesView.as_view(),     name='onboarding_employees'),
    path('onboarding/employee/',     OnboardingEmployeeCreateView.as_view(),name='onboarding_employee_create'),
    path('onboarding/employees/<int:employee_id>/', OnboardingEmployeeDetailView.as_view(), name='onboarding_employee_detail'),
    path('onboarding/data/',         OnboardingDataView.as_view(),          name='onboarding_data'),

    # ── HR — Employee Management ───────────────────────
    path('employees/',               HREmployeeListView.as_view(),          name='hr_employees'),
    path('employees/<int:employee_id>/', HREmployeeDetailView.as_view(),    name='hr_employee_detail'),

    # ── Attendance (Admin/HR view + mark/regularize) ───
    path('attendance/data/',         AttendanceDataView.as_view(),          name='attendance_data'),
    path('attendance/mark/',         AttendanceMarkView.as_view(),          name='attendance_mark'),
    path('attendance/regularize/',   AttendanceRegularizeView.as_view(),    name='attendance_regularize'),

    # ── Payroll ────────────────────────────────────────
    path('payroll/data/',            PayrollDataView.as_view(),             name='payroll_data'),
    path('payroll/process/',         PayrollProcessView.as_view(),          name='payroll_process'),

    # ── ESS — Employee Self-Service ────────────────────
    path('ess/profile/',             ESSProfileView.as_view(),              name='ess_profile'),
    path('ess/attendance/today/',    ESSAttendanceTodayView.as_view(),      name='ess_attendance_today'),
    path('ess/attendance/history/',  ESSAttendanceHistoryView.as_view(),    name='ess_attendance_history'),
    path('ess/payslips/',            ESSPayslipsView.as_view(),             name='ess_payslips'),

    # ── Leave Management ───────────────────────────────
    path('leave/types/',             LeaveTypeView.as_view(),               name='leave_types'),
    path('leave/apply/',             LeaveApplicationView.as_view(),        name='leave_apply'),
    path('leave/approve/',           LeaveApproveView.as_view(),            name='leave_approve'),
]