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
    AttendanceDataView, AttendanceMarkView, AttendanceRegularizeView, AttendanceReportView, AttendanceStatusListView,
    # Payroll
    PayrollDataView, PayrollProcessView, SalaryComponentView, SalaryStructureView, EmployeeSalarySetupView, PayslipView,
    # ESS — Employee Self-Service
    ESSAttendanceTodayView, ESSAttendanceHistoryView,
    ESSProfileView, ESSPayslipsView,
    # Leave
    LeaveTypeView, LeaveApplicationView, LeaveApproveView,
    # Onboarding Flow
    SendOnboardingInviteView, EmployeeOnboardingPublicView,
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
    path('attendance/report/',       AttendanceReportView.as_view(),        name='attendance_report'),
    path('attendance/statuses/',     AttendanceStatusListView.as_view(),    name='attendance_statuses'),

    # ── Payroll ────────────────────────────────────────
    path('payroll/data/',            PayrollDataView.as_view(),             name='payroll_data'),
    path('payroll/process/',         PayrollProcessView.as_view(),          name='payroll_process'),
    path('payroll/components/',      SalaryComponentView.as_view(),         name='salary_components'),
    path('payroll/structures/',      SalaryStructureView.as_view(),         name='salary_structures'),
    path('payroll/setup/',           EmployeeSalarySetupView.as_view(),      name='employee_salary_setup'),
    path('payroll/payslip/<int:record_id>/', PayslipView.as_view(),           name='payroll_payslip_detail'),

    # ── ESS — Employee Self-Service ────────────────────
    path('ess/profile/',             ESSProfileView.as_view(),              name='ess_profile'),
    path('ess/attendance/today/',    ESSAttendanceTodayView.as_view(),      name='ess_attendance_today'),
    path('ess/attendance/history/',  ESSAttendanceHistoryView.as_view(),    name='ess_attendance_history'),
    path('ess/payslips/',            ESSPayslipsView.as_view(),             name='ess_payslips'),

    # ── Leave Management ───────────────────────────────
    path('leave/types/',             LeaveTypeView.as_view(),               name='leave_types'),
    path('leave/apply/',             LeaveApplicationView.as_view(),        name='leave_apply'),
    path('leave/approve/',           LeaveApproveView.as_view(),            name='leave_approve'),

    # ── Onboarding Flow ────────────────────────────────
    path('employees/invite/',        SendOnboardingInviteView.as_view(),    name='hr_employee_invite'),
    path('onboarding/public/<str:token>/', EmployeeOnboardingPublicView.as_view(), name='employee_onboarding_public'),
]