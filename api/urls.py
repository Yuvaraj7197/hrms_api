from django.urls import path
from . import views

urlpatterns = [
    # ── Auth ──────────────────────────────────────────
    path('register/',        views.RegisterView.as_view(),  name='register'),
    path('verify-otp/',      views.VerifyOTPView.as_view(), name='verify_otp'),
    path('login/',           views.LoginView.as_view(),     name='login'),
    path('ess/login/',       views.ESSLoginView.as_view(),  name='ess_login'),

    # ── Dashboard ─────────────────────────────────────
    path('dashboard/',       views.DashboardView.as_view(), name='dashboard'),

    # ── Onboarding (Admin Wizard) ──────────────────────
    path('onboarding/setup/',        views.OnboardingSetupView.as_view(),         name='onboarding_setup'),
    path('onboarding/departments/',  views.OnboardingDepartmentsView.as_view(),   name='onboarding_departments'),
    path('onboarding/roles/',        views.OnboardingRolesView.as_view(),         name='onboarding_roles'),
    path('onboarding/employees/',    views.OnboardingEmployeesView.as_view(),     name='onboarding_employees'),
    path('onboarding/employee/',     views.OnboardingEmployeeCreateView.as_view(),name='onboarding_employee_create'),
    path('onboarding/employees/<int:employee_id>/', views.OnboardingEmployeeDetailView.as_view(), name='onboarding_employee_detail'),
    path('onboarding/data/',         views.OnboardingDataView.as_view(),          name='onboarding_data'),

    # ── HR — Employee Management ───────────────────────
    path('employees/',               views.HREmployeeListView.as_view(),          name='hr_employees'),
    path('employees/<int:employee_id>/', views.HREmployeeDetailView.as_view(),    name='hr_employee_detail'),

    # ── Attendance ──────────────────────────────────────
    path('attendance/data/',         views.AttendanceDataView.as_view(),          name='attendance_data'),
    path('attendance/mark/',         views.AttendanceMarkView.as_view(),          name='attendance_mark'),
    path('attendance/regularize/',   views.AttendanceRegularizeView.as_view(),    name='attendance_regularize'),
    path('attendance/report/',       views.AttendanceReportView.as_view(),        name='attendance_report'),
    path('attendance/statuses/',     views.AttendanceStatusListView.as_view(),    name='attendance_statuses'),
    path('attendance/export/',       views.AttendanceExportView.as_view(),        name='attendance_export'),

    # ── Payroll ────────────────────────────────────────
    path('payroll/data/',            views.PayrollDataView.as_view(),             name='payroll_data'),
    path('payroll/process/',         views.PayrollProcessView.as_view(),          name='payroll_process'),
    path('payroll/components/',      views.SalaryComponentView.as_view(),         name='salary_components'),
    path('payroll/structures/',      views.SalaryStructureView.as_view(),         name='salary_structures'),
    path('payroll/setup/',           views.EmployeeSalarySetupView.as_view(),     name='employee_salary_setup'),
    path('payroll/payslip/<int:record_id>/', views.PayslipView.as_view(),         name='payroll_payslip_detail'),
    path('payroll/adjust/<int:record_id>/', views.PayrollAdjustmentView.as_view(),name='payroll_adjustment'),
    path('payroll/settings/',        views.PayrollSettingView.as_view(),          name='payroll_settings'),
    path('payroll/audit-logs/',     views.PayrollAuditLogsView.as_view(),       name='payroll_audit_logs'),
    path('payroll/tax/verify/<int:record_id>/', views.PayrollTaxVerifyView.as_view(), name='payroll_tax_verify'),
    path('payroll/form16/download/', views.PayrollForm16DownloadView.as_view(), name='payroll_form16_download'),

    # ── ESS — Employee Self-Service ────────────────────
    path('ess/profile/',             views.ESSProfileView.as_view(),              name='ess_profile'),
    path('ess/attendance/today/',    views.ESSAttendanceTodayView.as_view(),      name='ess_attendance_today'),
    path('ess/attendance/history/',  views.ESSAttendanceHistoryView.as_view(),    name='ess_attendance_history'),
    path('ess/payslips/',            views.ESSPayslipsView.as_view(),             name='ess_payslips'),

    # ── Leave Management ───────────────────────────────
    path('leave/types/',             views.LeaveTypeView.as_view(),               name='leave_types'),
    path('leave/apply/',             views.LeaveApplicationView.as_view(),        name='leave_apply'),
    path('leave/approve/',           views.LeaveApproveView.as_view(),            name='leave_approve'),
    path('leave/master/',            views.LeaveTypeMasterView.as_view(),         name='leave_type_master'),
    path('leave/balances/',          views.LeaveBalanceView.as_view(),            name='leave_balances'),
    path('leave/reconcile/',         views.ReconcileBalancesView.as_view(),       name='leave_reconcile'),

    # ── Holiday Calendar ───────────────────────────────
    path('holidays/',                views.HolidayCalendarView.as_view(),         name='holidays'),

    # ── Onboarding Flow ────────────────────────────────
    path('employees/invite/',        views.SendOnboardingInviteView.as_view(),    name='hr_employee_invite'),
    path('onboarding/public/<str:token>/', views.EmployeeOnboardingPublicView.as_view(), name='employee_onboarding_public'),

    # ── Admin Utilities ────────────────────────────────
    path('admin/seed-defaults/',     views.SeedDefaultsView.as_view(),           name='seed_defaults'),
    path('admin/setup-wizard/',      views.AdminSetupWizardView.as_view(),       name='admin_setup_wizard'),
    path('admin/permissions/',      views.AdminPermissionsView.as_view(),       name='admin_permissions'),
    path('admin/user-role-update/', views.UserRoleUpdateView.as_view(),        name='admin_user_role_update'),
    path('meta/roles-menus/',      views.MetaRolesMenusView.as_view(),         name='meta_roles_menus'),
    path('admin/role-permissions/', views.AdminRolePermissionListView.as_view(), name='admin_role_permissions'),
    path('admin/role-permissions/<int:role_id>/', views.AdminRolePermissionUpdateView.as_view(), name='admin_role_permission_update'),
    path('admin/role-permissions/seed/', views.AdminSeedRolePermissionsView.as_view(), name='admin_role_permissions_seed'),

    # ── Master Data ────────────────────────────────────
    path('master/industries/',       views.MasterIndustryView.as_view(),         name='master_industries'),
    path('master/departments/',      views.MasterDepartmentView.as_view(),       name='master_departments'),
    path('master/roles/',            views.MasterRoleView.as_view(),             name='master_roles'),
]