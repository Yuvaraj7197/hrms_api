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
    AttendanceDataView, AttendanceMarkView, AttendanceRegularizeView, AttendanceReportView,
    AttendanceStatusListView, AttendanceExportView,
    # Payroll
    PayrollDataView, PayrollProcessView, SalaryComponentView, SalaryStructureView,
    EmployeeSalarySetupView, PayslipView, PayrollAdjustmentView,
    PayrollSettingView,
    PayrollAuditLogsView, PayrollTaxVerifyView, PayrollForm16DownloadView,
    # ESS — Employee Self-Service
    ESSAttendanceTodayView, ESSAttendanceHistoryView,
    ESSProfileView, ESSPayslipsView,
    # Leave
    LeaveTypeView, LeaveApplicationView, LeaveApproveView,
    LeaveTypeMasterView, LeaveBalanceView,
    # Holidays
    HolidayCalendarView,
    # Onboarding Flow
    SendOnboardingInviteView, EmployeeOnboardingPublicView,
    # Seed defaults
    SeedDefaultsView,
    # Master Data
    MasterIndustryView, MasterDepartmentView, MasterRoleView,
)

urlpatterns = [
    # ── Auth ──────────────────────────────────────────
    path('register/',        RegisterView.as_view(),  name='register'),
    path('verify-otp/',      VerifyOTPView.as_view(), name='verify_otp'),
    path('login/',           LoginView.as_view(),     name='login'),
    path('ess/login/',       ESSLoginView.as_view(),  name='ess_login'),

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

    # ── Attendance ──────────────────────────────────────
    path('attendance/data/',         AttendanceDataView.as_view(),          name='attendance_data'),
    path('attendance/mark/',         AttendanceMarkView.as_view(),          name='attendance_mark'),
    path('attendance/regularize/',   AttendanceRegularizeView.as_view(),    name='attendance_regularize'),
    path('attendance/report/',       AttendanceReportView.as_view(),        name='attendance_report'),
    path('attendance/statuses/',     AttendanceStatusListView.as_view(),    name='attendance_statuses'),
    path('attendance/export/',       AttendanceExportView.as_view(),        name='attendance_export'),

    # ── Payroll ────────────────────────────────────────
    path('payroll/data/',            PayrollDataView.as_view(),             name='payroll_data'),
    path('payroll/process/',         PayrollProcessView.as_view(),          name='payroll_process'),
    path('payroll/components/',      SalaryComponentView.as_view(),         name='salary_components'),
    path('payroll/structures/',      SalaryStructureView.as_view(),         name='salary_structures'),
    path('payroll/setup/',           EmployeeSalarySetupView.as_view(),     name='employee_salary_setup'),
    path('payroll/payslip/<int:record_id>/', PayslipView.as_view(),         name='payroll_payslip_detail'),
    path('payroll/adjust/<int:record_id>/', PayrollAdjustmentView.as_view(),name='payroll_adjustment'),
    path('payroll/settings/',        PayrollSettingView.as_view(),          name='payroll_settings'),
    path('payroll/audit-logs/',     PayrollAuditLogsView.as_view(),       name='payroll_audit_logs'),
    path('payroll/tax/verify/<int:record_id>/', PayrollTaxVerifyView.as_view(), name='payroll_tax_verify'),
    path('payroll/form16/download/', PayrollForm16DownloadView.as_view(), name='payroll_form16_download'),

    # ── ESS — Employee Self-Service ────────────────────
    path('ess/profile/',             ESSProfileView.as_view(),              name='ess_profile'),
    path('ess/attendance/today/',    ESSAttendanceTodayView.as_view(),      name='ess_attendance_today'),
    path('ess/attendance/history/',  ESSAttendanceHistoryView.as_view(),    name='ess_attendance_history'),
    path('ess/payslips/',            ESSPayslipsView.as_view(),             name='ess_payslips'),

    # ── Leave Management ───────────────────────────────
    path('leave/types/',             LeaveTypeView.as_view(),               name='leave_types'),
    path('leave/apply/',             LeaveApplicationView.as_view(),        name='leave_apply'),
    path('leave/approve/',           LeaveApproveView.as_view(),            name='leave_approve'),
    path('leave/master/',            LeaveTypeMasterView.as_view(),         name='leave_type_master'),
    path('leave/balances/',          LeaveBalanceView.as_view(),            name='leave_balances'),

    # ── Holiday Calendar ───────────────────────────────
    path('holidays/',                HolidayCalendarView.as_view(),         name='holidays'),

    # ── Onboarding Flow ────────────────────────────────
    path('employees/invite/',        SendOnboardingInviteView.as_view(),    name='hr_employee_invite'),
    path('onboarding/public/<str:token>/', EmployeeOnboardingPublicView.as_view(), name='employee_onboarding_public'),

    # ── Admin Utilities ────────────────────────────────
    path('admin/seed-defaults/',     SeedDefaultsView.as_view(),           name='seed_defaults'),

    # ── Master Data ────────────────────────────────────
    path('master/industries/',       MasterIndustryView.as_view(),         name='master_industries'),
    path('master/departments/',      MasterDepartmentView.as_view(),       name='master_departments'),
    path('master/roles/',            MasterRoleView.as_view(),             name='master_roles'),
]