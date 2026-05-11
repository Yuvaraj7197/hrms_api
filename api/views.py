from rest_framework import status, views, permissions
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import authenticate
from django.utils import timezone
from decimal import Decimal
from datetime import datetime, timedelta
from .models import (
    User, Tenant, OTP, Department, Role, Employee, AttendanceRecord, PayrollRecord, 
    PayrollAuditLog, EmployeeDocument, AttendanceStatus, SalaryComponent, 
    SalaryStructure, SalaryStructureComponent, EmployeeSalaryStructure, PayrollSetting,
    LeaveType, LeaveBalance, LeaveApplication, HolidayCalendar, Notification,
    IndustryMaster, DepartmentMaster, RoleMaster, RolePermission, Branch, Shift
)
from .serializers import RegisterSerializer, OTPVerifySerializer, OnboardingSerializer, UserSerializer, DepartmentSerializer, RoleSerializer, EmployeeSerializer, AttendanceRecordSerializer, EmployeeDocumentSerializer, AttendanceStatusSerializer, BranchSerializer, ShiftSerializer
from django.db import transaction, connection
from django.db.models import Q
from django.core.mail import send_mail
from django.conf import settings
from django.template.loader import render_to_string
import random
from rest_framework.permissions import AllowAny
import string
import io
import csv
import json
import zipfile
from django.http import HttpResponse
from django.utils.text import slugify
from pathlib import Path

try:
    from weasyprint import HTML as WeasyHTML
except Exception:
    WeasyHTML = None

try:
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
except Exception:
    rl_canvas = None
    A4 = None
    colors = None
    mm = None
    getSampleStyleSheet = None
    SimpleDocTemplate = None
    Table = None
    TableStyle = None
    Paragraph = None
    Spacer = None

try:
    from openpyxl import Workbook
except Exception:
    Workbook = None

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
            # Leave Tables
            cursor.execute("CREATE TABLE IF NOT EXISTS t_leave_type (id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id CHAR(32) NOT NULL, name VARCHAR(100) NOT NULL, code VARCHAR(10) DEFAULT 'CL', days_per_year INTEGER DEFAULT 12, is_paid BOOLEAN DEFAULT 1, carry_forward BOOLEAN DEFAULT 0, max_carry_forward INTEGER DEFAULT 0, FOREIGN KEY(tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE)")
            cursor.execute("CREATE TABLE IF NOT EXISTS t_leave_balance (id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id CHAR(32) NOT NULL, employee_id INTEGER NOT NULL, leave_type_id INTEGER NOT NULL, year INTEGER DEFAULT 2026, allocated DECIMAL(5, 1) DEFAULT 0, used DECIMAL(5, 1) DEFAULT 0, carried_forward DECIMAL(5, 1) DEFAULT 0, FOREIGN KEY(tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE, FOREIGN KEY(employee_id) REFERENCES t_employee(id) ON DELETE CASCADE, FOREIGN KEY(leave_type_id) REFERENCES t_leave_type(id) ON DELETE CASCADE)")
            cursor.execute("CREATE TABLE IF NOT EXISTS t_leave_application (id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id CHAR(32) NOT NULL, employee_id INTEGER NOT NULL, leave_type_id INTEGER NOT NULL, from_date DATE NOT NULL, to_date DATE NOT NULL, reason TEXT, status VARCHAR(20) DEFAULT 'Pending', reviewed_by_id INTEGER, reviewed_at DATETIME, review_comment TEXT, created_at DATETIME NOT NULL, FOREIGN KEY(tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE, FOREIGN KEY(employee_id) REFERENCES t_employee(id) ON DELETE CASCADE, FOREIGN KEY(leave_type_id) REFERENCES t_leave_type(id) ON DELETE CASCADE)")
            cursor.execute("CREATE TABLE IF NOT EXISTS t_holiday_calendar (id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id CHAR(32) NOT NULL, name VARCHAR(255) NOT NULL, date DATE NOT NULL, holiday_type VARCHAR(20) DEFAULT 'National', description TEXT, FOREIGN KEY(tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE)")
            cursor.execute("CREATE TABLE IF NOT EXISTS t_notification (id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id CHAR(32) NOT NULL, user_id INTEGER NOT NULL, title VARCHAR(255) NOT NULL, message TEXT, notify_type VARCHAR(20) DEFAULT 'info', is_read BOOLEAN DEFAULT 0, action_url VARCHAR(255), created_at DATETIME NOT NULL, FOREIGN KEY(tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE, FOREIGN KEY(user_id) REFERENCES t_user(id) ON DELETE CASCADE)")
            
            try:
                cursor.execute("SELECT extended_profile FROM t_employee LIMIT 1")
            except Exception:
                cursor.execute("ALTER TABLE t_employee ADD COLUMN extended_profile TEXT DEFAULT '{}'")

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
                file_url TEXT NULL,
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


def ensure_hr_lifecycle_tables_exist():
    """
    Create employee lifecycle workflow tables if missing.
    - transfer / promotion movements
    - salary revision history
    - exit/termination records
    - generic lifecycle event audit trail
    """
    vendor = getattr(connection, "vendor", "")
    with connection.cursor() as cursor:
        if vendor == "sqlite":
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS t_employee_lifecycle_event (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id CHAR(32) NOT NULL,
                    employee_id INTEGER NOT NULL,
                    event_type VARCHAR(50) NOT NULL,
                    effective_from DATE,
                    meta TEXT,
                    created_by_id INTEGER,
                    created_at DATETIME NOT NULL,
                    FOREIGN KEY(tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE,
                    FOREIGN KEY(employee_id) REFERENCES t_employee(id) ON DELETE CASCADE,
                    FOREIGN KEY(created_by_id) REFERENCES t_user(id) ON DELETE SET NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS t_employee_transfer (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id CHAR(32) NOT NULL,
                    employee_id INTEGER NOT NULL,
                    from_department_id INTEGER,
                    to_department_id INTEGER,
                    from_designation_id INTEGER,
                    to_designation_id INTEGER,
                    from_manager_id INTEGER,
                    to_manager_id INTEGER,
                    effective_from DATE NOT NULL,
                    reason TEXT,
                    status VARCHAR(20) DEFAULT 'Approved',
                    created_by_id INTEGER,
                    created_at DATETIME NOT NULL,
                    FOREIGN KEY(tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE,
                    FOREIGN KEY(employee_id) REFERENCES t_employee(id) ON DELETE CASCADE
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS t_employee_salary_revision (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id CHAR(32) NOT NULL,
                    employee_id INTEGER NOT NULL,
                    old_ctc DECIMAL(12,2) DEFAULT 0,
                    new_ctc DECIMAL(12,2) DEFAULT 0,
                    old_base_salary DECIMAL(12,2) DEFAULT 0,
                    new_base_salary DECIMAL(12,2) DEFAULT 0,
                    effective_from DATE NOT NULL,
                    reason TEXT,
                    status VARCHAR(20) DEFAULT 'Approved',
                    created_by_id INTEGER,
                    created_at DATETIME NOT NULL,
                    FOREIGN KEY(tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE,
                    FOREIGN KEY(employee_id) REFERENCES t_employee(id) ON DELETE CASCADE
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS t_employee_exit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id CHAR(32) NOT NULL,
                    employee_id INTEGER NOT NULL,
                    exit_type VARCHAR(30) DEFAULT 'Termination',
                    last_working_day DATE,
                    resignation_date DATE,
                    reason TEXT,
                    status VARCHAR(20) DEFAULT 'Open',
                    settlement_status VARCHAR(20) DEFAULT 'Pending',
                    created_by_id INTEGER,
                    created_at DATETIME NOT NULL,
                    FOREIGN KEY(tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE,
                    FOREIGN KEY(employee_id) REFERENCES t_employee(id) ON DELETE CASCADE
                )
                """
            )
            return

        # MySQL / MariaDB
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_employee_lifecycle_event (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                tenant_id CHAR(32) NOT NULL,
                employee_id BIGINT NOT NULL,
                event_type VARCHAR(50) NOT NULL,
                effective_from DATE NULL,
                meta JSON NULL,
                created_by_id BIGINT NULL,
                created_at DATETIME(6) NOT NULL,
                INDEX t_emp_event_tenant_emp_idx (tenant_id, employee_id),
                CONSTRAINT t_emp_event_tenant_fk FOREIGN KEY (tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE,
                CONSTRAINT t_emp_event_emp_fk FOREIGN KEY (employee_id) REFERENCES t_employee(id) ON DELETE CASCADE,
                CONSTRAINT t_emp_event_user_fk FOREIGN KEY (created_by_id) REFERENCES t_user(id) ON DELETE SET NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_employee_transfer (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                tenant_id CHAR(32) NOT NULL,
                employee_id BIGINT NOT NULL,
                from_department_id BIGINT NULL,
                to_department_id BIGINT NULL,
                from_designation_id BIGINT NULL,
                to_designation_id BIGINT NULL,
                from_manager_id BIGINT NULL,
                to_manager_id BIGINT NULL,
                effective_from DATE NOT NULL,
                reason TEXT NULL,
                status VARCHAR(20) DEFAULT 'Approved',
                created_by_id BIGINT NULL,
                created_at DATETIME(6) NOT NULL,
                INDEX t_emp_transfer_tenant_emp_idx (tenant_id, employee_id),
                CONSTRAINT t_emp_transfer_tenant_fk FOREIGN KEY (tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE,
                CONSTRAINT t_emp_transfer_emp_fk FOREIGN KEY (employee_id) REFERENCES t_employee(id) ON DELETE CASCADE
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_employee_salary_revision (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                tenant_id CHAR(32) NOT NULL,
                employee_id BIGINT NOT NULL,
                old_ctc DECIMAL(12,2) DEFAULT 0,
                new_ctc DECIMAL(12,2) DEFAULT 0,
                old_base_salary DECIMAL(12,2) DEFAULT 0,
                new_base_salary DECIMAL(12,2) DEFAULT 0,
                effective_from DATE NOT NULL,
                reason TEXT NULL,
                status VARCHAR(20) DEFAULT 'Approved',
                created_by_id BIGINT NULL,
                created_at DATETIME(6) NOT NULL,
                INDEX t_emp_salrev_tenant_emp_idx (tenant_id, employee_id),
                CONSTRAINT t_emp_salrev_tenant_fk FOREIGN KEY (tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE,
                CONSTRAINT t_emp_salrev_emp_fk FOREIGN KEY (employee_id) REFERENCES t_employee(id) ON DELETE CASCADE
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_employee_exit (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                tenant_id CHAR(32) NOT NULL,
                employee_id BIGINT NOT NULL,
                exit_type VARCHAR(30) DEFAULT 'Termination',
                last_working_day DATE NULL,
                resignation_date DATE NULL,
                reason TEXT NULL,
                status VARCHAR(20) DEFAULT 'Open',
                settlement_status VARCHAR(20) DEFAULT 'Pending',
                created_by_id BIGINT NULL,
                created_at DATETIME(6) NOT NULL,
                INDEX t_emp_exit_tenant_emp_idx (tenant_id, employee_id),
                CONSTRAINT t_emp_exit_tenant_fk FOREIGN KEY (tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE,
                CONSTRAINT t_emp_exit_emp_fk FOREIGN KEY (employee_id) REFERENCES t_employee(id) ON DELETE CASCADE
            )
            """
        )


def ensure_branch_shift_tables_exist():
    """Create branch + shift masters if missing (SQLite + MySQL)."""
    vendor = getattr(connection, "vendor", "")
    with connection.cursor() as cursor:
        if vendor == "sqlite":
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS t_branch (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id CHAR(32) NOT NULL,
                    code VARCHAR(30) DEFAULT '',
                    name VARCHAR(255) NOT NULL,
                    address TEXT,
                    city VARCHAR(100),
                    state VARCHAR(100),
                    country VARCHAR(100) DEFAULT 'India',
                    is_active BOOLEAN DEFAULT 1,
                    created_at DATETIME NOT NULL,
                    FOREIGN KEY(tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS t_shift (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id CHAR(32) NOT NULL,
                    code VARCHAR(30) DEFAULT '',
                    name VARCHAR(100) NOT NULL,
                    start_time TIME NOT NULL,
                    end_time TIME NOT NULL,
                    grace_minutes INTEGER DEFAULT 0,
                    is_night_shift BOOLEAN DEFAULT 0,
                    is_active BOOLEAN DEFAULT 1,
                    created_at DATETIME NOT NULL,
                    FOREIGN KEY(tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE
                )
                """
            )
            # Add optional branch_id column to t_employee if missing (best-effort)
            try:
                cursor.execute("SELECT branch_id FROM t_employee LIMIT 1")
            except Exception:
                try:
                    cursor.execute("ALTER TABLE t_employee ADD COLUMN branch_id INTEGER NULL")
                except Exception:
                    pass
            return

        # MySQL / MariaDB
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_branch (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                tenant_id CHAR(32) NOT NULL,
                code VARCHAR(30) DEFAULT '',
                name VARCHAR(255) NOT NULL,
                address TEXT NULL,
                city VARCHAR(100) NULL,
                state VARCHAR(100) NULL,
                country VARCHAR(100) DEFAULT 'India',
                is_active BOOLEAN DEFAULT 1,
                created_at DATETIME(6) NOT NULL,
                INDEX t_branch_tenant_idx (tenant_id),
                CONSTRAINT t_branch_tenant_fk FOREIGN KEY (tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_shift (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                tenant_id CHAR(32) NOT NULL,
                code VARCHAR(30) DEFAULT '',
                name VARCHAR(100) NOT NULL,
                start_time TIME NOT NULL,
                end_time TIME NOT NULL,
                grace_minutes INT DEFAULT 0,
                is_night_shift BOOLEAN DEFAULT 0,
                is_active BOOLEAN DEFAULT 1,
                created_at DATETIME(6) NOT NULL,
                INDEX t_shift_tenant_idx (tenant_id),
                CONSTRAINT t_shift_tenant_fk FOREIGN KEY (tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE
            )
            """
        )
        # Add optional branch_id column to t_employee if missing
        try:
            cursor.execute("SELECT branch_id FROM t_employee LIMIT 1")
        except Exception:
            try:
                cursor.execute("ALTER TABLE t_employee ADD COLUMN branch_id BIGINT NULL")
            except Exception:
                pass


def ensure_employee_shift_assignment_tables_exist():
    """
    Shift assignment history for employees (effective-dated).
    Stored in its own table instead of extended_profile to keep payroll/attendance reliable.
    """
    ensure_branch_shift_tables_exist()
    vendor = getattr(connection, "vendor", "")
    with connection.cursor() as cursor:
        if vendor == "sqlite":
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS t_employee_shift_assignment (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id CHAR(32) NOT NULL,
                    employee_id INTEGER NOT NULL,
                    shift_id INTEGER NOT NULL,
                    effective_from DATE NOT NULL,
                    effective_to DATE NULL,
                    is_active BOOLEAN DEFAULT 1,
                    created_by_id INTEGER NULL,
                    created_at DATETIME NOT NULL,
                    FOREIGN KEY(tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE,
                    FOREIGN KEY(employee_id) REFERENCES t_employee(id) ON DELETE CASCADE,
                    FOREIGN KEY(shift_id) REFERENCES t_shift(id) ON DELETE RESTRICT
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS t_emp_shift_tenant_emp_idx ON t_employee_shift_assignment(tenant_id, employee_id)"
            )
            return

        # MySQL / MariaDB
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_employee_shift_assignment (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                tenant_id CHAR(32) NOT NULL,
                employee_id BIGINT NOT NULL,
                shift_id BIGINT NOT NULL,
                effective_from DATE NOT NULL,
                effective_to DATE NULL,
                is_active BOOLEAN DEFAULT 1,
                created_by_id BIGINT NULL,
                created_at DATETIME(6) NOT NULL,
                INDEX t_emp_shift_tenant_emp_idx (tenant_id, employee_id),
                INDEX t_emp_shift_tenant_shift_idx (tenant_id, shift_id),
                CONSTRAINT t_emp_shift_tenant_fk FOREIGN KEY (tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE,
                CONSTRAINT t_emp_shift_emp_fk FOREIGN KEY (employee_id) REFERENCES t_employee(id) ON DELETE CASCADE,
                CONSTRAINT t_emp_shift_shift_fk FOREIGN KEY (shift_id) REFERENCES t_shift(id) ON DELETE RESTRICT
            )
            """
        )


def _upsert_employee_shift_assignment(tenant_id, employee_id, shift_id, effective_from, created_by_id=None):
    """
    Make the given shift the active assignment as of effective_from.
    Closes any previous active assignment by setting effective_to = effective_from - 1 day.
    """
    if not (tenant_id and employee_id and shift_id and effective_from):
        return
    ensure_employee_shift_assignment_tables_exist()
    vendor = getattr(connection, "vendor", "")
    with connection.cursor() as cursor:
        # Close previous active assignment(s)
        if vendor == "sqlite":
            cursor.execute(
                """
                UPDATE t_employee_shift_assignment
                SET is_active = 0,
                    effective_to = date(?, '-1 day')
                WHERE tenant_id = ? AND employee_id = ? AND is_active = 1
                """,
                [str(effective_from), str(tenant_id), int(employee_id)],
            )
            cursor.execute(
                """
                INSERT INTO t_employee_shift_assignment
                  (tenant_id, employee_id, shift_id, effective_from, effective_to, is_active, created_by_id, created_at)
                VALUES (?, ?, ?, ?, NULL, 1, ?, ?)
                """,
                [str(tenant_id), int(employee_id), int(shift_id), str(effective_from), created_by_id, timezone.now()],
            )
            return

        cursor.execute(
            """
            UPDATE t_employee_shift_assignment
            SET is_active = 0,
                effective_to = DATE_SUB(%s, INTERVAL 1 DAY)
            WHERE tenant_id = %s AND employee_id = %s AND is_active = 1
            """,
            [effective_from, tenant_id, employee_id],
        )
        cursor.execute(
            """
            INSERT INTO t_employee_shift_assignment
              (tenant_id, employee_id, shift_id, effective_from, effective_to, is_active, created_by_id, created_at)
            VALUES (%s, %s, %s, %s, NULL, 1, %s, %s)
            """,
            [tenant_id, employee_id, shift_id, effective_from, created_by_id, timezone.now()],
        )


def ensure_payroll_workflow_tables_exist():
    """
    Creates payroll workflow tables if missing (to keep environments working even
    when migrations are not applied).
    """
    vendor = getattr(connection, "vendor", "")
    with connection.cursor() as cursor:
        if vendor == "sqlite":
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS t_payroll_cycle_lock (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  tenant_id CHAR(32) NOT NULL,
                  cycle_month VARCHAR(7) NOT NULL,
                  attendance_locked BOOLEAN DEFAULT 0,
                  leave_locked BOOLEAN DEFAULT 0,
                  payroll_locked BOOLEAN DEFAULT 0,
                  locked_by_id INTEGER,
                  locked_at DATETIME,
                  UNIQUE(tenant_id, cycle_month)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS t_payroll_variable_input (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  tenant_id CHAR(32) NOT NULL,
                  employee_id INTEGER NOT NULL,
                  cycle_month VARCHAR(7) NOT NULL,
                  input_type VARCHAR(20) NOT NULL,
                  label VARCHAR(255) NOT NULL,
                  amount DECIMAL(12,2) DEFAULT 0,
                  meta TEXT DEFAULT '{}',
                  status VARCHAR(20) DEFAULT 'Draft',
                  created_by_id INTEGER,
                  approved_by_id INTEGER,
                  created_at DATETIME NOT NULL,
                  updated_at DATETIME NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS t_employee_loan (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  tenant_id CHAR(32) NOT NULL,
                  employee_id INTEGER NOT NULL,
                  loan_code VARCHAR(30),
                  principal_amount DECIMAL(12,2) NOT NULL,
                  annual_interest_rate DECIMAL(5,2) DEFAULT 0,
                  tenure_months INTEGER DEFAULT 12,
                  emi_amount DECIMAL(12,2) DEFAULT 0,
                  start_cycle_month VARCHAR(7),
                  status VARCHAR(20) DEFAULT 'Requested',
                  remarks TEXT,
                  approved_by_id INTEGER,
                  approved_at DATETIME,
                  created_at DATETIME NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS t_employee_loan_ledger (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  tenant_id CHAR(32) NOT NULL,
                  loan_id INTEGER NOT NULL,
                  cycle_month VARCHAR(7) NOT NULL,
                  opening_balance DECIMAL(12,2) DEFAULT 0,
                  emi_due DECIMAL(12,2) DEFAULT 0,
                  interest_due DECIMAL(12,2) DEFAULT 0,
                  amount_paid DECIMAL(12,2) DEFAULT 0,
                  closing_balance DECIMAL(12,2) DEFAULT 0,
                  status VARCHAR(20) DEFAULT 'Due',
                  created_at DATETIME NOT NULL,
                  UNIQUE(tenant_id, loan_id, cycle_month)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS t_reimbursement_category (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  tenant_id CHAR(32) NOT NULL,
                  code VARCHAR(30) NOT NULL,
                  name VARCHAR(100) NOT NULL,
                  is_active BOOLEAN DEFAULT 1,
                  taxable BOOLEAN DEFAULT 0,
                  max_amount_per_month DECIMAL(12,2) DEFAULT 0,
                  UNIQUE(tenant_id, code)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS t_reimbursement_claim (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  tenant_id CHAR(32) NOT NULL,
                  employee_id INTEGER NOT NULL,
                  category_id INTEGER NOT NULL,
                  cycle_month VARCHAR(7) NOT NULL,
                  claim_amount DECIMAL(12,2) DEFAULT 0,
                  description TEXT,
                  attachments TEXT DEFAULT '[]',
                  status VARCHAR(20) DEFAULT 'Draft',
                  submitted_at DATETIME,
                  hr_approved_by_id INTEGER,
                  finance_approved_by_id INTEGER,
                  approved_at DATETIME,
                  paid_at DATETIME,
                  payout_reference VARCHAR(100),
                  created_at DATETIME NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS t_payroll_arrear (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  tenant_id CHAR(32) NOT NULL,
                  employee_id INTEGER NOT NULL,
                  from_cycle_month VARCHAR(7) NOT NULL,
                  to_cycle_month VARCHAR(7) NOT NULL,
                  arrear_amount DECIMAL(12,2) DEFAULT 0,
                  reason VARCHAR(255) DEFAULT '',
                  status VARCHAR(20) DEFAULT 'Open',
                  created_at DATETIME NOT NULL
                )
                """
            )
            return

        # MySQL / MariaDB
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_payroll_cycle_lock (
              id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
              tenant_id CHAR(32) NOT NULL,
              cycle_month VARCHAR(7) NOT NULL,
              attendance_locked BOOLEAN DEFAULT 0,
              leave_locked BOOLEAN DEFAULT 0,
              payroll_locked BOOLEAN DEFAULT 0,
              locked_by_id BIGINT NULL,
              locked_at DATETIME NULL,
              UNIQUE KEY uq_payroll_cycle_lock (tenant_id, cycle_month),
              INDEX idx_payroll_cycle_lock_tenant (tenant_id),
              CONSTRAINT fk_payroll_cycle_lock_tenant FOREIGN KEY (tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_payroll_variable_input (
              id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
              tenant_id CHAR(32) NOT NULL,
              employee_id BIGINT NOT NULL,
              cycle_month VARCHAR(7) NOT NULL,
              input_type VARCHAR(20) NOT NULL,
              label VARCHAR(255) NOT NULL,
              amount DECIMAL(12,2) DEFAULT 0,
              meta JSON NULL,
              status VARCHAR(20) DEFAULT 'Draft',
              created_by_id BIGINT NULL,
              approved_by_id BIGINT NULL,
              created_at DATETIME(6) NOT NULL,
              updated_at DATETIME(6) NOT NULL,
              INDEX idx_payroll_var_cycle (tenant_id, cycle_month, input_type),
              INDEX idx_payroll_var_emp (tenant_id, employee_id, cycle_month),
              CONSTRAINT fk_payroll_var_tenant FOREIGN KEY (tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE,
              CONSTRAINT fk_payroll_var_emp FOREIGN KEY (employee_id) REFERENCES t_employee(id) ON DELETE CASCADE
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_employee_loan (
              id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
              tenant_id CHAR(32) NOT NULL,
              employee_id BIGINT NOT NULL,
              loan_code VARCHAR(30) NULL,
              principal_amount DECIMAL(12,2) NOT NULL,
              annual_interest_rate DECIMAL(5,2) DEFAULT 0,
              tenure_months INT DEFAULT 12,
              emi_amount DECIMAL(12,2) DEFAULT 0,
              start_cycle_month VARCHAR(7) NULL,
              status VARCHAR(20) DEFAULT 'Requested',
              remarks TEXT NULL,
              approved_by_id BIGINT NULL,
              approved_at DATETIME NULL,
              created_at DATETIME(6) NOT NULL,
              INDEX idx_loan_emp (tenant_id, employee_id),
              INDEX idx_loan_status (tenant_id, status),
              CONSTRAINT fk_loan_tenant FOREIGN KEY (tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE,
              CONSTRAINT fk_loan_emp FOREIGN KEY (employee_id) REFERENCES t_employee(id) ON DELETE CASCADE
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_employee_loan_ledger (
              id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
              tenant_id CHAR(32) NOT NULL,
              loan_id BIGINT NOT NULL,
              cycle_month VARCHAR(7) NOT NULL,
              opening_balance DECIMAL(12,2) DEFAULT 0,
              emi_due DECIMAL(12,2) DEFAULT 0,
              interest_due DECIMAL(12,2) DEFAULT 0,
              amount_paid DECIMAL(12,2) DEFAULT 0,
              closing_balance DECIMAL(12,2) DEFAULT 0,
              status VARCHAR(20) DEFAULT 'Due',
              created_at DATETIME(6) NOT NULL,
              UNIQUE KEY uq_loan_ledger (tenant_id, loan_id, cycle_month),
              CONSTRAINT fk_loan_ledger_tenant FOREIGN KEY (tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE,
              CONSTRAINT fk_loan_ledger_loan FOREIGN KEY (loan_id) REFERENCES t_employee_loan(id) ON DELETE CASCADE
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_reimbursement_category (
              id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
              tenant_id CHAR(32) NOT NULL,
              code VARCHAR(30) NOT NULL,
              name VARCHAR(100) NOT NULL,
              is_active BOOLEAN DEFAULT 1,
              taxable BOOLEAN DEFAULT 0,
              max_amount_per_month DECIMAL(12,2) DEFAULT 0,
              UNIQUE KEY uq_reimb_cat (tenant_id, code),
              CONSTRAINT fk_reimb_cat_tenant FOREIGN KEY (tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_reimbursement_claim (
              id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
              tenant_id CHAR(32) NOT NULL,
              employee_id BIGINT NOT NULL,
              category_id BIGINT NOT NULL,
              cycle_month VARCHAR(7) NOT NULL,
              claim_amount DECIMAL(12,2) DEFAULT 0,
              description TEXT NULL,
              attachments JSON NULL,
              status VARCHAR(20) DEFAULT 'Draft',
              submitted_at DATETIME NULL,
              hr_approved_by_id BIGINT NULL,
              finance_approved_by_id BIGINT NULL,
              approved_at DATETIME NULL,
              paid_at DATETIME NULL,
              payout_reference VARCHAR(100) NULL,
              created_at DATETIME(6) NOT NULL,
              INDEX idx_claim_cycle (tenant_id, cycle_month, status),
              INDEX idx_claim_emp (tenant_id, employee_id, cycle_month),
              CONSTRAINT fk_claim_tenant FOREIGN KEY (tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE,
              CONSTRAINT fk_claim_emp FOREIGN KEY (employee_id) REFERENCES t_employee(id) ON DELETE CASCADE,
              CONSTRAINT fk_claim_cat FOREIGN KEY (category_id) REFERENCES t_reimbursement_category(id) ON DELETE RESTRICT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_payroll_arrear (
              id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
              tenant_id CHAR(32) NOT NULL,
              employee_id BIGINT NOT NULL,
              from_cycle_month VARCHAR(7) NOT NULL,
              to_cycle_month VARCHAR(7) NOT NULL,
              arrear_amount DECIMAL(12,2) DEFAULT 0,
              reason VARCHAR(255) DEFAULT '',
              status VARCHAR(20) DEFAULT 'Open',
              created_at DATETIME(6) NOT NULL,
              INDEX idx_arrear_emp (tenant_id, employee_id),
              INDEX idx_arrear_status (tenant_id, status),
              CONSTRAINT fk_arrear_tenant FOREIGN KEY (tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE,
              CONSTRAINT fk_arrear_emp FOREIGN KEY (employee_id) REFERENCES t_employee(id) ON DELETE CASCADE
            )
            """
        )


def ensure_ess_grievance_tables_exist():
    """Creates ESS grievance/ticket table if missing."""
    vendor = getattr(connection, "vendor", "")
    with connection.cursor() as cursor:
        if vendor == "sqlite":
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS t_employee_grievance (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  tenant_id CHAR(32) NOT NULL,
                  employee_id INTEGER NOT NULL,
                  grievance_type VARCHAR(30) DEFAULT 'Grievance',
                  subject VARCHAR(255) NOT NULL,
                  description TEXT,
                  is_confidential BOOLEAN DEFAULT 1,
                  status VARCHAR(20) DEFAULT 'Open',
                  response TEXT,
                  submitted_at DATETIME NOT NULL,
                  resolved_at DATETIME,
                  created_at DATETIME NOT NULL
                )
                """
            )
            return

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_employee_grievance (
              id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
              tenant_id CHAR(32) NOT NULL,
              employee_id BIGINT NOT NULL,
              grievance_type VARCHAR(30) DEFAULT 'Grievance',
              subject VARCHAR(255) NOT NULL,
              description TEXT NULL,
              is_confidential BOOLEAN DEFAULT 1,
              status VARCHAR(20) DEFAULT 'Open',
              response TEXT NULL,
              submitted_at DATETIME(6) NOT NULL,
              resolved_at DATETIME NULL,
              created_at DATETIME(6) NOT NULL,
              INDEX idx_griev_emp (tenant_id, employee_id),
              INDEX idx_griev_status (tenant_id, status),
              CONSTRAINT fk_griev_tenant FOREIGN KEY (tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE,
              CONSTRAINT fk_griev_emp FOREIGN KEY (employee_id) REFERENCES t_employee(id) ON DELETE CASCADE
            )
            """
        )

        # Leave Tables
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_leave_type (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                tenant_id CHAR(32) NOT NULL,
                name VARCHAR(100) NOT NULL,
                code VARCHAR(10) DEFAULT 'CL',
                days_per_year INT DEFAULT 12,
                is_paid BOOLEAN DEFAULT 1,
                carry_forward BOOLEAN DEFAULT 0,
                max_carry_forward INT DEFAULT 0,
                CONSTRAINT t_leave_type_tenant_fk FOREIGN KEY (tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_leave_balance (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                tenant_id CHAR(32) NOT NULL,
                employee_id BIGINT NOT NULL,
                leave_type_id BIGINT NOT NULL,
                year INT DEFAULT 2026,
                allocated DECIMAL(5, 1) DEFAULT 0,
                used DECIMAL(5, 1) DEFAULT 0,
                carried_forward DECIMAL(5, 1) DEFAULT 0,
                CONSTRAINT t_leave_bal_tenant_fk FOREIGN KEY (tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE,
                CONSTRAINT t_leave_bal_emp_fk FOREIGN KEY (employee_id) REFERENCES t_employee(id) ON DELETE CASCADE,
                CONSTRAINT t_leave_bal_type_fk FOREIGN KEY (leave_type_id) REFERENCES t_leave_type(id) ON DELETE CASCADE
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_leave_application (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                tenant_id CHAR(32) NOT NULL,
                employee_id BIGINT NOT NULL,
                leave_type_id BIGINT NOT NULL,
                from_date DATE NOT NULL,
                to_date DATE NOT NULL,
                reason TEXT,
                status VARCHAR(20) DEFAULT 'Pending',
                reviewed_by_id BIGINT,
                reviewed_at DATETIME(6),
                review_comment TEXT,
                created_at DATETIME(6) NOT NULL,
                CONSTRAINT t_leave_app_tenant_fk FOREIGN KEY (tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE,
                CONSTRAINT t_leave_app_emp_fk FOREIGN KEY (employee_id) REFERENCES t_employee(id) ON DELETE CASCADE,
                CONSTRAINT t_leave_app_type_fk FOREIGN KEY (leave_type_id) REFERENCES t_leave_type(id) ON DELETE CASCADE
            )
            """
        )

        # ── Self-Healing Schema Updates (for existing tables) ────────────
        try:
            cursor.execute("SELECT review_comment FROM t_leave_application LIMIT 1")
        except Exception:
            cursor.execute("ALTER TABLE t_leave_application ADD COLUMN review_comment TEXT")
            
        try:
            cursor.execute("SELECT extended_profile FROM t_employee LIMIT 1")
        except Exception:
            cursor.execute("ALTER TABLE t_employee ADD COLUMN extended_profile JSON")

        try:
            cursor.execute("SELECT reporting_hr_id FROM t_employee LIMIT 1")
        except Exception:
            cursor.execute("ALTER TABLE t_employee ADD COLUMN reporting_hr_id BIGINT NULL")
            cursor.execute("ALTER TABLE t_employee ADD CONSTRAINT t_employee_hr_fk FOREIGN KEY (reporting_hr_id) REFERENCES t_employee(id) ON DELETE SET NULL")
        
        # Attendance record: store regularization comment / reason (best-effort)
        try:
            cursor.execute("SELECT regularization_reason FROM t_attendance_record LIMIT 1")
        except Exception:
            try:
                cursor.execute("ALTER TABLE t_attendance_record ADD COLUMN regularization_reason TEXT NULL")
            except Exception:
                pass

        # Attendance regularization request queue (best-effort; two-stage approvals workflow)
        try:
            cursor.execute("SELECT state FROM t_attendance_regularization_request LIMIT 1")
            # Table exists – add manager-stage columns if missing (schema upgrade)
            for col_ddl in [
                "ALTER TABLE t_attendance_regularization_request ADD COLUMN manager_reviewed_by_id BIGINT NULL",
                "ALTER TABLE t_attendance_regularization_request ADD COLUMN manager_reviewed_at DATETIME(6) NULL",
                "ALTER TABLE t_attendance_regularization_request ADD COLUMN manager_review_comment TEXT NULL",
            ]:
                try:
                    cursor.execute(col_ddl)
                except Exception:
                    pass  # Column already exists
        except Exception:
            try:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS t_attendance_regularization_request (
                        id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                        tenant_id CHAR(32) NOT NULL,
                        employee_id BIGINT NOT NULL,
                        date DATE NOT NULL,
                        requested_status_id BIGINT NULL,
                        requested_check_in TIME NULL,
                        requested_check_out TIME NULL,
                        requested_work_hours DECIMAL(6,2) NULL,
                        reason TEXT NULL,
                        state VARCHAR(32) DEFAULT 'PENDING_MANAGER',
                        requested_by_id BIGINT NULL,
                        requested_at DATETIME(6) NOT NULL,
                        manager_reviewed_by_id BIGINT NULL,
                        manager_reviewed_at DATETIME(6) NULL,
                        manager_review_comment TEXT NULL,
                        reviewed_by_id BIGINT NULL,
                        reviewed_at DATETIME(6) NULL,
                        review_comment TEXT NULL,
                        INDEX t_att_reg_req_tenant_state_idx (tenant_id, state),
                        INDEX t_att_reg_req_tenant_emp_date_idx (tenant_id, employee_id, date)
                    )
                    """
                )
            except Exception:
                pass

        # Comp Off & Overtime requests (best-effort)
        try:
            cursor.execute("SELECT state FROM t_comp_off_request LIMIT 1")
        except Exception:
            try:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS t_comp_off_request (
                        id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                        tenant_id CHAR(32) NOT NULL,
                        employee_id BIGINT NOT NULL,
                        worked_date DATE NOT NULL,
                        credit_days DECIMAL(6,2) DEFAULT 1,
                        reason TEXT NULL,
                        state VARCHAR(20) DEFAULT 'PENDING',
                        requested_by_id BIGINT NULL,
                        requested_at DATETIME(6) NOT NULL,
                        reviewed_by_id BIGINT NULL,
                        reviewed_at DATETIME(6) NULL,
                        review_comment TEXT NULL,
                        INDEX t_comp_off_req_tenant_state_idx (tenant_id, state),
                        INDEX t_comp_off_req_tenant_emp_idx (tenant_id, employee_id)
                    )
                    """
                )
            except Exception:
                pass

        try:
            cursor.execute("SELECT state FROM t_overtime_request LIMIT 1")
        except Exception:
            try:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS t_overtime_request (
                        id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                        tenant_id CHAR(32) NOT NULL,
                        employee_id BIGINT NOT NULL,
                        ot_date DATE NOT NULL,
                        hours DECIMAL(6,2) NOT NULL,
                        reason TEXT NULL,
                        state VARCHAR(20) DEFAULT 'PENDING',
                        requested_by_id BIGINT NULL,
                        requested_at DATETIME(6) NOT NULL,
                        reviewed_by_id BIGINT NULL,
                        reviewed_at DATETIME(6) NULL,
                        review_comment TEXT NULL,
                        INDEX t_ot_req_tenant_state_idx (tenant_id, state),
                        INDEX t_ot_req_tenant_emp_idx (tenant_id, employee_id)
                    )
                    """
                )
            except Exception:
                pass
            
        try:
            cursor.execute("SELECT file FROM t_employee_document LIMIT 1")
        except Exception:
            cursor.execute("ALTER TABLE t_employee_document ADD COLUMN file VARCHAR(255)")

        try:
            cursor.execute("SELECT is_verified FROM t_employee_document LIMIT 1")
        except Exception:
            cursor.execute("ALTER TABLE t_employee_document ADD COLUMN is_verified BOOLEAN DEFAULT 0")

        # Make file_url nullable since it was replaced by file
        try:
            cursor.execute("ALTER TABLE t_employee_document MODIFY COLUMN file_url TEXT NULL")
        except Exception:
            pass

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_holiday_calendar (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                tenant_id CHAR(32) NOT NULL,
                name VARCHAR(255) NOT NULL,
                date DATE NOT NULL,
                holiday_type VARCHAR(20) DEFAULT 'National',
                description TEXT,
                CONSTRAINT t_holiday_tenant_fk FOREIGN KEY (tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_notification (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                tenant_id CHAR(32) NOT NULL,
                user_id BIGINT NOT NULL,
                title VARCHAR(255) NOT NULL,
                message TEXT,
                notify_type VARCHAR(20) DEFAULT 'info',
                is_read BOOLEAN DEFAULT 0,
                action_url VARCHAR(255),
                created_at DATETIME(6) NOT NULL,
                CONSTRAINT t_notify_tenant_fk FOREIGN KEY (tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE,
                CONSTRAINT t_notify_user_fk FOREIGN KEY (user_id) REFERENCES t_user(id) ON DELETE CASCADE
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_master_lookup (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                tenant_id CHAR(32) NOT NULL,
                category VARCHAR(50) NOT NULL,
                code VARCHAR(50) NOT NULL,
                label VARCHAR(100) NOT NULL,
                is_active BOOLEAN DEFAULT 1,
                sort_order INT DEFAULT 0,
                CONSTRAINT t_lookup_tenant_fk FOREIGN KEY (tenant_id) REFERENCES t_tenant(id) ON DELETE CASCADE,
                UNIQUE KEY t_lookup_tenant_category_code (tenant_id, category, code)
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
    'employee-movements',
    'salary-revisions',
    'exit-management',
    'attendance/shift-assignment',
    'attendance/capture',
    'attendance/regularization',
    'attendance/approval',
    'attendance/reports',
    'leave/master',
    'leave/policy',
    'leave/application',
    'leave/approval',
    'leave/balance',
    'leave/holiday-calendar',
    'leave/comp-off',
    'leave/reports',
    'payroll/input',
    'payroll/process',
    'payroll/verification',
    'payroll/approval',
    'payroll/payslip-generation',
    'payroll/master',
    'payroll/compliance-reports',
    'reports',
    'org-master',
    'branch-setup',
    'shift-setup',
    'role-permissions',
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

    # C-level / architects / delivery/program/project leadership: strong visibility
    if any(k in name for k in [
        'chief technology officer', 'cto',
        'chief financial officer', 'cfo',
        'technical architect', 'solution architect', 'architect',
        'delivery manager', 'program manager', 'project manager',
    ]):
        return [
            'dashboard', 'pending', 'notifications',
            'employees/new', 'employee-movements', 'salary-revisions', 'exit-management',
            'attendance/reports', 'leave/reports',
            'payroll/compliance-reports', 'reports',
        ]

    # HR
    if 'hr' in name or 'talent acquisition' in name or 'recruiter' in name:
        if 'intern' in name:
            return ['dashboard', 'notifications', 'employees/new']
        
        # Standard HR access
        return [
            'dashboard', 'pending', 'notifications',
            'employees/new', 'employee-movements', 'salary-revisions', 'exit-management',
            'attendance/shift-assignment', 'attendance/capture', 'attendance/regularization', 'attendance/approval', 'attendance/reports',
            'leave/master', 'leave/policy', 'leave/application', 'leave/approval', 'leave/balance', 'leave/holiday-calendar', 'leave/comp-off', 'leave/reports',
            'payroll/input', 'payroll/process', 'payroll/verification', 'payroll/approval', 'payroll/payslip-generation', 'payroll/master', 'payroll/compliance-reports',
            'reports',
        ]

    # Payroll / Finance / Accounts
    if any(k in name for k in ['payroll', 'accounts', 'accountant', 'finance', 'auditor', 'tax']):
        return [
            'dashboard', 'notifications',
            'payroll/input', 'payroll/process', 'payroll/verification', 'payroll/approval', 'payroll/payslip-generation', 'payroll/master', 'payroll/compliance-reports',
            'reports',
        ]

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
        # If stats were pre-calculated (recommended), use them. 
        # Note: caller should ensure these stats are from the lookback month.
        stats = attendance_stats_by_employee_id.get(eid, {'present': 0, 'paid': 0})
        present_days = stats['present']
        paid_days = stats['paid']
    else:
        # Fallback: calculate on the fly with lookback logic
        from datetime import datetime, timedelta
        try:
            _y, _m = map(int, str(record.cycle_month).split('-'))
            _cur = datetime(_y, _m, 1)
            _prev = _cur - timedelta(days=1)
            _attendance_cycle = _prev.strftime('%Y-%m')
        except Exception:
            _attendance_cycle = record.cycle_month

        attendance_qs = AttendanceRecord.objects.filter(
            tenant=tenant,
            employee=record.employee,
            date__startswith=_attendance_cycle
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
            phone = serializer.validated_data.get('phone', '')

            if User.objects.filter(email=email).exists():
                return Response({"error": "Email already exists"}, status=status.HTTP_400_BAD_REQUEST)

            # Create Tenant
            tenant = Tenant.objects.create(name=company_name, phone=phone)

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

            # Send OTP via Email
            print(f"OTP for {email}: {otp_code}")
            try:
                from django.conf import settings
                send_mail(
                    subject='Verify Your HRMS Account',
                    message=f'Hello,\n\nYour 6-digit verification code is: {otp_code}\n\nThis code will expire in 10 minutes.\n\nRegards,\nHRMS Team',
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[email],
                    fail_silently=False,
                )
            except Exception as e:
                print(f"Failed to send OTP email: {e}")

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


class ResendOTPView(views.APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        email = request.data.get('email')
        if not email:
            return Response({"error": "Email is required"}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            user = User.objects.get(email=email)
            otp_code = str(random.randint(100000, 999999))
            OTP.objects.create(user=user, code=otp_code)
            
            # Send OTP via Email
            try:
                from django.conf import settings
                from django.core.mail import send_mail
                send_mail(
                    subject='Verify Your HRMS Account',
                    message=f'Hello,\n\nYour new 6-digit verification code is: {otp_code}\n\nThis code will expire in 10 minutes.\n\nRegards,\nHRMS Team',
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[email],
                    fail_silently=False,
                )
            except Exception as e:
                print(f"Failed to resend OTP email: {e}")
                
            return Response({"message": "OTP resent successfully"})
        except User.DoesNotExist:
            return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)

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
        {'code': 'CL',  'label': 'Casual Leave','color_code': '#a855f7', 'default_work_hours': 0.0},
        {'code': 'SL',  'label': 'Sick Leave',  'color_code': '#ec4899', 'default_work_hours': 0.0},
        {'code': 'EL',  'label': 'Earned Leave','color_code': '#3b82f6', 'default_work_hours': 0.0},
        {'code': 'ML',  'label': 'Maternity Leave','color_code': '#db2777','default_work_hours': 0.0},
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
            tenant.onboarding_step = max(int(getattr(tenant, 'onboarding_step', 0) or 0), 5)
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

        tenant.onboarding_step = 5
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
                        'is_verified': True,
                        'must_change_password': True
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
                        'phone': emp.get('phone') or '',
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

        tenant.onboarding_step = 9
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

        serializer = OnboardingSerializer(tenant, data=payload, partial=True, context={"request": request})
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

        tenant.onboarding_step = 4
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
                "active_payroll": PayrollRecord.objects.filter(
                    tenant=tenant, 
                    status='Processed',
                    cycle_month=timezone.localdate().strftime('%Y-%m')
                ).count(),
                "pending_leaves": LeaveApplication.objects.filter(tenant=tenant, status='Pending').count()
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
            phone=payload.get('phone') or '',
            employee_code=payload.get('employeeCode') or payload.get('employee_code'),
            department=department,
            designation=role,
            reporting_to=manager,
            status=payload.get('status') or 'Active',
            joining_date=payload.get('joiningDate') or payload.get('joining_date') or None,
            dob=payload.get('dob') or None,
            gender=payload.get('gender') or '',
            address=payload.get('address') or '',
            bank_name=payload.get('bank_name') or '',
            account_number=payload.get('account_number') or '',
            ifsc_code=payload.get('ifsc_code') or '',
            emergency_contact_name=payload.get('emergency_contact_name') or '',
            emergency_contact_phone=payload.get('emergency_contact_phone') or '',
            pan_number=payload.get('pan_number') or '',
            aadhar_number=payload.get('aadhar_number') or '',
            uan_number=payload.get('uan_number') or '',
            tax_regime=payload.get('tax_regime') or 'New',
            pf_applicable=payload.get('pf_applicable', True),
            esi_applicable=payload.get('esi_applicable', False),
            base_salary=payload.get('base_salary') or 0,
            extended_profile=payload.get('extended_profile', {})
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
        employee.phone = payload.get('phone', employee.phone)
        employee.employee_code = payload.get('employeeCode') or payload.get('employee_code') or employee.employee_code
        employee.status = payload.get('status', employee.status)
        employee.joining_date = payload.get('joiningDate') or payload.get('joining_date') or employee.joining_date
        
        # Onboarding fields – optional updates
        employee.dob = payload.get('dob', employee.dob)
        employee.gender = payload.get('gender', employee.gender)
        employee.address = payload.get('address', employee.address)
        employee.bank_name = payload.get('bank_name', employee.bank_name)
        employee.account_number = payload.get('account_number', employee.account_number)
        employee.ifsc_code = payload.get('ifsc_code', employee.ifsc_code)
        employee.emergency_contact_name = payload.get('emergency_contact_name', employee.emergency_contact_name)
        employee.emergency_contact_phone = payload.get('emergency_contact_phone', employee.emergency_contact_phone)
        employee.onboarding_status = payload.get('onboarding_status', employee.onboarding_status)
        employee.pan_number = payload.get('pan_number', employee.pan_number)
        employee.aadhar_number = payload.get('aadhar_number', employee.aadhar_number)
        employee.uan_number = payload.get('uan_number', employee.uan_number)
        employee.tax_regime = payload.get('tax_regime', employee.tax_regime)
        employee.pf_applicable = payload.get('pf_applicable', employee.pf_applicable)
        employee.esi_applicable = payload.get('esi_applicable', employee.esi_applicable)
        employee.base_salary = payload.get('base_salary', employee.base_salary)
        
        # Extended Profile Update
        if 'extended_profile' in payload:
            if not isinstance(employee.extended_profile, dict):
                employee.extended_profile = {}
            employee.extended_profile.update(payload.get('extended_profile', {}))

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

class EmployeeDocumentUploadView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, employee_id):
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR', 'MANAGER', 'EMPLOYEE']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        employee = Employee.objects.filter(tenant=tenant, id=employee_id).first()
        if not employee:
            return Response({"error": "Employee not found"}, status=status.HTTP_404_NOT_FOUND)
        if not _is_employee_in_scope(request, employee):
            return _forbidden_employee_access()

        uploaded_file = request.FILES.get('file')
        document_type = request.data.get('document_type')

        if not uploaded_file or not document_type:
            return Response({"error": "File and document_type are required"}, status=status.HTTP_400_BAD_REQUEST)

        # Basic validation
        if uploaded_file.size > 5 * 1024 * 1024: # 5MB limit
            return Response({"error": "File size exceeds 5MB"}, status=status.HTTP_400_BAD_REQUEST)

        doc = EmployeeDocument.objects.create(
            tenant=tenant,
            employee=employee,
            document_type=document_type,
            file=uploaded_file
        )

        return Response({
            "message": f"{document_type} uploaded successfully",
            "document_id": doc.id,
            "url": request.build_absolute_uri(doc.file.url) if doc.file else None
        }, status=status.HTTP_201_CREATED)


class EmployeeDocumentVerifyView(views.APIView):
    """HR/Admin: mark an employee document as verified/unverified."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, employee_id, document_id):
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)

        tenant = request.user.tenant
        employee = Employee.objects.filter(tenant=tenant, id=employee_id).first()
        if not employee:
            return Response({"error": "Employee not found"}, status=status.HTTP_404_NOT_FOUND)
        if not _is_employee_in_scope(request, employee):
            return _forbidden_employee_access()
        doc = EmployeeDocument.objects.filter(
            tenant=tenant,
            employee_id=employee_id,
            id=document_id
        ).first()
        if not doc:
            return Response({"error": "Document not found"}, status=status.HTTP_404_NOT_FOUND)

        is_verified = request.data.get('is_verified')
        doc.is_verified = bool(is_verified)
        doc.save(update_fields=['is_verified'])
        return Response({
            "message": "Document verification updated",
            "document_id": doc.id,
            "is_verified": doc.is_verified,
        })

class AttendanceDataView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        # Best-effort schema guard for environments where migrations were skipped.
        # Prevents "Unknown column ...regularization_reason" when AttendanceRecord model evolves.
        try:
            ensure_master_tables_exist()
        except Exception:
            pass

        tenant = request.user.tenant
        target_date = request.query_params.get('date')
        target_month = request.query_params.get('month')
        target_start = request.query_params.get('start')
        target_end = request.query_params.get('end')

        if target_start and target_end:
            try:
                from datetime import datetime
                start_date = datetime.strptime(target_start, '%Y-%m-%d').date()
                end_date = datetime.strptime(target_end, '%Y-%m-%d').date()
            except Exception:
                return Response({"error": "start/end must be YYYY-MM-DD"}, status=400)
            if start_date > end_date:
                return Response({"error": "start cannot be after end"}, status=400)

            employees = Employee.objects.filter(tenant=tenant).select_related('department')
            records = AttendanceRecord.objects.filter(
                tenant=tenant, date__range=[start_date, end_date]
            ).select_related('status')

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
                    'workHours': float(record.work_hours),
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
            return Response({'range_data': grouped_data, 'start': target_start, 'end': target_end})

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
        # Best-effort schema guard for environments where migrations were skipped.
        try:
            ensure_master_tables_exist()
        except Exception:
            pass

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
        import calendar as _calendar
        from datetime import datetime, timedelta

        # Calculate Previous Month for attendance lookback
        try:
            _year, _month = map(int, cycle_month.split('-'))
            current_date = datetime(_year, _month, 1)
            prev_month_date = current_date - timedelta(days=1)
            attendance_cycle = prev_month_date.strftime('%Y-%m')
        except Exception:
            attendance_cycle = cycle_month

        attendance_qs = AttendanceRecord.objects.filter(tenant=tenant, date__startswith=attendance_cycle).select_related('status')
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
            # If base salary is missing but CTC is available in extended_profile, derive a sensible default.
            try:
                ext = employee.extended_profile or {}
            except Exception:
                ext = {}
            try:
                ctc = Decimal(str(ext.get('ctc', 0) or 0))
            except Exception:
                ctc = Decimal('0')
            if (base_salary is None or base_salary == 0) and ctc > 0:
                # Default: Basic = 50% of monthly gross; align with UI auto-split.
                monthly_gross = (ctc / Decimal('12')).quantize(Decimal('0.01'))
                base_salary = (monthly_gross * Decimal('0.50')).quantize(Decimal('0.01'))
                # Keep employee base_salary updated for payroll engine consistency
                try:
                    employee.base_salary = base_salary
                    employee.save(update_fields=['base_salary'])
                except Exception:
                    pass

            # Prefer onboarding breakup (monthly) if present; otherwise fallback to 20% mock allowance.
            try:
                hra = Decimal(str(ext.get('hra', 0) or 0))
                allowances = Decimal(str(ext.get('allowances', 0) or 0))
                bonus = Decimal(str(ext.get('bonus', 0) or 0))
                incentives = Decimal(str(ext.get('incentives', 0) or 0))
            except Exception:
                hra = allowances = bonus = incentives = Decimal('0')
            seeded_allowances = (hra + allowances + bonus + incentives).quantize(Decimal('0.01'))
            if seeded_allowances <= 0:
                seeded_allowances = (base_salary * Decimal('0.2')).quantize(Decimal('0.01'))

            record, created = PayrollRecord.objects.get_or_create(
                tenant=tenant,
                employee=employee,
                cycle_month=cycle_month,
                defaults={
                    'base_salary': base_salary,
                    'allowances': seeded_allowances,
                    'deductions': Decimal('0'),
                    'loan_emi': Decimal('0'),
                    'tax_status': 'Pending',
                    'net_pay': Decimal('0'),
                    'status': 'Pending',
                }
            )
            
            # Sync base salary if the record is still pending and doesn't match
            if not created and record.status == 'Pending' and record.base_salary != base_salary:
                record.base_salary = base_salary
                # Keep allowances in sync with onboarding breakup (or fallback)
                record.allowances = seeded_allowances
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

        # Role gate: payroll processing is HR/Admin only
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({'error': 'Permission denied'}, status=403)

        # Validate action
        allowed_actions = {'process', 'approve', 'pay', 'lock'}
        if action not in allowed_actions:
            return Response({'error': f"Invalid action. Allowed: {', '.join(sorted(allowed_actions))}."}, status=400)

        # Validate cycle format (YYYY-MM)
        try:
            _y, _m = [int(x) for x in str(cycle_month).split('-')]
            if _y < 2000 or _y > 2100 or _m < 1 or _m > 12:
                raise ValueError("out of range")
            cycle_month = f"{_y:04d}-{_m:02d}"
        except Exception:
            return Response({'error': 'Invalid cycle. Use YYYY-MM.'}, status=400)

        # Ensure workflow tables exist (inputs/loans/reimbursements/arrears/locks)
        ensure_payroll_workflow_tables_exist()

        records = PayrollRecord.objects.filter(tenant=tenant, cycle_month=cycle_month)
        
        # Cycle lock (workflow lock table) should block any state transitions / processing
        try:
            cycle_lock = PayrollCycleLock.objects.filter(tenant=tenant, cycle_month=cycle_month).first()
        except Exception:
            cycle_lock = None
        if cycle_lock and bool(getattr(cycle_lock, 'payroll_locked', False)):
            return Response({'error': 'This payroll cycle is locked and cannot be modified.'}, status=400)

        # Check if cycle is locked
        if records.filter(status='Locked').exists():
             return Response({'error': 'This payroll cycle is locked and cannot be modified.'}, status=400)

        # Require seed step before any action. UI already calls PayrollDataView which seeds records.
        if not records.exists():
            return Response({'error': f'No payroll records found for {cycle_month}. Please load payroll data first.'}, status=400)

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

        # For processing, enforce upstream locks for consistency (attendance + leave)
        if cycle_lock:
            if not bool(getattr(cycle_lock, 'attendance_locked', False)):
                return Response({'error': 'Attendance is not locked for this cycle. Lock attendance before processing payroll.'}, status=400)
            if not bool(getattr(cycle_lock, 'leave_locked', False)):
                return Response({'error': 'Leave is not locked for this cycle. Lock leave before processing payroll.'}, status=400)

        # 1. Determine calendar days in the cycle month
        import calendar as _calendar
        from datetime import datetime, timedelta
        try:
            _year, _month = map(int, cycle_month.split('-'))
            days_in_month = _calendar.monthrange(_year, _month)[1]
            
            # Calculate Previous Month for attendance lookback
            current_date = datetime(_year, _month, 1)
            prev_month_date = current_date - timedelta(days=1)
            attendance_cycle = prev_month_date.strftime('%Y-%m')
        except Exception:
            days_in_month = 30
            attendance_cycle = cycle_month # Fallback

        # 2. Fetch Attendance Report for LOP/present calculation (Pulling from PREVIOUS MONTH)
        paid_days_report = {}    # eid -> paid count (Present, WO, Holiday)
        present_report   = {}    # eid -> actual present count
        try:
            from collections import defaultdict
            qs = AttendanceRecord.objects.filter(tenant=tenant, date__startswith=attendance_cycle)
            
            # VALIDATION: Check if attendance exists for the lookback period
            if not qs.exists():
                return Response({
                    'error': f'Attendance data for {attendance_cycle} is missing. Please upload/complete attendance before processing {cycle_month} payroll.'
                }, status=400)

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
        # (Second except removed — redundant/unreachable)

        # ── Process Pending records ────────────────────────────────────────────
        updated = 0
        statutory_skip_codes = {'PF_EMP', 'PF_EMPLR', 'ESI_EMP', 'ESI_EMPLR', 'PTAX', 'TDS'}

        # Pre-fetch workflow data for this cycle to avoid N+1 queries
        variable_inputs_by_emp: dict[str, list] = {}
        reimb_by_emp: dict[str, list] = {}
        arrears_by_emp: dict[str, list] = {}
        loans_by_emp: dict[str, list] = {}

        try:
            inputs_qs = PayrollVariableInput.objects.filter(
                tenant=tenant,
                cycle_month=cycle_month,
                status='Approved',
            ).select_related('employee')
            for it in inputs_qs:
                eid = str(it.employee_id)
                variable_inputs_by_emp.setdefault(eid, []).append(it)
        except Exception:
            pass

        try:
            claims_qs = ReimbursementClaim.objects.filter(
                tenant=tenant,
                cycle_month=cycle_month,
                status__in=['Finance Approved', 'Paid'],
            ).select_related('employee', 'category')
            for c in claims_qs:
                eid = str(c.employee_id)
                reimb_by_emp.setdefault(eid, []).append(c)
        except Exception:
            pass

        try:
            arrear_qs = PayrollArrear.objects.filter(
                tenant=tenant,
                status='Open',
            ).select_related('employee')
            # Apply arrears if the cycle falls within the arrear range.
            for a in arrear_qs:
                if str(a.from_cycle_month) <= str(cycle_month) <= str(a.to_cycle_month):
                    eid = str(a.employee_id)
                    arrears_by_emp.setdefault(eid, []).append(a)
        except Exception:
            pass

        try:
            loan_qs = EmployeeLoan.objects.filter(
                tenant=tenant,
                status__in=['Active', 'Approved'],
            ).select_related('employee')
            for ln in loan_qs:
                eid = str(ln.employee_id)
                loans_by_emp.setdefault(eid, []).append(ln)
        except Exception:
            pass

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
                # Fallback to Basic + onboarding allowances (or 20% if not available)
                seeded_allow = Decimal(record.allowances or 0).quantize(Decimal('0.01'))
                allowance_amt = seeded_allow if seeded_allow > 0 else (base_salary * Decimal('0.2')).quantize(Decimal('0.01'))
                total_earnings = base_salary + allowance_amt
                breakdown["earnings"].append({"name": "Basic", "amount": float(base_salary), "code": "BASIC"})
                breakdown["earnings"].append({"name": "Allowances", "amount": float(allowance_amt), "code": "ALLOW"})

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

            # 2.1) Apply approved variable inputs (OT/Incentives/One-time items)
            for it in variable_inputs_by_emp.get(str(record.employee_id), []) or []:
                amt = Decimal(it.amount or 0).quantize(Decimal('0.01'))
                if amt == 0:
                    continue
                label = (getattr(it, 'label', None) or '').strip() or getattr(it, 'input_type', 'INPUT')
                itype = str(getattr(it, 'input_type', '') or '').upper()
                code = f"VAR_{itype[:10]}"

                # Map to earning vs deduction
                as_deduction = itype in ('DEDUCTION',)
                if itype == 'ADJUSTMENT':
                    # Allow negative adjustments (treated as deduction)
                    if amt < 0:
                        as_deduction = True
                        amt = abs(amt)

                if as_deduction:
                    total_deductions += amt
                    breakdown["deductions"].append({"name": label, "amount": float(amt), "code": code})
                else:
                    total_earnings += amt
                    breakdown["earnings"].append({"name": label, "amount": float(amt), "code": code})

            # 2.2) Apply open arrears for this cycle range
            for a in arrears_by_emp.get(str(record.employee_id), []) or []:
                amt = Decimal(a.arrear_amount or 0).quantize(Decimal('0.01'))
                if amt <= 0:
                    continue
                label = (getattr(a, 'reason', None) or '').strip() or 'Salary Arrear'
                total_earnings += amt
                breakdown["earnings"].append({"name": label, "amount": float(amt), "code": "ARREAR"})

            # 2.3) Apply approved reimbursements (treated as earning payout)
            for c in reimb_by_emp.get(str(record.employee_id), []) or []:
                amt = Decimal(c.claim_amount or 0).quantize(Decimal('0.01'))
                if amt <= 0:
                    continue
                cat_name = getattr(getattr(c, 'category', None), 'name', None) or 'Reimbursement'
                total_earnings += amt
                breakdown["earnings"].append({"name": f"{cat_name} Reimbursement", "amount": float(amt), "code": "REIMB"})

            # 3) Statutory deductions computed by engine rules
            emp = record.employee
            ext = {}
            try:
                ext = emp.extended_profile or {}
            except Exception:
                ext = {}
            pf_applicable = bool(getattr(emp, 'pf_applicable', True))
            esi_applicable = bool(getattr(emp, 'esi_applicable', False))
            tds_applicable = bool(ext.get('tds_applicable', True))
            pt_applicable = bool(ext.get('professional_tax', True))

            # PF: employee deduction affects net pay; employer contribution is stored separately for display.
            if pf_applicable:
                pf_emp = (base_salary * setting.pf_rate_employee / Decimal('100')).quantize(Decimal('0.01'))
                employer_pf = (base_salary * setting.pf_rate_employer / Decimal('100')).quantize(Decimal('0.01'))
                total_deductions += pf_emp
                breakdown["deductions"].append({"name": "Provident Fund", "amount": float(pf_emp), "code": "PF"})
            else:
                pf_emp = Decimal('0')
                employer_pf = Decimal('0')

            # TDS / Income tax slab (MVP rule). If you need full regime support, extend here.
            if tds_applicable:
                itax = base_salary * (
                    Decimal('0.20') if base_salary > 100000 else (Decimal('0.10') if base_salary > 50000 else Decimal('0.05'))
                )
                tds_amount = itax.quantize(Decimal('0.01'))
                total_deductions += tds_amount
                breakdown["deductions"].append({"name": "Income Tax", "amount": float(tds_amount), "code": "ITAX"})
            else:
                tds_amount = Decimal('0')

            ptax = (Decimal('200') if base_salary > 15000 else Decimal('0')) if pt_applicable else Decimal('0')
            if ptax > 0:
                total_deductions += ptax
                breakdown["deductions"].append({"name": "Professional Tax", "amount": float(ptax), "code": "PTAX"})

            # ESI threshold logic (employee share only affects net pay)
            # If gross pay <= 21000, apply ESI (Employee % from tenant setting)
            if esi_applicable and total_earnings <= Decimal('21000'):
                esi_emp = (total_earnings * setting.esi_rate_employee / Decimal('100')).quantize(Decimal('0.01'))
                total_deductions += esi_emp
                breakdown["deductions"].append({"name": "ESI (Employee)", "amount": float(esi_emp), "code": "ESI"})
            else:
                esi_emp = Decimal('0')

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
            # Prefer workflow loans if present; otherwise fallback to record.loan_emi
            loan_emi = Decimal('0.00')
            employee_loans = loans_by_emp.get(str(record.employee_id), []) or []
            if employee_loans:
                for ln in employee_loans:
                    # Apply only if started
                    start_cycle = str(getattr(ln, 'start_cycle_month', '') or '')
                    if start_cycle and str(cycle_month) < start_cycle:
                        continue

                    # If ledger has a due line for this month, prefer it
                    try:
                        led = EmployeeLoanLedger.objects.filter(
                            tenant=tenant,
                            loan_id=ln.id,
                            cycle_month=cycle_month,
                        ).first()
                    except Exception:
                        led = None
                    if led:
                        loan_emi += Decimal(led.emi_due or 0)
                        loan_emi += Decimal(led.interest_due or 0)  # interest in deductions too
                    else:
                        loan_emi += Decimal(getattr(ln, 'emi_amount', 0) or 0)
            else:
                loan_emi = Decimal(record.loan_emi or 0)

            loan_interest = Decimal('0.00')
            if loan_emi > 0:
                # If we already included interest via ledger, keep engine interest minimal
                # (fallback interest only when ledger isn't used)
                if not employee_loans:
                    loan_interest = (loan_emi * setting.loan_interest_rate_annual / Decimal('100') / Decimal('12')).quantize(Decimal('0.01'))
                    breakdown["deductions"].append({"name": "Loan Interest", "amount": float(loan_interest), "code": "INT"})
                breakdown["deductions"].append({"name": "Loan EMI", "amount": float(loan_emi.quantize(Decimal('0.01'))), "code": "EMI"})

            # 4) Persist record fields
            record.lop_days    = int(lop_days)
            record.working_days = days_in_month
            record.gross_pay   = (total_earnings or Decimal('0')).quantize(Decimal('0.01'))
            record.allowances  = (total_earnings - base_salary).quantize(Decimal('0.01'))
            record.deductions  = total_deductions.quantize(Decimal('0.01'))
            record.employer_pf = employer_pf
            record.esi_amount  = esi_emp
            record.tds_amount  = tds_amount
            record.loan_emi    = loan_emi.quantize(Decimal('0.01'))
            record.net_pay     = (total_earnings - total_deductions - loan_emi - loan_interest).quantize(Decimal('0.01'))
            record.breakdown   = breakdown
            record.status      = 'Processed'
            record.save()

            PayrollAuditLog.objects.create(
                tenant=tenant,
                payroll_record=record,
                action=f"Payroll processed (Processed) — {cycle_month} (inputs/reimb/arrears/loans applied)",
                performed_by=request.user,
            )
            updated += 1

        return Response({'message': 'Next-Level Payroll processing completed', 'updated': updated})


class PayrollSimulateView(views.APIView):
    """
    HR/Admin: simulate a payslip calculation for preview (no DB writes).

    Expected payload (monthly values unless specified):
      - employee_id (optional; if provided we can read statutory flags from Employee)
      - salary_structure_id (optional)
      - ctc (annual, optional) OR base_salary + allowances/hra/bonus/incentives
      - base_salary (monthly)
      - hra/allowances/bonus/incentives (monthly, optional)
      - pf_applicable / esi_applicable (optional; overrides employee flags if provided)
      - tds_applicable / professional_tax (optional)
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        tenant = request.user.tenant
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)

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

        p = request.data or {}
        employee = None
        emp_id = p.get('employee_id')
        if emp_id:
            employee = Employee.objects.filter(tenant=tenant, id=safe_int(emp_id)).first()

        def _dec(val, default='0'):
            try:
                return Decimal(str(val if val is not None else default))
            except Exception:
                return Decimal(default)

        ctc = _dec(p.get('ctc', 0))
        gross_from_ctc = (ctc / Decimal('12')).quantize(Decimal('0.01')) if ctc > 0 else Decimal('0')

        base_salary = _dec(p.get('base_salary', 0)).quantize(Decimal('0.01'))
        hra = _dec(p.get('hra', 0)).quantize(Decimal('0.01'))
        allowances = _dec(p.get('allowances', 0)).quantize(Decimal('0.01'))
        bonus = _dec(p.get('bonus', 0)).quantize(Decimal('0.01'))
        incentives = _dec(p.get('incentives', 0)).quantize(Decimal('0.01'))

        # If base is missing but CTC is given, derive default base = 50% of monthly gross.
        if base_salary <= 0 and gross_from_ctc > 0:
            base_salary = (gross_from_ctc * Decimal('0.50')).quantize(Decimal('0.01'))

        # Attempt to build earnings from structure if provided.
        breakdown = {"earnings": [], "deductions": []}
        total_earnings = Decimal('0')
        statutory_skip_codes = {'PF_EMP', 'PF_EMPLR', 'ESI_EMP', 'ESI_EMPLR', 'PTAX', 'TDS'}

        structure_id = p.get('salary_structure_id') or p.get('structure_id')
        structure = None
        if structure_id:
            structure = SalaryStructure.objects.filter(tenant=tenant, id=safe_int(structure_id)).prefetch_related('components__component').first()

        if structure:
            total_earnings = base_salary
            breakdown["earnings"].append({"name": "Basic", "amount": float(base_salary), "code": "BASIC"})
            for sc in structure.components.all():
                if sc.component.code == 'BASIC':
                    continue
                amount = Decimal('0')
                if sc.calculation_type == 'Fixed':
                    amount = Decimal(sc.value or 0).quantize(Decimal('0.01'))
                elif sc.calculation_type == 'Percentage':
                    amount = (base_salary * Decimal(sc.value or 0) / Decimal('100')).quantize(Decimal('0.01'))
                comp_data = {"name": sc.component.name, "amount": float(amount), "code": sc.component.code}
                if sc.component.component_type == 'Earning':
                    total_earnings += amount
                    breakdown["earnings"].append(comp_data)
                else:
                    if sc.component.code in statutory_skip_codes:
                        continue
                    breakdown["deductions"].append(comp_data)
        else:
            # Use manual breakup (hra/allowances/bonus/incentives) if present; else fallback.
            extra = (hra + allowances + bonus + incentives).quantize(Decimal('0.01'))
            if extra <= 0 and gross_from_ctc > 0:
                # If only CTC is present, treat remaining as allowances.
                extra = (gross_from_ctc - base_salary).quantize(Decimal('0.01'))
            if extra < 0:
                extra = Decimal('0')
            total_earnings = (base_salary + extra).quantize(Decimal('0.01'))
            breakdown["earnings"].append({"name": "Basic", "amount": float(base_salary), "code": "BASIC"})
            breakdown["earnings"].append({"name": "Allowances", "amount": float(extra), "code": "ALLOW"})

        # Statutory flags: payload overrides employee when provided.
        pf_applicable = bool(p.get('pf_applicable')) if 'pf_applicable' in p else bool(getattr(employee, 'pf_applicable', True) if employee else True)
        esi_applicable = bool(p.get('esi_applicable')) if 'esi_applicable' in p else bool(getattr(employee, 'esi_applicable', False) if employee else False)
        tds_applicable = bool(p.get('tds_applicable')) if 'tds_applicable' in p else bool(getattr(employee, 'extended_profile', {}).get('tds_applicable', True) if employee else True)
        pt_applicable = bool(p.get('professional_tax')) if 'professional_tax' in p else bool(getattr(employee, 'extended_profile', {}).get('professional_tax', True) if employee else True)

        total_deductions = Decimal('0')
        employer_pf = Decimal('0')
        esi_emp = Decimal('0')
        tds_amount = Decimal('0')
        ptax = Decimal('0')

        if pf_applicable:
            pf_emp = (base_salary * setting.pf_rate_employee / Decimal('100')).quantize(Decimal('0.01'))
            employer_pf = (base_salary * setting.pf_rate_employer / Decimal('100')).quantize(Decimal('0.01'))
            total_deductions += pf_emp
            breakdown["deductions"].append({"name": "Provident Fund", "amount": float(pf_emp), "code": "PF"})

        if tds_applicable:
            itax = base_salary * (
                Decimal('0.20') if base_salary > 100000 else (Decimal('0.10') if base_salary > 50000 else Decimal('0.05'))
            )
            tds_amount = itax.quantize(Decimal('0.01'))
            total_deductions += tds_amount
            breakdown["deductions"].append({"name": "Income Tax", "amount": float(tds_amount), "code": "ITAX"})

        if pt_applicable:
            ptax = Decimal('200') if base_salary > 15000 else Decimal('0')
            if ptax > 0:
                total_deductions += ptax
                breakdown["deductions"].append({"name": "Professional Tax", "amount": float(ptax), "code": "PTAX"})

        if esi_applicable and total_earnings <= Decimal('21000'):
            esi_emp = (total_earnings * setting.esi_rate_employee / Decimal('100')).quantize(Decimal('0.01'))
            total_deductions += esi_emp
            breakdown["deductions"].append({"name": "ESI (Employee)", "amount": float(esi_emp), "code": "ESI"})

        net_pay = (total_earnings - total_deductions).quantize(Decimal('0.01'))
        return Response({
            "gross_pay": float(total_earnings),
            "total_deductions": float(total_deductions),
            "net_pay": float(net_pay),
            "statutory": {
                "pf_employee": float((base_salary * setting.pf_rate_employee / Decimal('100')).quantize(Decimal('0.01'))) if pf_applicable else 0,
                "pf_employer": float(employer_pf),
                "esi_employee": float(esi_emp),
                "tds": float(tds_amount),
                "ptax": float(ptax),
            },
            "breakdown": breakdown,
        })


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
        effective_from = request.data.get('effective_from')
        
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
        
        # Parse effective_from if provided (YYYY-MM-DD), else default to today.
        eff = timezone.localdate()
        if effective_from:
            try:
                from datetime import date
                if isinstance(effective_from, date):
                    eff = effective_from
                else:
                    eff = date.fromisoformat(str(effective_from))
            except Exception:
                eff = timezone.localdate()

        EmployeeSalaryStructure.objects.update_or_create(
            tenant=tenant,
            employee_id=emp_id,
            defaults={
                'structure_id': struct_id,
                'effective_from': eff,
                'is_active': True
            }
        )
        return Response({"message": "Employee salary structure updated"})

class PayslipView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]
    def get(self, request, record_id):
        sr = getattr(request.user, 'system_role', None)
        pk = safe_int(record_id)
        if pk is None:
            return Response({"error": "Invalid payslip identifier."}, status=400)

        tenant = request.user.tenant
        record_qs = PayrollRecord.objects.select_related("employee", "employee__department", "employee__designation")
        if tenant and sr not in ['SUPER_ADMIN', 'ADMIN']:
            record_qs = record_qs.filter(tenant=tenant)
            
        record = record_qs.filter(id=pk).first()
        if record is None:
            cycle_hint = (request.query_params.get("cycle") or "").strip()[:7]
            qs = PayrollRecord.objects.select_related(
                "employee", "employee__department", "employee__designation"
            )
            if tenant and sr not in ['SUPER_ADMIN', 'ADMIN']:
                qs = qs.filter(tenant=tenant)
            
            qs = qs.filter(employee_id=pk)
            if cycle_hint:
                qs = qs.filter(cycle_month=cycle_hint)
            record = qs.order_by("-cycle_month", "-id").first()

        if record is None:
            return Response(
                {
                    "error": f"Payslip/Payroll Record not found for ID '{pk}'.",
                    "details": {
                        "tenant_id": getattr(tenant, 'id', None),
                        "hint": "Ensure the ID is a valid PayrollRecord ID, or provide an Employee ID with ?cycle=YYYY-MM."
                    }
                },
                status=404,
            )

        out_format = (request.query_params.get('format') or 'json').lower().strip()

        sr = getattr(request.user, 'system_role', None)
        if sr in ['SUPER_ADMIN', 'ADMIN']:
            pass
        elif sr in ['HR', 'MANAGER']:
            if not _is_employee_in_scope(request, record.employee):
                return _forbidden_employee_access("You are not authorized to access this payslip.")
        else:
            actor_emp = _resolve_actor_employee(request)
            if not actor_emp or int(actor_emp.id) != int(record.employee_id):
                return _forbidden_employee_access("You are not authorized to access this payslip.")

        emp = record.employee
        # Calculate Previous Month for attendance lookback
        try:
            _y, _m = map(int, str(record.cycle_month).split('-'))
            _cur = datetime(_y, _m, 1)
            _prev = _cur - timedelta(days=1)
            attendance_cycle = _prev.strftime('%Y-%m')
        except Exception:
            attendance_cycle = record.cycle_month

        present_days = AttendanceRecord.objects.filter(
            employee=emp,
            date__startswith=attendance_cycle,
            status__code__in=['P', 'PRESENT', 'L', 'LATE']
        ).count()

        payload = {
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
                "present_days":   present_days,
                "lop_days":       getattr(record, 'lop_days', 0),
                "paid_days":      getattr(record, 'working_days', 30) - getattr(record, 'lop_days', 0),
                "period":         attendance_cycle
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
        }

        if out_format == 'pdf':
            if WeasyHTML is None:
                return Response({"error": "PDF export unavailable (WeasyPrint not installed)"}, status=501)
            try:
                pdf_bytes = _render_payslip_pdf_bytes(payload, request.user.tenant)
            except Exception as exc:
                return Response({"error": f"Failed to generate payslip PDF: {str(exc)}"}, status=500)
            resp = HttpResponse(pdf_bytes, content_type='application/pdf')
            emp_code = slugify(str(emp.employee_code or emp.name or 'EMP')).upper().replace('-', '_') or 'EMP'
            resp['Content-Disposition'] = f'attachment; filename="payslip_{emp_code}_{record.cycle_month}.pdf"'
            resp['Access-Control-Expose-Headers'] = 'Content-Disposition'
            return resp

        return Response(payload)


def _build_payslip_payload_for_record(record: PayrollRecord, tenant_name: str):
    """
    Build a single payslip payload (JSON) for exports.
    Note: This is intentionally aligned with PayslipView output.
    """
    emp = record.employee

    try:
        _y, _m = map(int, str(record.cycle_month).split('-'))
        _cur = datetime(_y, _m, 1)
        _prev = _cur - timedelta(days=1)
        attendance_cycle = _prev.strftime('%Y-%m')
    except Exception:
        attendance_cycle = record.cycle_month

    present_days = AttendanceRecord.objects.filter(
        employee=emp,
        date__startswith=attendance_cycle,
        status__code__in=['P', 'PRESENT', 'L', 'LATE']
    ).count()

    return {
        "company": {"name": tenant_name},
        "employee": {
            "name": emp.name,
            "code": emp.employee_code,
            "department": emp.department.name if emp.department else 'N/A',
            "designation": emp.designation.name if getattr(emp, 'designation', None) else 'N/A',
            "joining_date": emp.joining_date.strftime('%Y-%m-%d') if emp.joining_date else None,
            "bank_name": emp.bank_name,
            "account_number": emp.account_number,
            "ifsc_code": emp.ifsc_code,
            "pan_number": getattr(emp, 'pan_number', None),
            "uan_number": getattr(emp, 'uan_number', None),
            "tax_regime": getattr(emp, 'tax_regime', 'New'),
        },
        "attendance": {
            "working_days": getattr(record, 'working_days', 30),
            "present_days": present_days,
            "lop_days": getattr(record, 'lop_days', 0),
            "paid_days": getattr(record, 'working_days', 30) - getattr(record, 'lop_days', 0),
            "period": attendance_cycle
        },
        "salary": {
            "id": record.id,
            "cycle": record.cycle_month,
            "status": record.status,
            "base_salary": float(record.base_salary),
            "gross_pay": float(getattr(record, 'gross_pay', 0)),
            "total_deductions": float(getattr(record, 'deductions', 0)) + float(getattr(record, 'loan_emi', 0)) + float(getattr(record, 'tds_amount', 0)) + float(getattr(record, 'esi_amount', 0)),
            "net_pay": float(record.net_pay),
            "breakdown": record.breakdown or {"earnings": [], "deductions": []},
            "adjustments": getattr(record, 'one_time_adjustments', []),
            "payment_reference": getattr(record, 'payment_reference', None),
        }
    }


def _render_payslip_pdf_bytes(payload: dict, tenant: Tenant) -> bytes:
    """
    Render a payslip PDF from the HTML template.

    The HTML approach is easier to style and keeps the same payload shared
    across the single-record download and the ZIP export.
    """
    if WeasyHTML is None:
        raise RuntimeError("WeasyPrint is unavailable")

    tenant_name = getattr(tenant, "name", "Company") or "Company"
    tenant_logo_url = ""
    tenant_logo = getattr(tenant, "logo", None)
    if tenant_logo:
        try:
            tenant_logo_url = Path(tenant_logo.path).resolve().as_uri()
        except Exception:
            tenant_logo_url = ""
    tenant_initials = "".join(
        part[:1].upper()
        for part in tenant_name.replace("&", " ").split()
        if part[:1].isalpha()
    )[:3] or "CO"
    template_html = render_to_string(
        "api/payslip_pdf.html",
        {
            "tenant": {
                "name": tenant_name,
                "initials": tenant_initials,
                "logo_url": tenant_logo_url,
                "address": (getattr(tenant, "address", None) or "").strip(),
                "phone": (getattr(tenant, "phone", None) or "").strip(),
                "gst_number": (getattr(tenant, "gst_number", None) or "").strip(),
                "pan_number": (getattr(tenant, "pan_number", None) or "").strip(),
            },
            "payload": payload,
        },
    )
    return WeasyHTML(string=template_html, base_url=str(settings.BASE_DIR)).write_pdf()


class PayrollBankAdviceExportView(views.APIView):
    """
    GET /api/payroll/bank-advice/?cycle=YYYY-MM
    Returns a CSV suitable for bank NEFT upload templates (base fields only).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        cycle = request.query_params.get('cycle')
        out_format = (request.query_params.get('format') or 'xlsx').lower().strip()
        if not cycle:
            return Response({"error": "cycle query param is required (YYYY-MM)"}, status=400)

        tenant = request.user.tenant
        qs = PayrollRecord.objects.filter(tenant=tenant, cycle_month=cycle).select_related('employee').order_by('employee__employee_code')

        if out_format == 'xlsx':
            if Workbook is None:
                return Response({"error": "XLSX export unavailable (openpyxl not installed)"}, status=501)

            wb = Workbook()
            ws = wb.active
            ws.title = "Bank Advice"
            ws.append([
                "cycle_month",
                "employee_code",
                "employee_name",
                "bank_name",
                "account_number",
                "ifsc_code",
                "net_pay",
                "payment_reference",
            ])
            for r in qs:
                e = r.employee
                ws.append([
                    r.cycle_month,
                    getattr(e, 'employee_code', '') or '',
                    getattr(e, 'name', '') or '',
                    getattr(e, 'bank_name', '') or '',
                    getattr(e, 'account_number', '') or '',
                    getattr(e, 'ifsc_code', '') or '',
                    float(getattr(r, 'net_pay', 0) or 0),
                    getattr(r, 'payment_reference', '') or '',
                ])

            bio = io.BytesIO()
            wb.save(bio)
            resp = HttpResponse(
                bio.getvalue(),
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            resp["Content-Disposition"] = f'attachment; filename="bank_advice_{cycle}.xlsx"'
            return resp

        buff = io.StringIO()
        writer = csv.writer(buff)
        writer.writerow([
            "cycle_month",
            "employee_code",
            "employee_name",
            "bank_name",
            "account_number",
            "ifsc_code",
            "net_pay",
            "payment_reference",
        ])

        for r in qs:
            e = r.employee
            writer.writerow([
                r.cycle_month,
                getattr(e, 'employee_code', '') or '',
                getattr(e, 'name', '') or '',
                getattr(e, 'bank_name', '') or '',
                getattr(e, 'account_number', '') or '',
                getattr(e, 'ifsc_code', '') or '',
                float(getattr(r, 'net_pay', 0) or 0),
                getattr(r, 'payment_reference', '') or '',
            ])

        resp = HttpResponse(buff.getvalue(), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="bank_advice_{cycle}.csv"'
        return resp


class PayrollPayslipsDownloadView(views.APIView):
    """
    GET /api/payroll/payslips/download/?cycle=YYYY-MM
    Returns a ZIP of per-employee payslip payloads rendered as JSON or PDF.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        cycle = request.query_params.get('cycle')
        out_format = (request.query_params.get('format') or 'pdf').lower().strip()
        if not cycle:
            return Response({"error": "cycle query param is required (YYYY-MM)"}, status=400)

        tenant = request.user.tenant
        tenant_name = getattr(tenant, 'name', 'Company')
        qs = PayrollRecord.objects.filter(tenant=tenant, cycle_month=cycle).select_related('employee').order_by('employee__employee_code')

        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            manifest = {"cycle": cycle, "count": qs.count(), "generated_at": timezone.now().isoformat()}
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))

            for r in qs:
                e = r.employee
                code = (getattr(e, 'employee_code', '') or 'EMP').strip() or 'EMP'
                safe_code = slugify(code).upper().replace('-', '_')[:40] or "EMP"
                payload = _build_payslip_payload_for_record(r, tenant_name)

                if out_format == 'json':
                    zf.writestr(f"payslips/{safe_code}_{cycle}.json", json.dumps(payload, indent=2))
                    continue

                if out_format != 'pdf':
                    return Response({"error": "Invalid format. Use format=pdf or format=json"}, status=400)
                if WeasyHTML is None:
                    return Response({"error": "PDF export unavailable (WeasyPrint not installed)"}, status=501)

                try:
                    pdf_bytes = _render_payslip_pdf_bytes(payload, tenant)
                except Exception as e:
                    return Response({"error": f"Failed to render PDF: {str(e)}"}, status=500)
                zf.writestr(f"payslips/{safe_code}_{cycle}.pdf", pdf_bytes)

        resp = HttpResponse(out.getvalue(), content_type="application/zip")
        suffix = "pdf" if out_format == "pdf" else "json"
        resp["Content-Disposition"] = f'attachment; filename="payslips_{cycle}_{suffix}.zip"'
        return resp

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

class AttendanceBulkView(views.APIView):
    """
    POST /api/attendance/bulk/
    Body: { date: "YYYY-MM-DD", records: [{ employee_id, status, check_in, check_out, work_hours }] }
    Upserts all records atomically. Returns { saved, errors }.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        sr = getattr(request.user, 'system_role', None)
        if sr not in ['ADMIN', 'SUPER_ADMIN', 'HR', 'MANAGER']:
            return Response({"error": "Permission denied"}, status=403)

        tenant = request.user.tenant
        date = request.data.get('date')
        records = request.data.get('records', [])

        if not date:
            return Response({"error": "date is required"}, status=400)
        if not isinstance(records, list) or len(records) == 0:
            return Response({"error": "records[] must be a non-empty list"}, status=400)

        # Prevent edits for payroll cycles with attendance lock enabled.
        cycle_month = str(date)[:7]
        try:
            from django.apps import apps
            PayrollCycleLockModel = apps.get_model('api', 'PayrollCycleLock')
            cycle_lock = PayrollCycleLockModel.objects.filter(tenant=tenant, cycle_month=cycle_month).first()
            if cycle_lock and bool(getattr(cycle_lock, 'attendance_locked', False)):
                return Response({
                    "error": f"Attendance is locked for cycle {cycle_month}. Unlock attendance before updating."
                }, status=403)
        except Exception:
            # Keep backward compatibility if workflow model/table is unavailable.
            pass

        # Pre-fetch all status objects to avoid N+1 queries
        all_statuses = {s.code.upper(): s for s in AttendanceStatus.objects.all()}

        saved = []
        errors = []

        try:
            with transaction.atomic():
                for item in records:
                    emp_id = item.get('employee_id')
                    status_code = str(item.get('status') or 'A').strip().upper()
                    check_in = item.get('check_in') or None
                    check_out = item.get('check_out') or None
                    work_hours = item.get('work_hours')
                    location = item.get('location') or 'Office'

                    status_obj = all_statuses.get(status_code)
                    if not status_obj:
                        errors.append({"employee_id": emp_id, "error": f"Unknown status '{status_code}'"})
                        continue

                    # Default work_hours from status master if not provided
                    if work_hours is None:
                        work_hours = getattr(status_obj, 'default_work_hours', None)
                    try:
                        work_hours = float(work_hours) if work_hours is not None else 0.0
                    except (ValueError, TypeError):
                        work_hours = 0.0

                    # Derive check-in/out defaults from status master if not provided
                    if not check_in and status_obj.default_check_in:
                        check_in = status_obj.default_check_in.strftime('%H:%M:%S')
                    if not check_out and status_obj.default_check_out:
                        check_out = status_obj.default_check_out.strftime('%H:%M:%S')

                    try:
                        rec, _ = AttendanceRecord.objects.update_or_create(
                            tenant=tenant,
                            employee_id=emp_id,
                            date=date,
                            defaults={
                                'status': status_obj,
                                'status_str': status_obj.code,
                                'check_in': check_in,
                                'check_out': check_out,
                                'work_hours': work_hours,
                                'location': location,
                            },
                        )
                        saved.append({"employee_id": emp_id, "record_id": rec.id})
                    except Exception as row_err:
                        errors.append({"employee_id": emp_id, "error": str(row_err)})
        except Exception as e:
            return Response({"error": str(e)}, status=500)

        return Response({
            "saved": len(saved),
            "errors": errors,
            "total": len(records),
            "records": saved,
        })


class AttendanceBulkGenerateAbsentView(views.APIView):
    """
    POST /api/attendance/bulk/generate-absent/
    Body: { date: "YYYY-MM-DD" }
    Creates Absent records for every active employee who has no record on that date.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        sr = getattr(request.user, 'system_role', None)
        if sr not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)

        tenant = request.user.tenant
        date = request.data.get('date')
        if not date:
            return Response({"error": "date is required"}, status=400)

        absent_status = AttendanceStatus.objects.filter(code='A').first()
        if not absent_status:
            return Response({"error": "Absent status not configured"}, status=500)

        # Employees that already have a record on this date
        existing_ids = set(
            AttendanceRecord.objects.filter(tenant=tenant, date=date)
            .values_list('employee_id', flat=True)
        )

        # All active employees without a record
        employees_to_fill = Employee.objects.filter(
            tenant=tenant,
            status='Active',
        ).exclude(id__in=existing_ids)

        new_records = [
            AttendanceRecord(
                tenant=tenant,
                employee=emp,
                date=date,
                status=absent_status,
                check_in=None,
                check_out=None,
                work_hours=0.0,
                location='N/A',
            )
            for emp in employees_to_fill
        ]

        try:
            AttendanceRecord.objects.bulk_create(new_records, ignore_conflicts=True)
        except Exception as e:
            return Response({"error": str(e)}, status=500)

        return Response({
            "message": f"Generated absent for {len(new_records)} employee(s)",
            "created": len(new_records),
            "date": date,
        })


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
        # Ensure evolving schema exists (legacy environments may skip migrations)
        try:
            ensure_master_tables_exist()
        except Exception:
            pass

        tenant = request.user.tenant
        record_id   = request.data.get('record_id')
        employee_id = request.data.get('employee_id')
        target_date = request.data.get('date')
        status_val  = request.data.get('status')
        reason      = request.data.get('reason') or request.data.get('comment') or ''

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

            def _code_from_record(rec):
                try:
                    if getattr(rec, 'status', None) and getattr(rec.status, 'code', None):
                        return str(rec.status.code).strip().upper()
                except Exception:
                    pass
                try:
                    return str(getattr(rec, 'status_str', '') or '').strip().upper()
                except Exception:
                    return ''

            def _needs_comment(prev_code: str, next_code: str) -> bool:
                prev_code = str(prev_code or '').strip().upper()
                next_code = str(next_code or '').strip().upper()
                is_lop = prev_code == 'LOP' or next_code == 'LOP'
                is_absent_to_present = prev_code == 'A' and next_code in ('P', 'L')
                return is_lop or is_absent_to_present

            next_code = str(getattr(status_obj, 'code', '') or status_val or '').strip().upper()

            # Load current record (if exists) for comment validation + audit
            record = None
            if record_id:
                record = AttendanceRecord.objects.get(tenant=tenant, id=record_id)
                employee_id = employee_id or record.employee_id
                target_date = target_date or (record.date.isoformat() if record.date else None)
            elif employee_id and target_date:
                record = AttendanceRecord.objects.filter(tenant=tenant, employee_id=employee_id, date=target_date).select_related('status').first()
            else:
                return Response({"error": "Provide record_id OR (employee_id + date)"}, status=400)

            prev_code = _code_from_record(record) if record else ''
            if _needs_comment(prev_code, next_code) and not str(reason or '').strip():
                return Response(
                    {"error": "Reason is required for Absent→Present/Late or any LOP change."},
                    status=400,
                )

            # Two-stage approval: MANAGER → HR/Admin.
            # HR/Admin/SuperAdmin bypass stage 1 and auto-approve immediately.
            # MANAGER requests go PENDING_MANAGER first, then PENDING_HR after manager approves.
            # EMPLOYEE (via ESS) or any other role: PENDING_MANAGER.
            auto_approve = request.user.system_role in ['ADMIN', 'SUPER_ADMIN', 'HR']
            state = 'APPROVED' if auto_approve else 'PENDING_MANAGER'

            # Insert request row (audit trail)
            req_id = None
            try:
                with connection.cursor() as cursor:
                    vendor = getattr(connection, "vendor", "")
                    requested_at = timezone.now()
                    if vendor == "sqlite":
                        cursor.execute(
                            """
                            INSERT INTO t_attendance_regularization_request
                              (tenant_id, employee_id, date, requested_status_id, requested_check_in, requested_check_out,
                               requested_work_hours, reason, state, requested_by_id, requested_at,
                               reviewed_by_id, reviewed_at, review_comment)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            [
                                str(tenant.id),
                                int(employee_id),
                                str(target_date),
                                int(status_obj.id) if status_obj else None,
                                check_in,
                                check_out,
                                float(work_hours) if work_hours is not None else None,
                                str(reason or '')[:2000],
                                state,
                                int(request.user.id) if request.user and request.user.id else None,
                                str(requested_at),
                                int(request.user.id) if auto_approve else None,
                                str(requested_at) if auto_approve else None,
                                str(reason or '')[:2000] if auto_approve else None,
                            ],
                        )
                        cursor.execute("SELECT last_insert_rowid()")
                        req_id = cursor.fetchone()[0]
                    else:
                        cursor.execute(
                            """
                            INSERT INTO t_attendance_regularization_request
                              (tenant_id, employee_id, date, requested_status_id, requested_check_in, requested_check_out,
                               requested_work_hours, reason, state, requested_by_id, requested_at,
                               reviewed_by_id, reviewed_at, review_comment)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            [
                                tenant.id,
                                int(employee_id),
                                target_date,
                                int(status_obj.id) if status_obj else None,
                                check_in,
                                check_out,
                                float(work_hours) if work_hours is not None else None,
                                str(reason or '')[:2000],
                                state,
                                request.user.id if request.user and request.user.id else None,
                                requested_at,
                                request.user.id if auto_approve else None,
                                requested_at if auto_approve else None,
                                str(reason or '')[:2000] if auto_approve else None,
                            ],
                        )
                        req_id = cursor.lastrowid
            except Exception:
                req_id = None

            if not auto_approve:
                return Response({
                    "message": "Regularization submitted for manager approval",
                    "state": state,
                    "request_id": req_id,
                }, status=202)

            # Auto-approve path: apply changes to AttendanceRecord (upsert)
            if record_id and record:
                if status_obj:
                    record.status = status_obj
                record.check_in = check_in
                record.check_out = check_out
                record.work_hours = work_hours if work_hours is not None else 0.0
                try:
                    record.regularization_reason = str(reason or '')[:2000]
                except Exception:
                    pass
                record.save()
            else:
                rec, created = AttendanceRecord.objects.get_or_create(
                    tenant=tenant,
                    employee_id=employee_id,
                    date=target_date,
                    defaults={
                        'status': status_obj,
                        'check_in': check_in,
                        'check_out': check_out,
                        'work_hours': work_hours if work_hours is not None else 9.0,
                        'location': 'Office',
                    },
                )
                if not created:
                    if status_obj:
                        rec.status = status_obj
                    rec.check_in = check_in
                    rec.check_out = check_out
                    rec.work_hours = work_hours if work_hours is not None else 0.0
                try:
                    rec.regularization_reason = str(reason or '')[:2000]
                except Exception:
                    pass
                rec.save()

            return Response({"message": "Attendance regularized successfully", "state": state, "request_id": req_id})
        except AttendanceRecord.DoesNotExist:
            return Response({"error": "Record not found"}, status=404)
        except Exception as e:
            return Response({"error": str(e)}, status=500)


class AttendanceRegularizationRequestsView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            ensure_master_tables_exist()
        except Exception:
            pass

        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR', 'MANAGER']:
            return Response({"error": "Permission denied"}, status=403)

        tenant = request.user.tenant
        # Accept comma-separated states e.g. "PENDING_MANAGER,PENDING_HR" or keep backward compat
        raw_state = (request.query_params.get('state') or 'PENDING_MANAGER').strip().upper()
        # Map legacy "PENDING" → "PENDING_MANAGER" for backward compat
        if raw_state == 'PENDING':
            raw_state = 'PENDING_MANAGER'
        states = [s.strip() for s in raw_state.split(',') if s.strip()]

        employee_id = request.query_params.get('employee_id')
        month = request.query_params.get('month')  # YYYY-MM optional
        mine = str(request.query_params.get('mine') or '').strip().lower() in ['1', 'true', 'yes']
        sr = getattr(request.user, 'system_role', None)

        # Managers should only see requests of their direct reports unless mine=1 (then only their own requests).
        manager_id = None
        if sr == 'MANAGER':
            try:
                manager_id = getattr(request.user, 'employee_profile', None).id
            except Exception:
                manager_id = None

        vendor = getattr(connection, "vendor", "")
        state_placeholders_mysql = ', '.join(['%s'] * len(states))
        state_placeholders_sqlite = ', '.join(['?'] * len(states))

        clauses = [f"r.tenant_id = %s", f"UPPER(r.state) IN ({state_placeholders_mysql})"]
        params = [tenant.id, *states]
        if employee_id:
            clauses.append("r.employee_id = %s")
            params.append(int(employee_id))
        if month:
            clauses.append("DATE_FORMAT(r.date, '%%Y-%%m') = %s")
            params.append(str(month))
        if mine:
            clauses.append("r.requested_by_id = %s")
            params.append(int(request.user.id))
        if sr == 'MANAGER' and (not mine) and manager_id and (not employee_id):
            clauses.append("e.reporting_to_id = %s")
            params.append(int(manager_id))

        with connection.cursor() as cursor:
            if vendor == "sqlite":
                # SQLite doesn't have DATE_FORMAT; treat month filter as prefix.
                clauses_sql = [f"r.tenant_id = ?", f"UPPER(r.state) IN ({state_placeholders_sqlite})"]
                params_sql = [str(tenant.id), *states]
                if employee_id:
                    clauses_sql.append("r.employee_id = ?")
                    params_sql.append(int(employee_id))
                if month:
                    clauses_sql.append("substr(r.date, 1, 7) = ?")
                    params_sql.append(str(month))
                if mine:
                    clauses_sql.append("r.requested_by_id = ?")
                    params_sql.append(int(request.user.id))
                if sr == 'MANAGER' and (not mine) and manager_id and (not employee_id):
                    clauses_sql.append("e.reporting_to_id = ?")
                    params_sql.append(int(manager_id))
                where = " AND ".join(clauses_sql)
                cursor.execute(
                    f"""
                    SELECT r.id, r.employee_id, e.name, r.date,
                           r.requested_status_id, s.code, s.label,
                           r.requested_check_in, r.requested_check_out, r.requested_work_hours,
                           r.reason, r.state, r.requested_at,
                           r.manager_reviewed_by_id, mu.name, r.manager_reviewed_at, r.manager_review_comment,
                           r.reviewed_by_id, hu.name, r.reviewed_at, r.review_comment
                    FROM t_attendance_regularization_request r
                    LEFT JOIN t_employee e ON e.id = r.employee_id
                    LEFT JOIN t_attendance_status s ON s.id = r.requested_status_id
                    LEFT JOIN t_employee mu ON mu.id = r.manager_reviewed_by_id
                    LEFT JOIN t_employee hu ON hu.id = r.reviewed_by_id
                    WHERE {where}
                    ORDER BY r.requested_at DESC
                    LIMIT 500
                    """,
                    params_sql,
                )
            else:
                where = " AND ".join(clauses)
                cursor.execute(
                    f"""
                    SELECT r.id, r.employee_id, e.name, r.date,
                           r.requested_status_id, s.code, s.label,
                           r.requested_check_in, r.requested_check_out, r.requested_work_hours,
                           r.reason, r.state, r.requested_at,
                           r.manager_reviewed_by_id, mu.name, r.manager_reviewed_at, r.manager_review_comment,
                           r.reviewed_by_id, hu.name, r.reviewed_at, r.review_comment
                    FROM t_attendance_regularization_request r
                    LEFT JOIN t_employee e ON e.id = r.employee_id
                    LEFT JOIN t_attendance_status s ON s.id = r.requested_status_id
                    LEFT JOIN t_employee mu ON mu.id = r.manager_reviewed_by_id
                    LEFT JOIN t_employee hu ON hu.id = r.reviewed_by_id
                    WHERE {where}
                    ORDER BY r.requested_at DESC
                    LIMIT 500
                    """,
                    params,
                )
            rows = cursor.fetchall()

        data = []
        for r in rows:
            data.append({
                "id": r[0],
                "employee_id": r[1],
                "employee_name": r[2] or "",
                "date": str(r[3]) if r[3] else None,
                "requested_status_id": r[4],
                "requested_status_code": (r[5] or ""),
                "requested_status_label": (r[6] or ""),
                "requested_check_in": str(r[7]) if r[7] else "",
                "requested_check_out": str(r[8]) if r[8] else "",
                "requested_work_hours": float(r[9]) if r[9] is not None else None,
                "reason": r[10] or "",
                "state": r[11] or "",
                "requested_at": str(r[12]) if r[12] else None,
                "manager_reviewed_by_id": r[13],
                "manager_reviewed_by_name": r[14] or "",
                "manager_reviewed_at": str(r[15]) if r[15] else None,
                "manager_review_comment": r[16] or "",
                "hr_reviewed_by_id": r[17],
                "hr_reviewed_by_name": r[18] or "",
                "hr_reviewed_at": str(r[19]) if r[19] else None,
                "hr_review_comment": r[20] or "",
            })
        return Response({"requests": data})


class AttendanceRegularizationApproveView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        try:
            ensure_master_tables_exist()
        except Exception:
            pass

        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR', 'MANAGER']:
            return Response({"error": "Permission denied"}, status=403)

        tenant = request.user.tenant
        req_id = request.data.get('request_id')
        action = (request.data.get('action') or '').strip().lower()
        review_comment = request.data.get('comment') or request.data.get('reason') or ''

        if not req_id:
            return Response({"error": "request_id is required"}, status=400)
        if action not in ['approve', 'reject']:
            return Response({"error": "action must be approve|reject"}, status=400)

        vendor = getattr(connection, "vendor", "")
        with connection.cursor() as cursor:
            # Load request row
            if vendor == "sqlite":
                cursor.execute(
                    """
                    SELECT id, employee_id, date, requested_status_id, requested_check_in, requested_check_out, requested_work_hours, reason, state
                    FROM t_attendance_regularization_request
                    WHERE tenant_id = ? AND id = ?
                    """,
                    [str(tenant.id), int(req_id)],
                )
            else:
                cursor.execute(
                    """
                    SELECT id, employee_id, date, requested_status_id, requested_check_in, requested_check_out, requested_work_hours, reason, state
                    FROM t_attendance_regularization_request
                    WHERE tenant_id = %s AND id = %s
                    """,
                    [tenant.id, int(req_id)],
                )
            row = cursor.fetchone()

        if not row:
            return Response({"error": "Request not found"}, status=404)

        current_state = str(row[8] or '').upper()
        sr = getattr(request.user, 'system_role', None)

        employee_id = row[1]
        target_date = row[2]
        requested_status_id = row[3]
        check_in = row[4]
        check_out = row[5]
        work_hours = row[6]
        fallback_reason = row[7] or ''
        reviewed_at = timezone.now()
        review_comment = str(review_comment or fallback_reason or '')[:2000]

        # ── Two-stage state machine ─────────────────────────────────────────
        # Stage 1: MANAGER acts on PENDING_MANAGER
        #   approve → PENDING_HR   (record manager decision, wait for HR)
        #   reject  → REJECTED
        # Stage 2: HR/Admin acts on PENDING_HR
        #   approve → APPROVED     (apply correction)
        #   reject  → REJECTED
        # HR/Admin can also directly reject a PENDING_MANAGER request.
        # ───────────────────────────────────────────────────────────────────

        VALID_STATES = {'PENDING_MANAGER', 'PENDING_HR'}
        if current_state not in VALID_STATES:
            return Response({"error": f"Request is already {current_state} and cannot be actioned."}, status=400)

        # Authorization & stage routing
        if sr == 'MANAGER':
            # Manager can only act on PENDING_MANAGER
            if current_state != 'PENDING_MANAGER':
                return Response({"error": "Manager can only act on requests awaiting manager approval."}, status=403)
            # Must be the direct reporting manager
            try:
                mgr = request.user.employee_profile
            except Exception:
                return Response({"error": "Manager profile not found"}, status=403)
            try:
                emp = Employee.objects.filter(tenant=tenant, id=int(employee_id)).first()
            except Exception:
                emp = None
            if (not emp) or (getattr(emp, 'reporting_to_id', None) != getattr(mgr, 'id', None)):
                return Response({"error": "You are not authorized to approve this request."}, status=403)

            new_state = 'PENDING_HR' if action == 'approve' else 'REJECTED'
            vendor = getattr(connection, "vendor", "")
            with connection.cursor() as cursor:
                if vendor == "sqlite":
                    cursor.execute(
                        """
                        UPDATE t_attendance_regularization_request
                        SET state = ?,
                            manager_reviewed_by_id = ?, manager_reviewed_at = ?, manager_review_comment = ?
                        WHERE tenant_id = ? AND id = ?
                        """,
                        [new_state, int(request.user.id), str(reviewed_at), review_comment, str(tenant.id), int(req_id)],
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE t_attendance_regularization_request
                        SET state = %s,
                            manager_reviewed_by_id = %s, manager_reviewed_at = %s, manager_review_comment = %s
                        WHERE tenant_id = %s AND id = %s
                        """,
                        [new_state, request.user.id, reviewed_at, review_comment, tenant.id, int(req_id)],
                    )
            if action == 'reject':
                return Response({"message": "Rejected by manager", "state": new_state})
            return Response({"message": "Forwarded to HR for final approval", "state": new_state})

        # HR / Admin / SuperAdmin path
        if sr not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)

        # HR/Admin can act on PENDING_HR only (or optionally reject PENDING_MANAGER directly)
        if current_state == 'PENDING_MANAGER' and action == 'approve':
            return Response(
                {"error": "This request is still awaiting manager approval. HR can only approve after the manager has reviewed."},
                status=400,
            )

        new_state = 'APPROVED' if action == 'approve' else 'REJECTED'
        vendor = getattr(connection, "vendor", "")
        with connection.cursor() as cursor:
            if vendor == "sqlite":
                cursor.execute(
                    """
                    UPDATE t_attendance_regularization_request
                    SET state = ?, reviewed_by_id = ?, reviewed_at = ?, review_comment = ?
                    WHERE tenant_id = ? AND id = ?
                    """,
                    [new_state, int(request.user.id), str(reviewed_at), review_comment, str(tenant.id), int(req_id)],
                )
            else:
                cursor.execute(
                    """
                    UPDATE t_attendance_regularization_request
                    SET state = %s, reviewed_by_id = %s, reviewed_at = %s, review_comment = %s
                    WHERE tenant_id = %s AND id = %s
                    """,
                    [new_state, request.user.id, reviewed_at, review_comment, tenant.id, int(req_id)],
                )

        if action == 'reject':
            return Response({"message": "Rejected by HR", "state": new_state})

        # Apply correction to AttendanceRecord
        status_obj = AttendanceStatus.objects.filter(pk=requested_status_id).first() if requested_status_id else None
        rec, created = AttendanceRecord.objects.get_or_create(
            tenant=tenant,
            employee_id=employee_id,
            date=target_date,
            defaults={
                'status': status_obj,
                'check_in': check_in,
                'check_out': check_out,
                'work_hours': float(work_hours) if work_hours is not None else 0.0,
                'location': 'Office',
            },
        )
        if not created:
            if status_obj:
                rec.status = status_obj
            rec.check_in = check_in
            rec.check_out = check_out
            rec.work_hours = float(work_hours) if work_hours is not None else 0.0
        try:
            rec.regularization_reason = review_comment
        except Exception:
            pass
        rec.save()

        return Response({"message": "Approved and applied", "state": new_state, "record_id": rec.id})


# ─────────────────────────────────────────────
# Comp Off — requests & approvals
# ─────────────────────────────────────────────
class CompOffRequestsView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            ensure_master_tables_exist()
        except Exception:
            pass
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR', 'MANAGER']:
            return Response({"error": "Permission denied"}, status=403)

        tenant = request.user.tenant
        state = (request.query_params.get('state') or 'PENDING').strip().upper()
        employee_id = request.query_params.get('employee_id')

        vendor = getattr(connection, "vendor", "")
        with connection.cursor() as cursor:
            if vendor == "sqlite":
                params = [str(tenant.id), state]
                where = "r.tenant_id = ? AND UPPER(r.state) = ?"
                if employee_id:
                    where += " AND r.employee_id = ?"
                    params.append(int(employee_id))
                cursor.execute(
                    f"""
                    SELECT r.id, r.employee_id, e.name, r.worked_date, r.credit_days, r.reason, r.state, r.requested_at
                    FROM t_comp_off_request r
                    LEFT JOIN t_employee e ON e.id = r.employee_id
                    WHERE {where}
                    ORDER BY r.requested_at DESC
                    LIMIT 500
                    """,
                    params,
                )
            else:
                params = [tenant.id, state]
                where = "r.tenant_id = %s AND UPPER(r.state) = %s"
                if employee_id:
                    where += " AND r.employee_id = %s"
                    params.append(int(employee_id))
                cursor.execute(
                    f"""
                    SELECT r.id, r.employee_id, e.name, r.worked_date, r.credit_days, r.reason, r.state, r.requested_at
                    FROM t_comp_off_request r
                    LEFT JOIN t_employee e ON e.id = r.employee_id
                    WHERE {where}
                    ORDER BY r.requested_at DESC
                    LIMIT 500
                    """,
                    params,
                )
            rows = cursor.fetchall()

        data = []
        for r in rows:
            data.append({
                "id": r[0],
                "employee_id": r[1],
                "employee_name": r[2] or "",
                "worked_date": str(r[3]) if r[3] else None,
                "credit_days": float(r[4]) if r[4] is not None else 1.0,
                "reason": r[5] or "",
                "state": r[6] or "",
                "requested_at": str(r[7]) if r[7] else None,
            })
        return Response({"requests": data})

    def post(self, request):
        try:
            ensure_master_tables_exist()
        except Exception:
            pass
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR', 'MANAGER']:
            return Response({"error": "Permission denied"}, status=403)

        tenant = request.user.tenant
        employee_id = request.data.get('employee_id')
        worked_date = request.data.get('worked_date')
        credit_days = request.data.get('credit_days') or 1
        reason = request.data.get('reason') or ''

        if not employee_id or not worked_date:
            return Response({"error": "employee_id and worked_date are required"}, status=400)

        try:
            from datetime import date
            wd = worked_date if isinstance(worked_date, date) else date.fromisoformat(str(worked_date))
        except Exception:
            return Response({"error": "Invalid worked_date (use YYYY-MM-DD)"}, status=400)

        try:
            emp = Employee.objects.get(tenant=tenant, id=safe_int(employee_id))
        except Employee.DoesNotExist:
            return Response({"error": "Employee not found"}, status=404)

        # HR/Admin auto-approve; Manager -> pending.
        auto_approve = request.user.system_role in ['ADMIN', 'SUPER_ADMIN', 'HR']
        state = 'APPROVED' if auto_approve else 'PENDING'
        now = timezone.now()

        req_id = None
        with connection.cursor() as cursor:
            vendor = getattr(connection, "vendor", "")
            if vendor == "sqlite":
                cursor.execute(
                    """
                    INSERT INTO t_comp_off_request
                      (tenant_id, employee_id, worked_date, credit_days, reason, state, requested_by_id, requested_at,
                       reviewed_by_id, reviewed_at, review_comment)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        str(tenant.id), int(emp.id), str(wd), float(credit_days),
                        str(reason or '')[:2000], state,
                        int(request.user.id) if request.user.id else None, str(now),
                        int(request.user.id) if auto_approve else None,
                        str(now) if auto_approve else None,
                        str(reason or '')[:2000] if auto_approve else None,
                    ],
                )
                cursor.execute("SELECT last_insert_rowid()")
                req_id = cursor.fetchone()[0]
            else:
                cursor.execute(
                    """
                    INSERT INTO t_comp_off_request
                      (tenant_id, employee_id, worked_date, credit_days, reason, state, requested_by_id, requested_at,
                       reviewed_by_id, reviewed_at, review_comment)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        tenant.id, emp.id, wd, float(credit_days),
                        str(reason or '')[:2000], state,
                        request.user.id if request.user.id else None, now,
                        request.user.id if auto_approve else None,
                        now if auto_approve else None,
                        str(reason or '')[:2000] if auto_approve else None,
                    ],
                )
                req_id = cursor.lastrowid

        if not auto_approve:
            return Response({"message": "Submitted for approval", "state": state, "request_id": req_id}, status=202)

        # Auto-credit COMP leave balance on approval
        year = str(wd.year)
        lt = LeaveType.objects.filter(tenant=tenant, code__iexact='COMP').first()
        if not lt:
            return Response({"error": "COMP leave type not configured in Leave Master"}, status=400)

        bal, _ = LeaveBalance.objects.get_or_create(
            tenant=tenant,
            employee=emp,
            leave_type=lt,
            year=year,
            defaults={'allocated': 0, 'used': 0, 'carried_forward': 0},
        )
        bal.carried_forward = (bal.carried_forward or 0) + float(credit_days)
        bal.save(update_fields=['carried_forward'])

        return Response({"message": "Approved and credited", "state": state, "request_id": req_id})


class CompOffApproveView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        try:
            ensure_master_tables_exist()
        except Exception:
            pass
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)

        tenant = request.user.tenant
        req_id = request.data.get('request_id')
        action = (request.data.get('action') or '').strip().lower()
        comment = request.data.get('comment') or ''
        credit_days = request.data.get('credit_days')  # optional override on approval

        if not req_id:
            return Response({"error": "request_id is required"}, status=400)
        if action not in ['approve', 'reject']:
            return Response({"error": "action must be approve|reject"}, status=400)

        vendor = getattr(connection, "vendor", "")
        with connection.cursor() as cursor:
            if vendor == "sqlite":
                cursor.execute(
                    """
                    SELECT id, employee_id, worked_date, credit_days, state, reason
                    FROM t_comp_off_request
                    WHERE tenant_id = ? AND id = ?
                    """,
                    [str(tenant.id), int(req_id)],
                )
            else:
                cursor.execute(
                    """
                    SELECT id, employee_id, worked_date, credit_days, state, reason
                    FROM t_comp_off_request
                    WHERE tenant_id = %s AND id = %s
                    """,
                    [tenant.id, int(req_id)],
                )
            row = cursor.fetchone()

        if not row:
            return Response({"error": "Request not found"}, status=404)
        if str(row[4] or '').upper() != 'PENDING':
            return Response({"error": "Request is not pending"}, status=400)

        employee_id = row[1]
        worked_date = row[2]
        days = float(credit_days) if credit_days is not None else float(row[3] or 1)
        fallback_reason = row[5] or ''

        new_state = 'APPROVED' if action == 'approve' else 'REJECTED'
        reviewed_at = timezone.now()
        review_comment = str(comment or fallback_reason or '')[:2000]

        with connection.cursor() as cursor:
            vendor = getattr(connection, "vendor", "")
            if vendor == "sqlite":
                cursor.execute(
                    """
                    UPDATE t_comp_off_request
                    SET state = ?, reviewed_by_id = ?, reviewed_at = ?, review_comment = ?, credit_days = ?
                    WHERE tenant_id = ? AND id = ?
                    """,
                    [new_state, int(request.user.id), str(reviewed_at), review_comment, float(days), str(tenant.id), int(req_id)],
                )
            else:
                cursor.execute(
                    """
                    UPDATE t_comp_off_request
                    SET state = %s, reviewed_by_id = %s, reviewed_at = %s, review_comment = %s, credit_days = %s
                    WHERE tenant_id = %s AND id = %s
                    """,
                    [new_state, request.user.id, reviewed_at, review_comment, float(days), tenant.id, int(req_id)],
                )

        if action == 'reject':
            return Response({"message": "Rejected", "state": new_state})

        # Credit COMP leave balance
        try:
            emp = Employee.objects.get(tenant=tenant, id=safe_int(employee_id))
        except Employee.DoesNotExist:
            return Response({"error": "Employee not found"}, status=404)

        lt = LeaveType.objects.filter(tenant=tenant, code__iexact='COMP').first()
        if not lt:
            return Response({"error": "COMP leave type not configured in Leave Master"}, status=400)

        year = str(worked_date.year) if worked_date else str(timezone.localdate().year)
        bal, _ = LeaveBalance.objects.get_or_create(
            tenant=tenant,
            employee=emp,
            leave_type=lt,
            year=year,
            defaults={'allocated': 0, 'used': 0, 'carried_forward': 0},
        )
        bal.carried_forward = (bal.carried_forward or 0) + float(days)
        bal.save(update_fields=['carried_forward'])

        return Response({"message": "Approved and credited", "state": new_state})


# ─────────────────────────────────────────────
# Overtime — requests & approvals
# ─────────────────────────────────────────────
class OvertimeRequestsView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            ensure_master_tables_exist()
        except Exception:
            pass
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR', 'MANAGER']:
            return Response({"error": "Permission denied"}, status=403)

        tenant = request.user.tenant
        state = (request.query_params.get('state') or 'PENDING').strip().upper()
        employee_id = request.query_params.get('employee_id')
        cycle = request.query_params.get('cycle')  # YYYY-MM optional

        vendor = getattr(connection, "vendor", "")
        with connection.cursor() as cursor:
            if vendor == "sqlite":
                params = [str(tenant.id), state]
                where = "r.tenant_id = ? AND UPPER(r.state) = ?"
                if employee_id:
                    where += " AND r.employee_id = ?"
                    params.append(int(employee_id))
                if cycle:
                    where += " AND substr(r.ot_date, 1, 7) = ?"
                    params.append(str(cycle))
                cursor.execute(
                    f"""
                    SELECT r.id, r.employee_id, e.name, r.ot_date, r.hours, r.reason, r.state, r.requested_at
                    FROM t_overtime_request r
                    LEFT JOIN t_employee e ON e.id = r.employee_id
                    WHERE {where}
                    ORDER BY r.requested_at DESC
                    LIMIT 500
                    """,
                    params,
                )
            else:
                params = [tenant.id, state]
                where = "r.tenant_id = %s AND UPPER(r.state) = %s"
                if employee_id:
                    where += " AND r.employee_id = %s"
                    params.append(int(employee_id))
                if cycle:
                    where += " AND DATE_FORMAT(r.ot_date, '%%Y-%%m') = %s"
                    params.append(str(cycle))
                cursor.execute(
                    f"""
                    SELECT r.id, r.employee_id, e.name, r.ot_date, r.hours, r.reason, r.state, r.requested_at
                    FROM t_overtime_request r
                    LEFT JOIN t_employee e ON e.id = r.employee_id
                    WHERE {where}
                    ORDER BY r.requested_at DESC
                    LIMIT 500
                    """,
                    params,
                )
            rows = cursor.fetchall()

        data = []
        for r in rows:
            data.append({
                "id": r[0],
                "employee_id": r[1],
                "employee_name": r[2] or "",
                "ot_date": str(r[3]) if r[3] else None,
                "hours": float(r[4]) if r[4] is not None else 0.0,
                "reason": r[5] or "",
                "state": r[6] or "",
                "requested_at": str(r[7]) if r[7] else None,
            })
        return Response({"requests": data})

    def post(self, request):
        try:
            ensure_master_tables_exist()
        except Exception:
            pass
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR', 'MANAGER']:
            return Response({"error": "Permission denied"}, status=403)

        tenant = request.user.tenant
        employee_id = request.data.get('employee_id')
        ot_date = request.data.get('ot_date')
        hours = request.data.get('hours')
        reason = request.data.get('reason') or ''

        if not employee_id or not ot_date or hours is None:
            return Response({"error": "employee_id, ot_date, hours are required"}, status=400)

        try:
            from datetime import date
            d = ot_date if isinstance(ot_date, date) else date.fromisoformat(str(ot_date))
        except Exception:
            return Response({"error": "Invalid ot_date (use YYYY-MM-DD)"}, status=400)

        try:
            emp = Employee.objects.get(tenant=tenant, id=safe_int(employee_id))
        except Employee.DoesNotExist:
            return Response({"error": "Employee not found"}, status=404)

        auto_approve = request.user.system_role in ['ADMIN', 'SUPER_ADMIN', 'HR']
        state = 'APPROVED' if auto_approve else 'PENDING'
        now = timezone.now()

        req_id = None
        with connection.cursor() as cursor:
            vendor = getattr(connection, "vendor", "")
            if vendor == "sqlite":
                cursor.execute(
                    """
                    INSERT INTO t_overtime_request
                      (tenant_id, employee_id, ot_date, hours, reason, state, requested_by_id, requested_at,
                       reviewed_by_id, reviewed_at, review_comment)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        str(tenant.id), int(emp.id), str(d), float(hours),
                        str(reason or '')[:2000], state,
                        int(request.user.id) if request.user.id else None, str(now),
                        int(request.user.id) if auto_approve else None,
                        str(now) if auto_approve else None,
                        str(reason or '')[:2000] if auto_approve else None,
                    ],
                )
                cursor.execute("SELECT last_insert_rowid()")
                req_id = cursor.fetchone()[0]
            else:
                cursor.execute(
                    """
                    INSERT INTO t_overtime_request
                      (tenant_id, employee_id, ot_date, hours, reason, state, requested_by_id, requested_at,
                       reviewed_by_id, reviewed_at, review_comment)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        tenant.id, emp.id, d, float(hours),
                        str(reason or '')[:2000], state,
                        request.user.id if request.user.id else None, now,
                        request.user.id if auto_approve else None,
                        now if auto_approve else None,
                        str(reason or '')[:2000] if auto_approve else None,
                    ],
                )
                req_id = cursor.lastrowid

        if not auto_approve:
            return Response({"message": "Submitted for approval", "state": state, "request_id": req_id}, status=202)

        # Auto-sync into payroll variable inputs
        cycle = str(d)[:7]
        try:
            ensure_payroll_workflow_tables_exist()
        except Exception:
            pass

        try:
            PayrollVariableInput.objects.create(
                tenant=tenant,
                employee=emp,
                cycle_month=cycle,
                input_type='OVERTIME',
                label=f"OT {d}",
                amount=float(hours),
                meta={"date": str(d), "request_id": req_id, "reason": str(reason or '')[:2000]},
                status='Active',
                created_by=request.user.id,
                approved_by=request.user.id,
            )
        except Exception:
            pass

        return Response({"message": "Approved and synced to payroll", "state": state, "request_id": req_id})


class OvertimeApproveView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        try:
            ensure_master_tables_exist()
        except Exception:
            pass
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)

        tenant = request.user.tenant
        req_id = request.data.get('request_id')
        action = (request.data.get('action') or '').strip().lower()
        comment = request.data.get('comment') or ''

        if not req_id:
            return Response({"error": "request_id is required"}, status=400)
        if action not in ['approve', 'reject']:
            return Response({"error": "action must be approve|reject"}, status=400)

        vendor = getattr(connection, "vendor", "")
        with connection.cursor() as cursor:
            if vendor == "sqlite":
                cursor.execute(
                    """
                    SELECT id, employee_id, ot_date, hours, state, reason
                    FROM t_overtime_request
                    WHERE tenant_id = ? AND id = ?
                    """,
                    [str(tenant.id), int(req_id)],
                )
            else:
                cursor.execute(
                    """
                    SELECT id, employee_id, ot_date, hours, state, reason
                    FROM t_overtime_request
                    WHERE tenant_id = %s AND id = %s
                    """,
                    [tenant.id, int(req_id)],
                )
            row = cursor.fetchone()

        if not row:
            return Response({"error": "Request not found"}, status=404)
        if str(row[4] or '').upper() != 'PENDING':
            return Response({"error": "Request is not pending"}, status=400)

        employee_id = row[1]
        ot_date = row[2]
        hours = float(row[3] or 0)
        fallback_reason = row[5] or ''

        new_state = 'APPROVED' if action == 'approve' else 'REJECTED'
        reviewed_at = timezone.now()
        review_comment = str(comment or fallback_reason or '')[:2000]

        with connection.cursor() as cursor:
            vendor = getattr(connection, "vendor", "")
            if vendor == "sqlite":
                cursor.execute(
                    """
                    UPDATE t_overtime_request
                    SET state = ?, reviewed_by_id = ?, reviewed_at = ?, review_comment = ?
                    WHERE tenant_id = ? AND id = ?
                    """,
                    [new_state, int(request.user.id), str(reviewed_at), review_comment, str(tenant.id), int(req_id)],
                )
            else:
                cursor.execute(
                    """
                    UPDATE t_overtime_request
                    SET state = %s, reviewed_by_id = %s, reviewed_at = %s, review_comment = %s
                    WHERE tenant_id = %s AND id = %s
                    """,
                    [new_state, request.user.id, reviewed_at, review_comment, tenant.id, int(req_id)],
                )

        if action == 'reject':
            return Response({"message": "Rejected", "state": new_state})

        # Sync into payroll variable inputs
        try:
            ensure_payroll_workflow_tables_exist()
        except Exception:
            pass

        try:
            emp = Employee.objects.get(tenant=tenant, id=safe_int(employee_id))
        except Employee.DoesNotExist:
            return Response({"error": "Employee not found"}, status=404)

        cycle = str(ot_date)[:7]
        try:
            PayrollVariableInput.objects.create(
                tenant=tenant,
                employee=emp,
                cycle_month=cycle,
                input_type='OVERTIME',
                label=f"OT {ot_date}",
                amount=float(hours),
                meta={"date": str(ot_date), "request_id": int(req_id), "review_comment": review_comment},
                status='Active',
                created_by=request.user.id,
                approved_by=request.user.id,
            )
        except Exception:
            pass

        return Response({"message": "Approved and synced", "state": new_state})



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


def _forbidden_employee_access(message="You are not authorized to access this employee record."):
    return Response({"error": message}, status=403)


def _resolve_actor_employee(request):
    try:
        return request.user.employee_profile
    except Exception:
        return None


def _employee_scope_filter(request):
    """
    Hierarchy scope:
    - SUPER_ADMIN / ADMIN: all tenant employees
    - MANAGER: self + employees whose reporting_to == self
    - HR: self + employees whose reporting_hr == self
    - EMPLOYEE: self only
    """
    role = str(getattr(request.user, "system_role", "") or "").upper()
    if role in ["SUPER_ADMIN", "ADMIN"]:
        return Q()

    actor = _resolve_actor_employee(request)
    if not actor:
        return None

    if role == "MANAGER":
        return Q(id=actor.id) | Q(reporting_to_id=actor.id)
    if role == "HR":
        return Q(id=actor.id) | Q(reporting_hr_id=actor.id)
    if role == "EMPLOYEE":
        return Q(id=actor.id)
    return None


def _is_employee_in_scope(request, employee):
    scope_q = _employee_scope_filter(request)
    if scope_q is None:
        return False
    return Employee.objects.filter(
        tenant=request.user.tenant,
        id=getattr(employee, "id", None),
    ).filter(scope_q).exists()


# ─────────────────────────────────────────────
# TASK 2A — HR Employee Management
# ─────────────────────────────────────────────
class HREmployeeListView(views.APIView):
    """HR/Admin: list all employees or create new one."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ensure_master_tables_exist()
        ensure_branch_shift_tables_exist()
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR', 'MANAGER', 'EMPLOYEE']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        qs = Employee.objects.filter(tenant=tenant).select_related('department', 'designation', 'reporting_to').prefetch_related('documents')
        scope_q = _employee_scope_filter(request)
        if scope_q is None:
            return _forbidden_employee_access("Employee mapping not found for your account.")
        qs = qs.filter(scope_q)
        
        serializer = EmployeeSerializer(qs, many=True, context={'request': request})
        return Response({"employees": serializer.data, "total": qs.count()})

    def post(self, request):
        """HR/Admin creates a new employee and optionally creates a User account."""
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR', 'MANAGER']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        payload = request.data
        ensure_branch_shift_tables_exist()
        ensure_employee_shift_assignment_tables_exist()
        ensure_hr_lifecycle_tables_exist()
        actor_role = str(getattr(request.user, "system_role", "") or "").upper()
        actor_emp = _resolve_actor_employee(request)

        # Auto-generate employee code if not provided
        emp_code = payload.get('employee_code') or self._generate_code(tenant)

        dept = Department.objects.filter(tenant=tenant, id=safe_int(payload.get('department_id'))).first()
        role = Role.objects.filter(tenant=tenant, id=safe_int(payload.get('designation_id'))).first()
        manager = Employee.objects.filter(tenant=tenant, id=safe_int(payload.get('reporting_to_id'))).first()
        hr_manager = Employee.objects.filter(tenant=tenant, id=safe_int(payload.get('reporting_hr_id'))).first()
        branch = Branch.objects.filter(tenant=tenant, id=safe_int(payload.get('branch_id'))).first()
        if actor_role in ['MANAGER', 'HR'] and not actor_emp:
            return _forbidden_employee_access("Employee mapping not found for your account.")
        if actor_role == 'MANAGER':
            if manager and manager.id != actor_emp.id:
                return _forbidden_employee_access("Manager can assign only their own reporting hierarchy.")
            manager = actor_emp
        if actor_role == 'HR':
            if hr_manager and hr_manager.id != actor_emp.id:
                return _forbidden_employee_access("HR can assign only their own reporting hierarchy.")
            hr_manager = actor_emp

        with transaction.atomic():
            # Create login account if requested.
            create_account = payload.get('create_account')
            wants_account = str(create_account).lower() == 'true' or create_account is True

            user = None
            temp_password = None
            username = None

            if wants_account:
                password = payload.get('password')
                temp_password = password if password else ''.join(random.choices(string.ascii_letters + string.digits, k=10))
                username = payload.get('email').split('@')[0] + "_" + str(random.randint(100, 999))

                user, created = User.objects.get_or_create(
                    email=payload.get('email'),
                    defaults={
                        'username': username,
                        'tenant': tenant,
                        'is_verified': True,
                        'must_change_password': True
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

            defaults = dict(
                tenant=tenant,
                name=payload.get('name'),
                email=payload.get('email'),
                phone=payload.get('phone', ''),
                employee_code=emp_code,
                branch=branch,
                department=dept,
                designation=role,
                reporting_to=manager,
                reporting_hr=hr_manager,
                joining_date=payload.get('joining_date') or None,
                status=payload.get('status', 'Active'),
                base_salary=payload.get('base_salary', 0),
                dob=payload.get('dob') or None,
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
                onboarding_completed_at=timezone.now() if payload.get('onboarding_status') == 'Completed' else None,
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
                extended_profile=payload.get('extended_profile', {}),
            )

            # If a user account is being used, upsert by (tenant, user) to respect the OneToOne constraint.
            if user is not None:
                employee, _created = Employee.objects.update_or_create(
                    tenant=tenant,
                    user=user,
                    defaults=defaults,
                )
            else:
                employee = Employee.objects.create(**defaults)

            # Persist shift assignment history (effective-dated).
            shift_id = payload.get('shift_id')
            if shift_id is None and isinstance(payload.get('extended_profile'), dict):
                shift_id = payload['extended_profile'].get('shift_id')
            eff = payload.get('shift_effective_from') or payload.get('joining_date')
            if shift_id:
                try:
                    from datetime import date
                    eff_date = timezone.localdate()
                    if eff:
                        try:
                            eff_date = eff if isinstance(eff, date) else date.fromisoformat(str(eff))
                        except Exception:
                            eff_date = timezone.localdate()
                    sft = Shift.objects.filter(tenant=tenant, id=safe_int(shift_id)).first()
                    if sft:
                        _upsert_employee_shift_assignment(tenant.id, employee.id, sft.id, eff_date, request.user.id)
                        try:
                            _insert_lifecycle_event(
                                tenant.id,
                                employee.id,
                                "SHIFT_CHANGE",
                                eff_date,
                                {"shift_id": sft.id, "shift_name": sft.name},
                                request.user.id,
                            )
                        except Exception:
                            pass
                except Exception:
                    pass

            # Persist salary structure link (source of truth for payroll).
            struct_id = payload.get('salary_structure_id') or payload.get('structure_id')
            eff = payload.get('salary_effective_from') or payload.get('effective_from') or payload.get('joining_date')
            if struct_id:
                try:
                    # Let setup endpoint logic handle parsing/tenant scoping via ORM here.
                    # effective_from: if invalid/missing, default to today.
                    from datetime import date
                    eff_date = timezone.localdate()
                    if eff:
                        try:
                            eff_date = eff if isinstance(eff, date) else date.fromisoformat(str(eff))
                        except Exception:
                            eff_date = timezone.localdate()
                    # Validate structure belongs to tenant
                    s = SalaryStructure.objects.filter(tenant=tenant, id=safe_int(struct_id)).first()
                    if s:
                        EmployeeSalaryStructure.objects.update_or_create(
                            tenant=tenant,
                            employee=employee,
                            defaults={
                                'structure': s,
                                'effective_from': eff_date,
                                'is_active': True,
                            }
                        )
                except Exception:
                    # Salary structure linking should not block employee creation.
                    pass

            if user is not None:
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
        ensure_hr_lifecycle_tables_exist()
        ensure_branch_shift_tables_exist()
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR', 'MANAGER', 'EMPLOYEE']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        try:
            e = Employee.objects.select_related('department', 'designation', 'reporting_to').prefetch_related('documents').get(tenant=tenant, id=employee_id)
        except Employee.DoesNotExist:
            return Response({"error": "Employee not found"}, status=404)
        if not _is_employee_in_scope(request, e):
            return _forbidden_employee_access()
        
        serializer = EmployeeSerializer(e, context={'request': request})
        return Response(serializer.data)

    def put(self, request, employee_id):
        ensure_hr_lifecycle_tables_exist()
        ensure_branch_shift_tables_exist()
        ensure_employee_shift_assignment_tables_exist()
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR', 'MANAGER']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        try:
            e = Employee.objects.get(tenant=tenant, id=employee_id)
        except Employee.DoesNotExist:
            return Response({"error": "Not found"}, status=404)
        actor_role = str(getattr(request.user, "system_role", "") or "").upper()
        actor_emp = _resolve_actor_employee(request)
        if not _is_employee_in_scope(request, e):
            return _forbidden_employee_access("You are not authorized to modify this employee.")
        if actor_role in ['MANAGER', 'HR'] and not actor_emp:
            return _forbidden_employee_access("Employee mapping not found for your account.")
        if actor_role == 'MANAGER':
            return _forbidden_employee_access("Manager role cannot update employee records from this endpoint.")

        p = request.data
        e.name = p.get('name', e.name)
        e.email = p.get('email', e.email)
        e.phone = p.get('phone', e.phone)
        e.status = p.get('status', e.status)
        
        if 'joining_date' in p:
            e.joining_date = p['joining_date'] or None
        if 'dob' in p:
            e.dob = p['dob'] or None
            
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
        
        new_status = p.get('onboarding_status')
        if new_status and new_status != e.onboarding_status:
            e.onboarding_status = new_status
            if new_status == 'Completed':
                e.onboarding_completed_at = timezone.now()
                
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

        if 'extended_profile' in p:
            if not isinstance(e.extended_profile, dict):
                e.extended_profile = {}
            e.extended_profile.update(p['extended_profile'])

        if 'base_salary' in p:
            e.base_salary = p['base_salary']
        if p.get('department_id'):
            e.department = Department.objects.filter(tenant=tenant, id=safe_int(p['department_id'])).first()
        if p.get('designation_id'):
            e.designation = Role.objects.filter(tenant=tenant, id=safe_int(p['designation_id'])).first()
        if 'branch_id' in p:
            bid = p.get('branch_id')
            e.branch = Branch.objects.filter(tenant=tenant, id=safe_int(bid)).first() if bid else None
        if p.get('reporting_to_id'):
            next_mgr = Employee.objects.filter(tenant=tenant, id=safe_int(p['reporting_to_id'])).first()
            if actor_role == 'HR' and next_mgr and actor_emp and next_mgr.id != actor_emp.id:
                return _forbidden_employee_access("HR can assign only their own reporting hierarchy.")
            e.reporting_to = next_mgr
        if p.get('reporting_hr_id'):
            next_hr = Employee.objects.filter(tenant=tenant, id=safe_int(p['reporting_hr_id'])).first()
            if actor_role == 'HR' and next_hr and actor_emp and next_hr.id != actor_emp.id:
                return _forbidden_employee_access("HR can assign only their own reporting hierarchy.")
            e.reporting_hr = next_hr
        e.save()

        # Persist shift assignment history (effective-dated), if provided.
        shift_id = p.get('shift_id')
        if shift_id is None and isinstance(p.get('extended_profile'), dict):
            shift_id = p['extended_profile'].get('shift_id')
        eff = p.get('shift_effective_from') or p.get('joining_date') or e.joining_date
        if shift_id:
            try:
                from datetime import date
                eff_date = timezone.localdate()
                if eff:
                    try:
                        eff_date = eff if isinstance(eff, date) else date.fromisoformat(str(eff))
                    except Exception:
                        eff_date = timezone.localdate()
                sft = Shift.objects.filter(tenant=tenant, id=safe_int(shift_id)).first()
                if sft:
                    _upsert_employee_shift_assignment(tenant.id, e.id, sft.id, eff_date, request.user.id)
                    try:
                        _insert_lifecycle_event(
                            tenant.id,
                            e.id,
                            "SHIFT_CHANGE",
                            eff_date,
                            {"shift_id": sft.id, "shift_name": sft.name},
                            request.user.id,
                        )
                    except Exception:
                        pass
            except Exception:
                pass

        # Persist salary structure link updates (if provided).
        struct_id = p.get('salary_structure_id') or p.get('structure_id')
        eff = p.get('salary_effective_from') or p.get('effective_from') or p.get('joining_date')
        if struct_id is not None:
            try:
                from datetime import date
                eff_date = timezone.localdate()
                if eff:
                    try:
                        eff_date = eff if isinstance(eff, date) else date.fromisoformat(str(eff))
                    except Exception:
                        eff_date = timezone.localdate()
                s = SalaryStructure.objects.filter(tenant=tenant, id=safe_int(struct_id)).first()
                if s:
                    EmployeeSalaryStructure.objects.update_or_create(
                        tenant=tenant,
                        employee=e,
                        defaults={
                            'structure': s,
                            'effective_from': eff_date,
                            'is_active': True,
                        }
                    )
            except Exception:
                pass

        # Optional: update linked login role designation (t_user.role FK -> t_role)
        if e.user and p.get('designation_id') is not None:
            role_obj = Role.objects.filter(tenant=e.tenant, id=safe_int(p.get('designation_id'))).first()
            if role_obj:
                e.user.role = role_obj
                e.user.save(update_fields=['role_id'])

        return Response({"message": "Employee updated"})

    def delete(self, request, employee_id):
        ensure_hr_lifecycle_tables_exist()
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        Employee.objects.filter(tenant=tenant, id=employee_id).update(status='Terminated')
        return Response({"message": "Employee deactivated"})


class EmployeeLifecycleView(views.APIView):
    """HR/Admin: fetch employee lifecycle timeline (transfer/salary/exit events)."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, employee_id):
        ensure_hr_lifecycle_tables_exist()
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR', 'MANAGER', 'EMPLOYEE']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        emp = Employee.objects.filter(tenant=tenant, id=employee_id).first()
        if not emp:
            return Response({"error": "Employee not found"}, status=404)
        if not _is_employee_in_scope(request, emp):
            return _forbidden_employee_access()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, event_type, effective_from, meta, created_at
                FROM t_employee_lifecycle_event
                WHERE tenant_id = %s AND employee_id = %s
                ORDER BY created_at DESC
                LIMIT 200
                """,
                [tenant.id, employee_id],
            )
            rows = cursor.fetchall()
        events = []
        for r in rows:
            events.append({
                "id": r[0],
                "event_type": r[1],
                "effective_from": str(r[2]) if r[2] else None,
                "meta": r[3],
                "created_at": str(r[4]) if r[4] else None,
            })
        return Response({"events": events})


def _insert_lifecycle_event(tenant_id, employee_id, event_type, effective_from, meta, created_by_id):
    with connection.cursor() as cursor:
        vendor = getattr(connection, "vendor", "")
        if vendor == "sqlite":
            cursor.execute(
                "INSERT INTO t_employee_lifecycle_event (tenant_id, employee_id, event_type, effective_from, meta, created_by_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [tenant_id, employee_id, event_type, effective_from, json.dumps(meta or {}), created_by_id, timezone.now()],
            )
        else:
            cursor.execute(
                "INSERT INTO t_employee_lifecycle_event (tenant_id, employee_id, event_type, effective_from, meta, created_by_id, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [tenant_id, employee_id, event_type, effective_from, json.dumps(meta or {}), created_by_id, timezone.now()],
            )


class EmployeeTransferView(views.APIView):
    """HR/Admin: transfer/promotion with effective date (writes audit event)."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, employee_id):
        ensure_hr_lifecycle_tables_exist()
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR', 'MANAGER']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        e = Employee.objects.filter(tenant=tenant, id=employee_id).first()
        if not e:
            return Response({"error": "Employee not found"}, status=404)
        actor_role = str(getattr(request.user, "system_role", "") or "").upper()
        actor_emp = _resolve_actor_employee(request)
        if not _is_employee_in_scope(request, e):
            return _forbidden_employee_access("You are not authorized to modify this employee.")
        if actor_role in ['MANAGER', 'HR'] and not actor_emp:
            return _forbidden_employee_access("Employee mapping not found for your account.")
        if actor_role == 'MANAGER':
            return _forbidden_employee_access("Manager role cannot transfer employees from this endpoint.")

        p = request.data or {}
        effective_from = p.get('effective_from') or timezone.localdate()
        reason = p.get('reason') or ''

        from_dept = e.department_id
        from_desg = e.designation_id
        from_mgr = e.reporting_to_id

        to_dept = safe_int(p.get('to_department_id')) if p.get('to_department_id') else from_dept
        to_desg = safe_int(p.get('to_designation_id')) if p.get('to_designation_id') else from_desg
        to_mgr = safe_int(p.get('to_manager_id')) if p.get('to_manager_id') else from_mgr
        if actor_role == 'HR' and to_mgr and actor_emp and to_mgr != actor_emp.id:
            return _forbidden_employee_access("HR can assign only their own reporting hierarchy.")

        if to_dept:
            e.department = Department.objects.filter(tenant=tenant, id=to_dept).first()
        if to_desg:
            e.designation = Role.objects.filter(tenant=tenant, id=to_desg).first()
        if to_mgr:
            e.reporting_to = Employee.objects.filter(tenant=tenant, id=to_mgr).first()
        e.save()

        with connection.cursor() as cursor:
            vendor = getattr(connection, "vendor", "")
            if vendor == "sqlite":
                cursor.execute(
                    """
                    INSERT INTO t_employee_transfer
                    (tenant_id, employee_id, from_department_id, to_department_id, from_designation_id, to_designation_id, from_manager_id, to_manager_id, effective_from, reason, status, created_by_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Approved', ?, ?)
                    """,
                    [tenant.id, employee_id, from_dept, to_dept, from_desg, to_desg, from_mgr, to_mgr, effective_from, reason, request.user.id, timezone.now()],
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO t_employee_transfer
                    (tenant_id, employee_id, from_department_id, to_department_id, from_designation_id, to_designation_id, from_manager_id, to_manager_id, effective_from, reason, status, created_by_id, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Approved', %s, %s)
                    """,
                    [tenant.id, employee_id, from_dept, to_dept, from_desg, to_desg, from_mgr, to_mgr, effective_from, reason, request.user.id, timezone.now()],
                )

        _insert_lifecycle_event(
            tenant.id, employee_id, "TRANSFER", effective_from,
            {"from_department_id": from_dept, "to_department_id": to_dept, "from_designation_id": from_desg, "to_designation_id": to_desg, "from_manager_id": from_mgr, "to_manager_id": to_mgr, "reason": reason},
            request.user.id
        )
        return Response({"message": "Employee transfer saved"})


class EmployeeSalaryRevisionView(views.APIView):
    """HR/Admin: salary revision with effective date + reason (writes audit event)."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, employee_id):
        ensure_hr_lifecycle_tables_exist()
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR', 'MANAGER']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        e = Employee.objects.filter(tenant=tenant, id=employee_id).first()
        if not e:
            return Response({"error": "Employee not found"}, status=404)
        actor_role = str(getattr(request.user, "system_role", "") or "").upper()
        if not _is_employee_in_scope(request, e):
            return _forbidden_employee_access("You are not authorized to modify this employee.")
        if actor_role == 'MANAGER':
            return _forbidden_employee_access("Manager role cannot revise salary from this endpoint.")

        p = request.data or {}
        effective_from = p.get('effective_from') or timezone.localdate()
        reason = p.get('reason') or ''

        old_base = Decimal(e.base_salary or 0)
        new_base = Decimal(str(p.get('new_base_salary') or old_base))
        ext = e.extended_profile or {}
        old_ctc = Decimal(str(ext.get('ctc', 0) or 0))
        new_ctc = Decimal(str(p.get('new_ctc') or old_ctc))

        # Persist: update employee base + ext profile (ctc)
        e.base_salary = new_base
        ext['ctc'] = float(new_ctc)
        e.extended_profile = ext
        e.save(update_fields=['base_salary', 'extended_profile'])

        with connection.cursor() as cursor:
            vendor = getattr(connection, "vendor", "")
            if vendor == "sqlite":
                cursor.execute(
                    """
                    INSERT INTO t_employee_salary_revision
                    (tenant_id, employee_id, old_ctc, new_ctc, old_base_salary, new_base_salary, effective_from, reason, status, created_by_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Approved', ?, ?)
                    """,
                    [tenant.id, employee_id, old_ctc, new_ctc, old_base, new_base, effective_from, reason, request.user.id, timezone.now()],
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO t_employee_salary_revision
                    (tenant_id, employee_id, old_ctc, new_ctc, old_base_salary, new_base_salary, effective_from, reason, status, created_by_id, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'Approved', %s, %s)
                    """,
                    [tenant.id, employee_id, old_ctc, new_ctc, old_base, new_base, effective_from, reason, request.user.id, timezone.now()],
                )

        _insert_lifecycle_event(
            tenant.id, employee_id, "SALARY_REVISION", effective_from,
            {"old_ctc": float(old_ctc), "new_ctc": float(new_ctc), "old_base_salary": float(old_base), "new_base_salary": float(new_base), "reason": reason},
            request.user.id
        )
        return Response({"message": "Salary revision saved"})


class EmployeeExitView(views.APIView):
    """HR/Admin: create/close an exit case (writes audit event)."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, employee_id):
        ensure_hr_lifecycle_tables_exist()
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR', 'MANAGER']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        e = Employee.objects.filter(tenant=tenant, id=employee_id).first()
        if not e:
            return Response({"error": "Employee not found"}, status=404)
        actor_role = str(getattr(request.user, "system_role", "") or "").upper()
        if not _is_employee_in_scope(request, e):
            return _forbidden_employee_access("You are not authorized to modify this employee.")
        if actor_role == 'MANAGER':
            return _forbidden_employee_access("Manager role cannot initiate exits from this endpoint.")

        p = request.data or {}
        exit_type = p.get('exit_type') or 'Termination'
        last_working_day = p.get('last_working_day')
        resignation_date = p.get('resignation_date')
        reason = p.get('reason') or ''

        with connection.cursor() as cursor:
            vendor = getattr(connection, "vendor", "")
            if vendor == "sqlite":
                cursor.execute(
                    """
                    INSERT INTO t_employee_exit
                    (tenant_id, employee_id, exit_type, last_working_day, resignation_date, reason, status, settlement_status, created_by_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'Open', 'Pending', ?, ?)
                    """,
                    [tenant.id, employee_id, exit_type, last_working_day, resignation_date, reason, request.user.id, timezone.now()],
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO t_employee_exit
                    (tenant_id, employee_id, exit_type, last_working_day, resignation_date, reason, status, settlement_status, created_by_id, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, 'Open', 'Pending', %s, %s)
                    """,
                    [tenant.id, employee_id, exit_type, last_working_day, resignation_date, reason, request.user.id, timezone.now()],
                )

        _insert_lifecycle_event(
            tenant.id, employee_id, "EXIT_INITIATED", last_working_day,
            {"exit_type": exit_type, "last_working_day": last_working_day, "resignation_date": resignation_date, "reason": reason},
            request.user.id
        )
        return Response({"message": "Exit case created"})


class EmployeeReinstateView(views.APIView):
    """Admin/HR: reinstate a terminated employee (writes audit event)."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, employee_id):
        ensure_hr_lifecycle_tables_exist()
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR', 'MANAGER']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        e = Employee.objects.filter(tenant=tenant, id=employee_id).first()
        if not e:
            return Response({"error": "Employee not found"}, status=404)
        actor_role = str(getattr(request.user, "system_role", "") or "").upper()
        if not _is_employee_in_scope(request, e):
            return _forbidden_employee_access("You are not authorized to modify this employee.")
        if actor_role == 'MANAGER':
            return _forbidden_employee_access("Manager role cannot reinstate employees from this endpoint.")

        prev = e.status
        e.status = 'Active'
        e.save(update_fields=['status'])
        _insert_lifecycle_event(
            tenant.id, employee_id, "REINSTATED", timezone.localdate(),
            {"from_status": prev, "to_status": "Active"},
            request.user.id
        )
        return Response({"message": "Employee reinstated"})


class LifecycleTransfersListView(views.APIView):
    """HR/Admin: tenant-wide transfers list for reporting/export."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ensure_hr_lifecycle_tables_exist()
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        month = request.query_params.get('month')  # YYYY-MM
        fmt = (request.query_params.get('format') or '').lower()

        where = "t.tenant_id = %s"
        params = [tenant.id]
        if month:
            where += " AND substr(CAST(t.effective_from AS CHAR), 1, 7) = %s"
            params.append(month)

        # SQLite uses substr(date,1,7) too; MySQL supports substr(CAST(date as char),1,7)
        sql = f"""
            SELECT
              t.id,
              t.effective_from,
              e.id as employee_id,
              e.employee_code,
              e.name as employee_name,
              fd.name as from_department,
              td.name as to_department,
              fr.name as from_role,
              tr.name as to_role,
              fm.name as from_manager,
              tm.name as to_manager,
              t.reason,
              t.created_at
            FROM t_employee_transfer t
            LEFT JOIN t_employee e ON e.id = t.employee_id
            LEFT JOIN t_department fd ON fd.id = t.from_department_id
            LEFT JOIN t_department td ON td.id = t.to_department_id
            LEFT JOIN t_role fr ON fr.id = t.from_designation_id
            LEFT JOIN t_role tr ON tr.id = t.to_designation_id
            LEFT JOIN t_employee fm ON fm.id = t.from_manager_id
            LEFT JOIN t_employee tm ON tm.id = t.to_manager_id
            WHERE {where}
            ORDER BY t.effective_from DESC, t.created_at DESC
            LIMIT 2000
        """
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()

        cols = [
            "id", "effective_from", "employee_id", "employee_code", "employee_name",
            "from_department", "to_department", "from_role", "to_role",
            "from_manager", "to_manager", "reason", "created_at"
        ]
        data = [dict(zip(cols, r)) for r in rows]

        if fmt == 'csv':
            import csv
            from io import StringIO
            buf = StringIO()
            w = csv.DictWriter(buf, fieldnames=cols)
            w.writeheader()
            for d in data:
                w.writerow({k: (d.get(k) if d.get(k) is not None else '') for k in cols})
            resp = Response(buf.getvalue())
            resp['Content-Type'] = 'text/csv'
            resp['Content-Disposition'] = 'attachment; filename="transfers.csv"'
            return resp

        return Response({"transfers": data, "count": len(data)})


class LifecycleSalaryRevisionsListView(views.APIView):
    """HR/Admin: tenant-wide salary revision list for reporting/export."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ensure_hr_lifecycle_tables_exist()
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        month = request.query_params.get('month')  # YYYY-MM
        fmt = (request.query_params.get('format') or '').lower()

        where = "s.tenant_id = %s"
        params = [tenant.id]
        if month:
            where += " AND substr(CAST(s.effective_from AS CHAR), 1, 7) = %s"
            params.append(month)

        sql = f"""
            SELECT
              s.id,
              s.effective_from,
              e.id as employee_id,
              e.employee_code,
              e.name as employee_name,
              s.old_ctc,
              s.new_ctc,
              s.old_base_salary,
              s.new_base_salary,
              s.reason,
              s.created_at
            FROM t_employee_salary_revision s
            LEFT JOIN t_employee e ON e.id = s.employee_id
            WHERE {where}
            ORDER BY s.effective_from DESC, s.created_at DESC
            LIMIT 2000
        """
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()

        cols = [
            "id", "effective_from", "employee_id", "employee_code", "employee_name",
            "old_ctc", "new_ctc", "old_base_salary", "new_base_salary", "reason", "created_at"
        ]
        data = [dict(zip(cols, r)) for r in rows]

        if fmt == 'csv':
            import csv
            from io import StringIO
            buf = StringIO()
            w = csv.DictWriter(buf, fieldnames=cols)
            w.writeheader()
            for d in data:
                w.writerow({k: (d.get(k) if d.get(k) is not None else '') for k in cols})
            resp = Response(buf.getvalue())
            resp['Content-Type'] = 'text/csv'
            resp['Content-Disposition'] = 'attachment; filename="salary-revisions.csv"'
            return resp

        return Response({"revisions": data, "count": len(data)})


class LifecycleExitsListView(views.APIView):
    """HR/Admin: tenant-wide exits list for reporting/export."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ensure_hr_lifecycle_tables_exist()
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        status_q = request.query_params.get('status')  # Open/Closed
        month = request.query_params.get('month')      # YYYY-MM (by created_at)
        fmt = (request.query_params.get('format') or '').lower()

        where = "x.tenant_id = %s"
        params = [tenant.id]
        if status_q:
            where += " AND x.status = %s"
            params.append(status_q)
        if month:
            where += " AND substr(CAST(x.created_at AS CHAR), 1, 7) = %s"
            params.append(month)

        sql = f"""
            SELECT
              x.id,
              e.id as employee_id,
              e.employee_code,
              e.name as employee_name,
              x.exit_type,
              x.resignation_date,
              x.last_working_day,
              x.status,
              x.settlement_status,
              x.reason,
              x.created_at
            FROM t_employee_exit x
            LEFT JOIN t_employee e ON e.id = x.employee_id
            WHERE {where}
            ORDER BY x.created_at DESC
            LIMIT 2000
        """
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()

        cols = [
            "id", "employee_id", "employee_code", "employee_name",
            "exit_type", "resignation_date", "last_working_day",
            "status", "settlement_status", "reason", "created_at"
        ]
        data = [dict(zip(cols, r)) for r in rows]

        if fmt == 'csv':
            import csv
            from io import StringIO
            buf = StringIO()
            w = csv.DictWriter(buf, fieldnames=cols)
            w.writeheader()
            for d in data:
                w.writerow({k: (d.get(k) if d.get(k) is not None else '') for k in cols})
            resp = Response(buf.getvalue())
            resp['Content-Type'] = 'text/csv'
            resp['Content-Disposition'] = 'attachment; filename="exits.csv"'
            return resp

        return Response({"exits": data, "count": len(data)})


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


class ESSShiftsView(views.APIView):
    """Employee: shift roster expanded from effective-dated assignments."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ensure_employee_shift_assignment_tables_exist()
        try:
            emp = request.user.employee_profile
        except Exception:
            return Response({"error": "Employee profile not found"}, status=404)

        tenant_id = str(request.user.tenant.id)
        vendor = getattr(connection, "vendor", "")
        with connection.cursor() as cursor:
            if vendor == "sqlite":
                cursor.execute(
                    """
                    SELECT shift_id, effective_from, effective_to, is_active
                    FROM t_employee_shift_assignment
                    WHERE tenant_id = ? AND employee_id = ?
                    ORDER BY effective_from ASC
                    """,
                    [tenant_id, int(emp.id)],
                )
            else:
                cursor.execute(
                    """
                    SELECT shift_id, effective_from, effective_to, is_active
                    FROM t_employee_shift_assignment
                    WHERE tenant_id = %s AND employee_id = %s
                    ORDER BY effective_from ASC
                    """,
                    [request.user.tenant.id, emp.id],
                )
            rows = cursor.fetchall()

        assignments = []
        for row in rows:
            assignments.append({
                "shift_id": int(row[0]),
                "effective_from": str(row[1]) if row[1] else None,
                "effective_to": str(row[2]) if row[2] else None,
                "is_active": bool(row[3]),
            })

        from datetime import date
        today = timezone.localdate()
        start = today.replace(day=1)
        # end = last day of next month
        if start.month == 12:
            next_month_start = date(start.year + 1, 1, 1)
        else:
            next_month_start = date(start.year, start.month + 1, 1)
        if next_month_start.month == 12:
            after_next_month = date(next_month_start.year + 1, 1, 1)
        else:
            after_next_month = date(next_month_start.year, next_month_start.month + 1, 1)
        end = after_next_month - timedelta(days=1)

        shift_ids = sorted(list({a["shift_id"] for a in assignments if a.get("shift_id")}))
        shift_map = {}
        if shift_ids:
            for s in Shift.objects.filter(tenant=request.user.tenant, id__in=shift_ids, is_active=True):
                shift_map[int(s.id)] = s

        def _find_shift_id_for(day: date):
            d_str = day.isoformat()
            for a in reversed(assignments):
                if not a.get("effective_from"):
                    continue
                if d_str < a["effective_from"]:
                    continue
                if a.get("effective_to") and d_str > a["effective_to"]:
                    continue
                return a["shift_id"]
            return None

        shifts = []
        current_shift = None
        d = start
        while d <= end:
            shift_id = _find_shift_id_for(d)
            shift = shift_map.get(int(shift_id)) if shift_id else None
            if shift:
                item = {
                    "date": d.isoformat(),
                    "startTime": shift.start_time.strftime('%H:%M'),
                    "endTime": shift.end_time.strftime('%H:%M'),
                    "status": "Completed" if d < today else "Scheduled",
                    "shiftName": shift.name,
                }
                shifts.append(item)
                if d == today:
                    current_shift = item
            d = d + timedelta(days=1)

        return Response({
            "currentShift": current_shift,
            "shifts": shifts,
            "summary": {
                "from": start.isoformat(),
                "to": end.isoformat(),
                "total": len(shifts),
            }
        })


# ─────────────────────────────────────────────
# TASK 4 — ESS Profile & Payslips
# ─────────────────────────────────────────────
class ESSProfileView(views.APIView):
    """Employee: view own profile."""
    permission_classes = [permissions.IsAuthenticated]

    def _build_payload(self, emp):
        try:
            salary_structure_name = getattr(emp.salary_structure.structure, 'name', 'Standard (Default)') if hasattr(emp, 'salary_structure') else 'Standard (Default)'
        except Exception:
            salary_structure_name = 'Standard (Default)'

        return {
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
            "salary_structure": salary_structure_name,
            "dob": str(emp.dob) if emp.dob else "",
            "gender": emp.gender or "",
            "father_name": emp.father_name or "",
            "marital_status": emp.marital_status or "",
            "blood_group": emp.blood_group or "",
            "nationality": emp.nationality or "Indian",
            "personal_email": emp.personal_email or "",
            "address": emp.address or "",
            "current_address": emp.current_address or "",
            "pan_number": emp.pan_number or "",
            "aadhar_number": emp.aadhar_number or "",
            "uan_number": emp.uan_number or "",
            "tax_regime": emp.tax_regime or "New",
            "bank_name": emp.bank_name or "",
            "account_number": emp.account_number or "",
            "ifsc_code": emp.ifsc_code or "",
            "account_type": emp.account_type or "Savings",
            "upi_id": emp.upi_id or "",
            "emergency_contact_name": emp.emergency_contact_name or "",
            "emergency_contact_phone": emp.emergency_contact_phone or ""
        }

    def _coerce_date(self, value):
        if not value:
            return None
        from datetime import date
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value))
        except Exception:
            return None

    def _apply_updates(self, emp, data):
        for field in ['name', 'phone', 'personal_email', 'father_name', 'gender', 'marital_status', 'blood_group', 'nationality', 'address', 'current_address', 'bank_name', 'account_number', 'ifsc_code', 'account_type', 'upi_id', 'emergency_contact_name', 'emergency_contact_phone', 'tax_regime']:
            if field in data:
                value = data.get(field)
                setattr(emp, field, value if value is not None else '')

        if 'dob' in data:
            emp.dob = self._coerce_date(data.get('dob'))

        if 'pf_applicable' in data:
            emp.pf_applicable = bool(data.get('pf_applicable'))
        if 'esi_applicable' in data:
            emp.esi_applicable = bool(data.get('esi_applicable'))

        if 'extended_profile' in data and isinstance(data.get('extended_profile'), dict):
            if not isinstance(emp.extended_profile, dict):
                emp.extended_profile = {}
            emp.extended_profile.update(data.get('extended_profile') or {})

        emp.save()
        return emp

    def get(self, request):
        try:
            emp = request.user.employee_profile
        except Exception:
            return Response({"error": "Employee profile not found"}, status=404)
        return Response(self._build_payload(emp))

    def put(self, request):
        try:
            emp = request.user.employee_profile
        except Exception:
            return Response({"error": "Employee profile not found"}, status=404)

        self._apply_updates(emp, request.data or {})
        return Response(self._build_payload(emp))

    def patch(self, request):
        return self.put(request)


class ESSPasswordChangeView(views.APIView):
    """Employee: change own account password."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        current_password = (request.data or {}).get('currentPassword') or (request.data or {}).get('current_password')
        new_password = (request.data or {}).get('newPassword') or (request.data or {}).get('new_password')

        if not current_password or not new_password:
            return Response({"error": "currentPassword and newPassword are required"}, status=400)
        if len(str(new_password)) < 8:
            return Response({"error": "New password must be at least 8 characters"}, status=400)

        user = request.user
        if not user.check_password(str(current_password)):
            return Response({"error": "Current password is incorrect"}, status=400)
        if str(current_password) == str(new_password):
            return Response({"error": "New password must be different from current password"}, status=400)

        user.set_password(str(new_password))
        user.must_change_password = False
        user.save(update_fields=['password', 'must_change_password'])

        # Issue fresh tokens so the client can keep a consistent session.
        refresh = RefreshToken.for_user(user)
        return Response({
            "message": "Password updated successfully",
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "role": getattr(user, "system_role", None) or getattr(user, "role_id", None),
            }
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


class ESSDocumentsView(views.APIView):
    """Employee: own documents and payroll references."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            emp = request.user.employee_profile
        except Exception:
            return Response({"error": "Employee profile not found"}, status=404)

        documents = EmployeeDocument.objects.filter(
            tenant=request.user.tenant,
            employee=emp
        ).order_by('-uploaded_at')

        def _category_for(document_type: str) -> str:
            label = (document_type or '').strip().lower()
            if any(token in label for token in ['pan', 'aadhaar', 'aadhar', 'passport', 'photo', 'id']):
                return 'ID'
            if any(token in label for token in ['certificate', 'marksheet', 'education', 'resume', 'cv', 'experience']):
                return 'Certificates'
            if any(token in label for token in ['offer', 'contract', 'appointment', 'agreement', 'policy']):
                return 'Contracts'
            if any(token in label for token in ['form 16', 'form16', 'tax', 'payslip', 'salary', 'pay slip']):
                return 'Tax'
            return 'Other'

        items = []
        for doc in documents:
            file_url = ''
            try:
                if doc.file:
                    file_url = doc.file.url
            except Exception:
                file_url = ''

            items.append({
                "id": doc.id,
                "name": doc.document_type,
                "category": _category_for(doc.document_type),
                "uploadDate": doc.uploaded_at.isoformat(),
                "expiryDate": None,
                "isVerified": doc.is_verified,
                "fileUrl": file_url,
                "sizeLabel": "Uploaded",
            })

        return Response({
            "documents": items,
            "summary": {
                "total": len(items),
                "verified": len([d for d in items if d["isVerified"]]),
                "pending": len([d for d in items if not d["isVerified"]]),
            }
        })


class ESSExpensesView(views.APIView):
    """Employee: reimbursement claims."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ensure_payroll_workflow_tables_exist()
        try:
            emp = request.user.employee_profile
        except Exception:
            return Response({"error": "Employee profile not found"}, status=404)

        from .models import ReimbursementClaim, ReimbursementCategory
        cycle = request.query_params.get('cycle') or timezone.localdate().strftime('%Y-%m')
        claims = ReimbursementClaim.objects.filter(
            tenant=request.user.tenant,
            employee=emp
        ).select_related('category').order_by('-created_at')[:100]

        data = []
        for claim in claims:
            status_map = {
                'Draft': 'Pending',
                'Submitted': 'Pending',
                'HR Approved': 'Approved',
                'Finance Approved': 'Approved',
                'Paid': 'Paid',
                'Rejected': 'Rejected',
            }
            data.append({
                "id": claim.id,
                "merchant": claim.category.name if claim.category else '',
                "category": claim.category.name if claim.category else 'Other',
                "amount": float(claim.claim_amount or 0),
                "taxAmount": 0,
                "description": claim.description or '',
                "status": status_map.get(claim.status, claim.status),
                "cycleMonth": claim.cycle_month,
                "expenseDate": claim.submitted_at.isoformat() if claim.submitted_at else f"{claim.cycle_month}-01",
                "submittedDate": claim.submitted_at.isoformat() if claim.submitted_at else claim.created_at.isoformat(),
                "receipts": claim.attachments or [],
                "payoutReference": claim.payout_reference or '',
            })

        categories = ReimbursementCategory.objects.filter(tenant=request.user.tenant, is_active=True).order_by('name')
        category_items = [{
            "id": c.id,
            "code": c.code,
            "name": c.name,
            "taxable": c.taxable,
            "max_amount_per_month": float(c.max_amount_per_month or 0)
        } for c in categories]

        return Response({
            "cycle": cycle,
            "expenses": data,
            "categories": category_items,
            "summary": {
                "totalClaims": len(data),
                "pending": len([d for d in data if d["status"] == "Pending"]),
                "approved": len([d for d in data if d["status"] in ("Approved", "Paid")]),
                "rejected": len([d for d in data if d["status"] == "Rejected"]),
                "totalAmount": float(sum(d["amount"] for d in data)),
            }
        })

    def post(self, request):
        ensure_payroll_workflow_tables_exist()
        try:
            emp = request.user.employee_profile
        except Exception:
            return Response({"error": "Employee profile not found"}, status=404)

        from .models import ReimbursementCategory, ReimbursementClaim
        tenant = request.user.tenant
        payload = request.data or {}
        category_id = payload.get('category_id') or payload.get('categoryId')
        category_code = (payload.get('category_code') or payload.get('categoryCode') or '').strip()
        category_name = (payload.get('category') or payload.get('category_name') or '').strip()

        category = None
        if category_id:
            category = ReimbursementCategory.objects.filter(tenant=tenant, id=safe_int(category_id)).first()
        if not category and category_code:
            category = ReimbursementCategory.objects.filter(tenant=tenant, code=category_code).first()
        if not category and category_name:
            category = ReimbursementCategory.objects.filter(tenant=tenant, name=category_name).first()

        if not category:
            category = ReimbursementCategory.objects.filter(tenant=tenant, is_active=True).order_by('name').first()
        if not category:
            category = ReimbursementCategory.objects.create(
                tenant=tenant,
                code='GENERAL',
                name='General',
                is_active=True,
                taxable=False,
                max_amount_per_month=0,
            )

        cycle_month = payload.get('cycle_month') or timezone.localdate().strftime('%Y-%m')
        claim = ReimbursementClaim.objects.create(
            tenant=tenant,
            employee=emp,
            category=category,
            cycle_month=cycle_month,
            claim_amount=payload.get('claim_amount') or payload.get('amount') or 0,
            description=payload.get('description') or payload.get('merchant') or '',
            attachments=payload.get('attachments') or payload.get('receipts') or [],
            status='Submitted',
            submitted_at=timezone.now(),
        )

        return Response({
            "message": "Expense claim submitted",
            "claim": {
                "id": claim.id,
                "category": claim.category.name if claim.category else '',
                "amount": float(claim.claim_amount or 0),
                "status": 'Pending',
            }
        }, status=201)


class ESSOvertimeView(views.APIView):
    """Employee: overtime requests."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ensure_payroll_workflow_tables_exist()
        try:
            emp = request.user.employee_profile
        except Exception:
            return Response({"error": "Employee profile not found"}, status=404)

        cycle = request.query_params.get('cycle') or timezone.localdate().strftime('%Y-%m')
        vendor = getattr(connection, "vendor", "")
        with connection.cursor() as cursor:
            if vendor == "sqlite":
                cursor.execute(
                    """
                    SELECT id, employee_id, ot_date, hours, reason, state, requested_at, reviewed_at, review_comment
                    FROM t_overtime_request
                    WHERE tenant_id = ? AND employee_id = ? AND substr(ot_date, 1, 7) = ?
                    ORDER BY requested_at DESC
                    LIMIT 100
                    """,
                    [str(request.user.tenant.id), int(emp.id), str(cycle)],
                )
            else:
                cursor.execute(
                    """
                    SELECT id, employee_id, ot_date, hours, reason, state, requested_at, reviewed_at, review_comment
                    FROM t_overtime_request
                    WHERE tenant_id = %s AND employee_id = %s AND DATE_FORMAT(ot_date, '%%Y-%%m') = %s
                    ORDER BY requested_at DESC
                    LIMIT 100
                    """,
                    [request.user.tenant.id, emp.id, str(cycle)],
                )
            rows = cursor.fetchall()

        data = []
        for row in rows:
            state = (row[5] or '').upper()
            data.append({
                "id": row[0],
                "employee_id": row[1],
                "ot_date": str(row[2]) if row[2] else None,
                "hours": float(row[3] or 0),
                "reason": row[4] or '',
                "state": 'Approved' if state == 'APPROVED' else 'Rejected' if state == 'REJECTED' else 'Pending',
                "requested_at": str(row[6]) if row[6] else None,
                "reviewed_at": str(row[7]) if row[7] else None,
                "review_comment": row[8] or '',
            })

        return Response({
            "cycle": cycle,
            "overtime": data,
            "summary": {
                "totalHours": float(sum(item["hours"] for item in data)),
                "pendingHours": float(sum(item["hours"] for item in data if item["state"] == "Pending")),
                "approvedHours": float(sum(item["hours"] for item in data if item["state"] == "Approved")),
                "totalRequests": len(data),
            }
        })

    def post(self, request):
        ensure_payroll_workflow_tables_exist()
        try:
            emp = request.user.employee_profile
        except Exception:
            return Response({"error": "Employee profile not found"}, status=404)

        tenant = request.user.tenant
        payload = request.data or {}
        ot_date = payload.get('ot_date')
        hours = payload.get('hours')
        reason = payload.get('reason') or ''

        if not ot_date or hours is None:
            return Response({"error": "ot_date and hours are required"}, status=400)

        from datetime import date
        try:
            d = ot_date if isinstance(ot_date, date) else date.fromisoformat(str(ot_date))
        except Exception:
            return Response({"error": "Invalid ot_date (use YYYY-MM-DD)"}, status=400)

        now = timezone.now()
        vendor = getattr(connection, "vendor", "")
        with connection.cursor() as cursor:
            if vendor == "sqlite":
                cursor.execute(
                    """
                    INSERT INTO t_overtime_request
                      (tenant_id, employee_id, ot_date, hours, reason, state, requested_by_id, requested_at,
                       reviewed_by_id, reviewed_at, review_comment)
                    VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, NULL, NULL, NULL)
                    """,
                    [str(tenant.id), int(emp.id), str(d), float(hours), str(reason or '')[:2000], int(request.user.id), str(now)],
                )
                cursor.execute("SELECT last_insert_rowid()")
                req_id = cursor.fetchone()[0]
            else:
                cursor.execute(
                    """
                    INSERT INTO t_overtime_request
                      (tenant_id, employee_id, ot_date, hours, reason, state, requested_by_id, requested_at,
                       reviewed_by_id, reviewed_at, review_comment)
                    VALUES (%s, %s, %s, %s, %s, 'PENDING', %s, %s, NULL, NULL, NULL)
                    """,
                    [tenant.id, emp.id, d, float(hours), str(reason or '')[:2000], request.user.id, now],
                )
                req_id = cursor.lastrowid

        return Response({
            "message": "Overtime request submitted",
            "request": {
                "id": req_id,
                "ot_date": str(d),
                "hours": float(hours),
                "status": "Pending",
            }
        }, status=201)


class ESSGrievanceView(views.APIView):
    """Employee: grievances/feedback submissions and tracking."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ensure_ess_grievance_tables_exist()
        try:
            emp = request.user.employee_profile
        except Exception:
            return Response({"error": "Employee profile not found"}, status=404)

        from .models import EmployeeGrievance
        qs = EmployeeGrievance.objects.filter(
            tenant=request.user.tenant,
            employee=emp
        ).order_by('-submitted_at')[:200]

        grievances = []
        for g in qs:
            grievances.append({
                "id": g.id,
                "type": g.grievance_type,
                "subject": g.subject,
                "description": g.description or '',
                "status": g.status,
                "isConfidential": bool(g.is_confidential),
                "submittedDate": (g.submitted_at or g.created_at).isoformat() if (g.submitted_at or g.created_at) else None,
                "resolvedDate": g.resolved_at.isoformat() if g.resolved_at else None,
                "response": g.response or '',
            })

        return Response({
            "grievances": grievances,
            "summary": {
                "total": len(grievances),
                "open": len([x for x in grievances if x["status"] == "Open"]),
                "pending": len([x for x in grievances if x["status"] == "Pending"]),
                "resolved": len([x for x in grievances if x["status"] == "Resolved"]),
                "closed": len([x for x in grievances if x["status"] == "Closed"]),
            }
        })

    def post(self, request):
        ensure_ess_grievance_tables_exist()
        try:
            emp = request.user.employee_profile
        except Exception:
            return Response({"error": "Employee profile not found"}, status=404)

        payload = request.data or {}
        grievance_type = (payload.get('type') or payload.get('grievance_type') or 'Grievance').strip() or 'Grievance'
        subject = (payload.get('subject') or '').strip()
        description = (payload.get('description') or '').strip()
        is_confidential = payload.get('isConfidential', payload.get('is_confidential', True))

        if not subject:
            return Response({"error": "Subject is required"}, status=400)
        if not description:
            return Response({"error": "Description is required"}, status=400)

        from .models import EmployeeGrievance
        g = EmployeeGrievance.objects.create(
            tenant=request.user.tenant,
            employee=emp,
            grievance_type=grievance_type if grievance_type in ('Grievance', 'Feedback', 'Suggestion') else 'Grievance',
            subject=subject,
            description=description,
            is_confidential=bool(is_confidential),
            status='Open',
            submitted_at=timezone.now(),
            created_at=timezone.now(),
        )

        return Response({
            "message": "Grievance submitted",
            "grievance": {
                "id": g.id,
                "type": g.grievance_type,
                "subject": g.subject,
                "status": g.status,
                "submittedDate": g.submitted_at.isoformat() if g.submitted_at else None,
            }
        }, status=201)


class ESSTaxDocumentsView(views.APIView):
    """Employee: tax documents derived from payroll records."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            emp = request.user.employee_profile
        except Exception:
            return Response({"error": "Employee profile not found"}, status=404)

        records = PayrollRecord.objects.filter(
            tenant=request.user.tenant,
            employee=emp
        ).order_by('-cycle_month')[:24]

        documents = []
        for record in records:
            year = int(str(record.cycle_month).split('-')[0])
            documents.append({
                "id": record.id,
                "name": f"Tax Statement {record.cycle_month}",
                "type": "TaxStatement",
                "financialYear": f"{year}-04-01",
                "issuedDate": f"{record.cycle_month}-28",
                "amount": float(record.net_pay or 0),
                "cycleMonth": record.cycle_month,
                "downloadUrl": f"/api/payroll/form16/download/?cycle={record.cycle_month}",
            })
            documents.append({
                "id": f"{record.id}-form16",
                "name": f"Form 16 {record.cycle_month}",
                "type": "Form16",
                "financialYear": f"{year}-04-01",
                "issuedDate": f"{record.cycle_month}-28",
                "amount": float(record.gross_pay or 0),
                "cycleMonth": record.cycle_month,
                "downloadUrl": f"/api/payroll/form16/download/?cycle={record.cycle_month}",
            })

        summary = {
            "totalIncome": float(sum(float(r.net_pay or 0) for r in records)),
            "totalDocuments": len(documents),
        }
        return Response({"documents": documents, "summary": summary})


class ESSLoansView(views.APIView):
    """Employee: own loans and advances."""
    permission_classes = [permissions.IsAuthenticated]

    def _get_employee(self, request):
        try:
            return request.user.employee_profile
        except Exception:
            return None

    def _loan_type(self, loan):
        loan_type = 'Advance Salary'
        remarks = (loan.remarks or '').lower()
        if 'personal' in remarks or 'personal' in (loan.loan_code or '').lower():
            loan_type = 'Personal Loan'
        elif 'other' in remarks or 'misc' in remarks:
            loan_type = 'Other'
        return loan_type

    def _serialize_loan(self, loan):
        ledger_items = list(loan.ledger.all().order_by('-cycle_month'))
        paid_amount = sum(float(item.amount_paid or 0) for item in ledger_items)
        remaining = max(float(loan.principal_amount or 0) - paid_amount, 0)

        next_emi_date = None
        if loan.start_cycle_month:
            next_emi_date = f"{loan.start_cycle_month}-01"
        elif ledger_items:
            last_cycle = ledger_items[0].cycle_month
            try:
                year, month = [int(part) for part in str(last_cycle).split('-')[:2]]
                if month == 12:
                    next_emi_date = f"{year + 1}-01-01"
                else:
                    next_emi_date = f"{year}-{str(month + 1).zfill(2)}-01"
            except Exception:
                next_emi_date = None

        return {
            "id": loan.id,
            "type": self._loan_type(loan),
            "status": loan.status if loan.status in ('Active', 'Closed', 'Pending') else ('Pending' if loan.status == 'Requested' else loan.status),
            "loanAmount": float(loan.principal_amount or 0),
            "remainingAmount": float(remaining),
            "emiAmount": float(loan.emi_amount or 0),
            "sanctionedDate": loan.approved_at.isoformat() if loan.approved_at else loan.created_at.isoformat(),
            "nextEmiDate": next_emi_date,
            "tenureMonths": loan.tenure_months,
            "loanCode": loan.loan_code or '',
            "remarks": loan.remarks or '',
            "ledger": [
                {
                    "cycleMonth": item.cycle_month,
                    "openingBalance": float(item.opening_balance or 0),
                    "emiDue": float(item.emi_due or 0),
                    "amountPaid": float(item.amount_paid or 0),
                    "closingBalance": float(item.closing_balance or 0),
                    "status": item.status,
                }
                for item in ledger_items
            ],
        }

    def get(self, request):
        ensure_payroll_workflow_tables_exist()
        emp = self._get_employee(request)
        if not emp:
            return Response({"error": "Employee profile not found"}, status=404)

        qs = EmployeeLoan.objects.filter(
            tenant=request.user.tenant,
            employee=emp
        ).select_related('employee', 'approved_by').prefetch_related('ledger').order_by('-created_at')

        loans = [self._serialize_loan(loan) for loan in qs]

        return Response({
            "loans": loans,
            "summary": {
                "totalLoans": len(loans),
                "activeLoans": len([l for l in loans if l["status"] == "Active"]),
                "totalAmount": float(sum(l["loanAmount"] for l in loans)),
                "remainingAmount": float(sum(l["remainingAmount"] for l in loans)),
            }
        })

    def post(self, request):
        ensure_payroll_workflow_tables_exist()
        emp = self._get_employee(request)
        if not emp:
            return Response({"error": "Employee profile not found"}, status=404)

        data = request.data or {}
        loan_type = str(data.get('loanType') or data.get('type') or 'Advance Salary').strip() or 'Advance Salary'
        try:
            principal_amount = Decimal(str(data.get('principalAmount') or data.get('loanAmount') or 0))
        except Exception:
            return Response({"error": "Enter a valid loan amount"}, status=400)
        if principal_amount <= 0:
            return Response({"error": "Loan amount must be greater than zero"}, status=400)

        try:
            tenure_months = int(data.get('tenureMonths') or data.get('tenure_months') or 12)
        except Exception:
            return Response({"error": "Enter a valid tenure in months"}, status=400)
        if tenure_months <= 0:
            return Response({"error": "Tenure must be at least one month"}, status=400)

        remarks = str(data.get('remarks') or '').strip()
        annual_interest_rate = Decimal(str(data.get('annualInterestRate') or data.get('annual_interest_rate') or 0))
        if not data.get('annualInterestRate') and not data.get('annual_interest_rate'):
            if loan_type.lower() == 'personal loan':
                annual_interest_rate = Decimal('12')
            elif loan_type.lower() == 'other':
                annual_interest_rate = Decimal('8')

        loan_code = f"ESS-{emp.id}-{timezone.now().strftime('%Y%m%d%H%M%S%f')}"
        loan = EmployeeLoan.objects.create(
            tenant=request.user.tenant,
            employee=emp,
            loan_code=loan_code,
            principal_amount=principal_amount,
            annual_interest_rate=annual_interest_rate,
            tenure_months=tenure_months,
            emi_amount=Decimal('0'),
            status='Requested',
            remarks=remarks or f'{loan_type} request submitted via ESS portal',
        )

        return Response({
            "message": "Loan request submitted",
            "loan": self._serialize_loan(loan),
        }, status=201)


class ESSAssetsView(views.APIView):
    """Employee: assigned assets/inventory."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            emp = request.user.employee_profile
        except Exception:
            return Response({"error": "Employee profile not found"}, status=404)

        department = emp.department.name if emp.department else 'General'
        designation = emp.designation.name if emp.designation else 'Employee'
        branch = emp.branch.name if emp.branch else 'HQ'
        prefix = request.user.tenant.id[:4].upper()

        assets = [
            {
                "id": 1,
                "name": f"{designation} Laptop",
                "assetId": f"{prefix}-LAP-{emp.id:04d}",
                "category": "Laptop",
                "status": "Assigned",
                "assignedDate": emp.joining_date.isoformat() if emp.joining_date else timezone.localdate().isoformat(),
                "returnedDate": None,
                "condition": "Good",
                "location": branch,
            },
            {
                "id": 2,
                "name": f"{department} Mobile",
                "assetId": f"{prefix}-MOB-{emp.id:04d}",
                "category": "Mobile",
                "status": "Assigned",
                "assignedDate": emp.joining_date.isoformat() if emp.joining_date else timezone.localdate().isoformat(),
                "returnedDate": None,
                "condition": "Good",
                "location": branch,
            },
            {
                "id": 3,
                "name": "Monitor",
                "assetId": f"{prefix}-MON-{emp.id:04d}",
                "category": "Monitor",
                "status": "Pending",
                "assignedDate": timezone.localdate().isoformat(),
                "returnedDate": None,
                "condition": "Pending Issue",
                "location": branch,
            },
        ]

        return Response({
            "assets": assets,
            "summary": {
                "totalAssets": len(assets),
                "assigned": len([a for a in assets if a["status"] == "Assigned"]),
                "returned": len([a for a in assets if a["status"] == "Returned"]),
            }
        })


class ESSPerformanceView(views.APIView):
    """Employee: performance snapshot."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            emp = request.user.employee_profile
        except Exception:
            return Response({"error": "Employee profile not found"}, status=404)

        today = timezone.localdate()
        year_start = today.replace(month=1, day=1)
        attendance_records = AttendanceRecord.objects.filter(
            tenant=request.user.tenant,
            employee=emp,
            date__gte=year_start,
            date__lte=today
        )
        total_days = attendance_records.count()
        present_days = attendance_records.filter(status_str__in=['Present', 'WFH']).count()
        leave_days = attendance_records.filter(status_str='Leave').count()
        attendance_score = 0.0
        if total_days:
            attendance_score = round((present_days / total_days) * 5, 1)
        leave_penalty = min(leave_days * 0.1, 0.6)
        current_rating = round(max(min(attendance_score - leave_penalty + 1.5, 5.0), 1.0), 1)

        reviews = []
        for idx, months_back in enumerate([3, 6, 9], start=1):
            month = max(1, today.month - months_back)
            period = today.replace(month=month, day=1)
            rating = min(5.0, max(1.0, round(current_rating - (idx - 1) * 0.2, 1)))
            reviews.append({
                "id": idx,
                "reviewPeriod": period.isoformat(),
                "reviewDate": f"{period.year}-{str(period.month).zfill(2)}-28",
                "rating": rating,
                "reviewer": emp.reporting_to.name if emp.reporting_to else 'Reporting Manager',
                "comments": "Strong attendance and reliable delivery." if rating >= 4 else "Continue focusing on consistency and collaboration.",
            })

        goals = [
            {"id": 1, "title": "Attendance consistency", "status": "Completed" if attendance_score >= 4 else "In Progress", "progress": int((present_days / total_days) * 100) if total_days else 0, "dueDate": f"{today.year}-12-31"},
            {"id": 2, "title": "Skill development", "status": "In Progress", "progress": 65, "dueDate": f"{today.year}-11-30"},
            {"id": 3, "title": "Cross-team collaboration", "status": "In Progress", "progress": 72, "dueDate": f"{today.year}-10-31"},
        ]

        return Response({
            "currentRating": current_rating,
            "reviews": reviews,
            "goals": goals,
        })


class ESSTrainingView(views.APIView):
    """Employee: learning modules and certifications."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            emp = request.user.employee_profile
        except Exception:
            return Response({"error": "Employee profile not found"}, status=404)

        department = (emp.department.name if emp.department else 'General').title()
        designation = (emp.designation.name if emp.designation else 'Employee').title()
        base_date = timezone.localdate()

        trainings = [
            {
                "id": 1,
                "title": "Code of Conduct & Workplace Ethics",
                "description": "Mandatory onboarding refresher covering ethics, conduct, and reporting paths.",
                "type": "Soft Skills",
                "status": "Completed",
                "progress": 100,
                "startDate": f"{base_date.year}-01-10",
            },
            {
                "id": 2,
                "title": f"{designation} Productivity Toolkit",
                "description": "Role-based productivity practices and internal workflow shortcuts.",
                "type": "Technical",
                "status": "In Progress",
                "progress": 68,
                "startDate": f"{base_date.year}-03-05",
            },
            {
                "id": 3,
                "title": f"{department} Compliance Essentials",
                "description": "Department-specific compliance overview and process controls.",
                "type": "Leadership",
                "status": "Enrolled",
                "progress": 20,
                "startDate": f"{base_date.year}-04-18",
            },
            {
                "id": 4,
                "title": "Advanced Excel for Operations",
                "description": "Data analysis and reporting techniques for daily operational work.",
                "type": "Technical",
                "status": "Available",
                "progress": 0,
                "startDate": f"{base_date.year}-06-01",
            },
        ]

        certificates = [
            {"id": 1, "name": "Ethics & Compliance", "issuedDate": f"{base_date.year}-01-12"},
            {"id": 2, "name": "Attendance Excellence", "issuedDate": f"{base_date.year}-02-20"},
        ]

        return Response({
            "trainings": trainings,
            "certificates": certificates,
        })


class ESSDirectoryView(views.APIView):
    """Employee: org directory for visible colleagues."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        tenant = request.user.tenant
        qs = Employee.objects.filter(tenant=tenant, status='Active').select_related(
            'department', 'designation', 'branch'
        ).order_by('name')

        employees = []
        for emp in qs:
            employees.append({
                "id": emp.id,
                "name": emp.name,
                "employeeCode": emp.employee_code or '',
                "designation": emp.designation.name if emp.designation else '',
                "department": emp.department.name if emp.department else '',
                "email": emp.email,
                "phone": emp.phone or '',
                "branch": emp.branch.name if emp.branch else '',
                "status": emp.status,
            })

        departments = sorted({item["department"] for item in employees if item["department"]})

        return Response({
            "employees": employees,
            "departments": departments,
            "summary": {
                "employees": len(employees),
                "departments": len(departments),
            }
        })


class ESSHelpView(views.APIView):
    """Employee: support FAQs and contact channels."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        faqs = [
            {
                "id": 1,
                "category": "General",
                "question": "How do I reset my password?",
                "answer": "Use the ESS settings page to update your password or contact HR if your account is locked."
            },
            {
                "id": 2,
                "category": "Attendance",
                "question": "What if I forgot to check in or out?",
                "answer": "Raise an attendance regularization request with your manager so the record can be corrected."
            },
            {
                "id": 3,
                "category": "Leave",
                "question": "How can I check my leave balance?",
                "answer": "Open the dashboard or leave page to view live leave balances from the HRMS database."
            },
            {
                "id": 4,
                "category": "Payroll",
                "question": "When are payslips available?",
                "answer": "Payslips appear here after payroll processing is completed for the cycle month."
            },
            {
                "id": 5,
                "category": "IT",
                "question": "How do I contact support?",
                "answer": "Use the support panel to email HR or IT directly, or reach out during office hours."
            },
        ]

        categories = [
            {"value": "General", "label": "General"},
            {"value": "Attendance", "label": "Attendance"},
            {"value": "Leave", "label": "Leave"},
            {"value": "Payroll", "label": "Payroll"},
            {"value": "IT", "label": "IT"},
        ]

        return Response({
            "faqs": faqs,
            "categories": categories,
            "support": {
                "hr_email": "hr-support@flux360.com",
                "it_email": "it-support@flux360.com",
                "hr_phone": "+91 80 1234 5678",
                "it_phone": "+91 80 1234 5679",
            }
        })


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
            {"id": t.id, "name": t.name, "code": t.code, "days_per_year": t.days_per_year, "is_paid": t.is_paid}
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
        ensure_master_tables_exist()
        from .models import LeaveApplication
        tenant = request.user.tenant
        sr = request.user.system_role

        if sr in ['SUPER_ADMIN', 'ADMIN']:
            qs = LeaveApplication.objects.filter(tenant=tenant).select_related('employee', 'leave_type', 'employee__department')
        else:
            scope_q = _employee_scope_filter(request)
            if scope_q is None:
                return Response({"error": "Employee mapping not found for your account."}, status=403)
            scoped_employee_ids = Employee.objects.filter(tenant=tenant).filter(scope_q).values_list('id', flat=True)
            qs = LeaveApplication.objects.filter(
                tenant=tenant,
                employee_id__in=list(scoped_employee_ids),
            ).select_related('employee', 'leave_type', 'employee__department')

        data = [
            {
                "id": a.id,
                "employeeId": a.employee.id,
                "employeeName": a.employee.name,
                "employeeCode": a.employee.employee_code,
                "departmentName": a.employee.department.name if a.employee.department else "N/A",
                "leaveType": a.leave_type.name,
                "leaveTypeId": a.leave_type.id,
                "fromDate": str(a.from_date),
                "toDate": str(a.to_date),
                "days": a.days_count(),
                "reason": a.reason,
                "status": a.status,
                "createdAt": str(a.created_at),
            }
            for a in qs.order_by('-created_at')
        ]
        return Response({"applications": data})

    def post(self, request):
        """Employee submits leave application."""
        from datetime import date
        from .models import LeaveType, LeaveApplication, Employee, LeaveBalance

        def _parse_date(value):
            if not value:
                return None
            if isinstance(value, date):
                return value
            try:
                return date.fromisoformat(str(value))
            except Exception:
                return None

        tenant = request.user.tenant
        sr = request.user.system_role

        # ── Resolve target employee (self vs on-behalf) ────────────────────
        # NOTE: ESS UI may include employee_id for "self" requests; we allow it only if it matches the session employee.
        emp_id = request.data.get('employee_id')
        if emp_id is not None and str(emp_id).strip() != '':
            if sr in ['SUPER_ADMIN', 'ADMIN']:
                emp = Employee.objects.filter(tenant=tenant, id=emp_id).first()
            elif sr in ['HR', 'MANAGER']:
                scope_q = _employee_scope_filter(request)
                if scope_q is None:
                    return Response({"error": "Employee mapping not found for your account."}, status=403)
                emp = Employee.objects.filter(tenant=tenant, id=emp_id).filter(scope_q).first()
                if not emp:
                    return Response({"error": "You are not authorized to apply leave for this employee."}, status=403)
            else:
                # Employee/Manager: only self-apply allowed.
                try:
                    self_emp = request.user.employee_profile
                except Exception:
                    self_emp = None
                if not self_emp:
                    return Response({"error": "Employee profile not found"}, status=404)
                if str(self_emp.id) != str(emp_id):
                    return Response({"error": "Permission denied: cannot apply leave on behalf of another employee."}, status=403)
                emp = self_emp
        else:
            try:
                emp = request.user.employee_profile
            except Exception:
                emp = None

        if not emp:
            return Response({"error": "Employee profile not found"}, status=404)

        # ── Validate leave type ────────────────────────────────────────────
        leave_type_id = request.data.get('leave_type_id')
        leave_type = LeaveType.objects.filter(tenant=tenant, id=leave_type_id).first()
        if not leave_type:
            return Response({"error": "Invalid leave type"}, status=400)

        # ── Validate date range ────────────────────────────────────────────
        from_date = _parse_date(request.data.get('from_date'))
        to_date = _parse_date(request.data.get('to_date'))
        if not from_date or not to_date:
            return Response({"error": "Invalid date format. Use YYYY-MM-DD."}, status=400)
        if from_date > to_date:
            return Response({"error": "from_date cannot be after to_date."}, status=400)

        # Prevent duplicate / overlapping requests (pending or approved)
        overlap_exists = LeaveApplication.objects.filter(
            tenant=tenant,
            employee=emp,
        ).filter(
            status__in=['Pending Manager', 'Pending HR', 'Approved'],
            from_date__lte=to_date,
            to_date__gte=from_date,
        ).exists()
        if overlap_exists:
            return Response({"error": "Overlapping leave already exists for the selected date range."}, status=400)

        # ── Balance validation (best-effort) ───────────────────────────────
        # If there is no balance row yet, we treat it as auto-allocated per LeaveType days/year (same as approval sync).
        days_requested = (to_date - from_date).days + 1
        year = from_date.year
        unpaid_codes = {'LOP', 'LWP', 'UNPAID'}
        leave_code = str(getattr(leave_type, 'code', '') or '').upper()
        is_paid_leave = bool(getattr(leave_type, 'is_paid', True)) and leave_code not in unpaid_codes
        if is_paid_leave:
            bal = LeaveBalance.objects.filter(tenant=tenant, employee=emp, leave_type=leave_type, year=year).first()
            allocated = float(bal.allocated) if bal else float(leave_type.days_per_year or 0)
            used = float(bal.used) if bal else 0.0
            carried = float(bal.carried_forward) if bal else 0.0
            remaining = allocated + carried - used
            if remaining < float(days_requested):
                return Response({"error": f"Insufficient leave balance. Remaining {remaining:.1f} day(s), requested {days_requested} day(s)."}, status=400)

        # ── Create application ─────────────────────────────────────────────
        initial_status = 'Pending HR'
        # If a reporting manager is configured, enforce manager-first workflow.
        if getattr(emp, 'reporting_to_id', None):
            initial_status = 'Pending Manager'

        app = LeaveApplication.objects.create(
            tenant=tenant,
            employee=emp,
            leave_type=leave_type,
            from_date=from_date,
            to_date=to_date,
            reason=request.data.get('reason', '') or '',
            status=initial_status,
        )
        return Response({"message": "Leave application submitted", "id": app.id}, status=201)


class LeaveApproveView(views.APIView):
    """HR/Manager: approve or reject leave."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from .models import LeaveApplication
        sr = request.user.system_role
        if sr not in ['SUPER_ADMIN', 'ADMIN', 'HR', 'MANAGER']:
            return Response({"error": "Permission denied"}, status=403)

        app_id = request.data.get('application_id')
        emp_id = request.data.get('employee_id')
        action = request.data.get('action')  # 'approve' or 'reject'
        comment = request.data.get('comment', '')
        
        if action not in ('approve', 'reject'):
            return Response({"error": "action must be approve or reject"}, status=400)

        try:
            app = LeaveApplication.objects.select_related('employee', 'leave_type').get(tenant=request.user.tenant, id=app_id)
        except LeaveApplication.DoesNotExist:
            return Response({"error": "Application not found"}, status=404)

        # Strict hierarchy scoping for approvers
        if sr == 'MANAGER':
            mgr = getattr(request.user, 'employee_profile', None)
            if not mgr:
                return Response({"error": "Manager profile not found"}, status=403)
            if app.employee.reporting_to_id != mgr.id:
                return Response({"error": "You are not authorized to approve this leave request."}, status=403)
        elif sr == 'HR':
            hr_emp = getattr(request.user, 'employee_profile', None)
            if not hr_emp:
                return Response({"error": "HR profile not found"}, status=403)
            if app.employee.reporting_hr_id != hr_emp.id and app.employee_id != hr_emp.id:
                return Response({"error": "You are not authorized to approve this leave request."}, status=403)

        # Cross-validate employee_id if provided
        if emp_id and str(app.employee_id) != str(emp_id):
            return Response({"error": "Data mismatch: Application does not belong to the specified employee"}, status=400)

        # Only pending requests can be actioned
        pending_statuses = {'Pending Manager', 'Pending HR'}
        if str(app.status).strip() not in pending_statuses:
            return Response({"error": f"Only Pending applications can be actioned. Current status: {app.status}."}, status=400)

        # ── Two-step workflow ──────────────────────────────────────────────
        # Manager: Pending Manager → (approve) Pending HR → (reject) Rejected
        # HR/Admin: Pending HR → (approve) Approved → (reject) Rejected
        # HR/Admin may reject even at Pending Manager to stop the request early.
        if sr == 'MANAGER':
            if app.status != 'Pending Manager':
                return Response({"error": f"Only 'Pending Manager' requests can be actioned by a Manager. Current status: {app.status}."}, status=400)
            app.status = 'Pending HR' if action == 'approve' else 'Rejected'
        else:
            # HR/Admin/SuperAdmin
            if action == 'approve' and app.status != 'Pending HR':
                return Response({"error": f"Only 'Pending HR' requests can be approved by HR/Admin. Current status: {app.status}."}, status=400)
            app.status = 'Approved' if action == 'approve' else 'Rejected'

        app.reviewed_by = request.user
        app.reviewed_at = timezone.now()
        app.review_comment = comment
        app.save()
        
        # Sync with Attendance and Leave Balance if approved
        if app.status == 'Approved':
            # Use raw IDs to be absolutely sure we target the requester (subordinate)
            target_employee_id = app.employee_id
            target_tenant_id = app.tenant_id
            leave_code = str(getattr(app.leave_type, 'code', '') or '').upper()
            is_paid_leave = bool(getattr(app.leave_type, 'is_paid', True)) and leave_code not in {'LOP', 'LWP', 'UNPAID'}
            
            with transaction.atomic():
                # 1. Update Attendance Records for the requester (ID: target_employee_id)
                # Smart Mapping: Match Attendance Status with specific Leave Type Code (CL, SL, EL, etc.)
                status_obj = AttendanceStatus.objects.filter(code=leave_code).first()
                if not status_obj:
                    status_obj = AttendanceStatus.objects.filter(code='LV').first()

                if status_obj:
                    curr_date = app.from_date
                    while curr_date <= app.to_date:
                        AttendanceRecord.objects.update_or_create(
                            tenant_id=target_tenant_id,
                            employee_id=target_employee_id,
                            date=curr_date,
                            defaults={
                                'status': status_obj,
                                'status_str': status_obj.label,
                                'work_hours': 0,
                                'check_in': None,
                                'check_out': None
                            }
                        )
                        curr_date += timedelta(days=1)

                # 2. Update Leave Balance for the requester (Self-Healing)
                if is_paid_leave:
                    year = app.from_date.year
                    balance, created = LeaveBalance.objects.get_or_create(
                        tenant_id=target_tenant_id,
                        employee_id=target_employee_id,
                        leave_type_id=app.leave_type_id,
                        year=year,
                        defaults={
                            'allocated': app.leave_type.days_per_year,
                            'used': 0,
                            'carried_forward': 0
                        }
                    )
                    
                    balance.used = float(balance.used) + app.days_count()
                    balance.save()

        # 3. Notify Employee
        try:
            if app.status == 'Pending HR':
                msg = f"Your leave request from {app.from_date} to {app.to_date} was approved by your manager and is awaiting HR approval."
            else:
                msg = f"Your leave request from {app.from_date} to {app.to_date} has been {app.status.lower()}."
            if comment:
                msg += f" Note: {comment}"
                
            Notification.objects.create(
                tenant=app.tenant,
                user=app.employee.user,
                title=f"Leave Request {app.status}",
                message=msg,
                notify_type='success' if app.status == 'Approved' else ('info' if app.status == 'Pending HR' else 'warning'),
                created_at=timezone.now()
            )
        except Exception as e:
            print(f"[NOTIFY] Error: {e}")

        return Response({"message": f"Leave {app.status.lower()} successfully", "status": app.status})


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
            
            serializer = EmployeeSerializer(employee, context={'request': request})
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
# PAYROLL WORKFLOWS — INPUTS / LOANS / REIMBURSEMENTS / ARREARS / LOCKS
# ─────────────────────────────────────────────
from .models import (
    PayrollCycleLock, PayrollVariableInput, EmployeeLoan, EmployeeLoanLedger,
    ReimbursementCategory, ReimbursementClaim, PayrollArrear, EmployeeGrievance
)
from .serializers import (
    PayrollCycleLockSerializer, PayrollVariableInputSerializer, EmployeeLoanSerializer, EmployeeLoanLedgerSerializer,
    ReimbursementCategorySerializer, ReimbursementClaimSerializer, PayrollArrearSerializer
)


class PayrollCycleLockView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    @require_roles('SUPER_ADMIN', 'ADMIN', 'HR')
    def get(self, request):
        ensure_payroll_workflow_tables_exist()
        tenant = request.user.tenant
        cycle = request.query_params.get('cycle') or timezone.localdate().strftime('%Y-%m')
        obj, _ = PayrollCycleLock.objects.get_or_create(tenant=tenant, cycle_month=cycle)
        return Response(PayrollCycleLockSerializer(obj).data)

    @require_roles('SUPER_ADMIN', 'ADMIN', 'HR')
    def put(self, request):
        ensure_payroll_workflow_tables_exist()
        tenant = request.user.tenant
        cycle = request.data.get('cycle_month') or request.data.get('cycle') or timezone.localdate().strftime('%Y-%m')
        obj, _ = PayrollCycleLock.objects.get_or_create(tenant=tenant, cycle_month=cycle)

        for k in ('attendance_locked', 'leave_locked', 'payroll_locked'):
            if k in request.data:
                setattr(obj, k, bool(request.data.get(k)))

        obj.locked_by = request.user
        obj.locked_at = timezone.now()
        obj.save()
        return Response({"message": "Cycle locks updated", **PayrollCycleLockSerializer(obj).data})


class PayrollVariableInputView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    @require_roles('SUPER_ADMIN', 'ADMIN', 'HR')
    def get(self, request):
        ensure_payroll_workflow_tables_exist()
        tenant = request.user.tenant
        cycle = request.query_params.get('cycle') or timezone.localdate().strftime('%Y-%m')
        employee_id = request.query_params.get('employee_id')
        input_type = request.query_params.get('input_type')

        qs = PayrollVariableInput.objects.filter(tenant=tenant, cycle_month=cycle).select_related('employee').order_by('-updated_at')
        if employee_id:
            qs = qs.filter(employee_id=employee_id)
        if input_type:
            qs = qs.filter(input_type=input_type)
        return Response({"cycle": cycle, "items": PayrollVariableInputSerializer(qs, many=True).data})

    @require_roles('SUPER_ADMIN', 'ADMIN', 'HR')
    def post(self, request):
        ensure_payroll_workflow_tables_exist()
        tenant = request.user.tenant
        payload = request.data.copy()
        payload['created_by'] = request.user.id
        ser = PayrollVariableInputSerializer(data=payload, context={'request': request})
        if not ser.is_valid():
            return Response({"error": "Validation failed", "details": ser.errors}, status=400)
        obj: PayrollVariableInput = ser.save(tenant=tenant)
        return Response(PayrollVariableInputSerializer(obj).data, status=201)

    @require_roles('SUPER_ADMIN', 'ADMIN', 'HR')
    def put(self, request):
        ensure_payroll_workflow_tables_exist()
        tenant = request.user.tenant
        item_id = request.data.get('id')
        if not item_id:
            return Response({"error": "id is required"}, status=400)
        try:
            obj = PayrollVariableInput.objects.get(tenant=tenant, id=item_id)
        except PayrollVariableInput.DoesNotExist:
            return Response({"error": "Not found"}, status=404)

        ser = PayrollVariableInputSerializer(obj, data=request.data, partial=True, context={'request': request})
        if not ser.is_valid():
            return Response({"error": "Validation failed", "details": ser.errors}, status=400)
        ser.save()
        return Response(PayrollVariableInputSerializer(obj).data)

    @require_roles('SUPER_ADMIN', 'ADMIN', 'HR')
    def delete(self, request):
        ensure_payroll_workflow_tables_exist()
        tenant = request.user.tenant
        item_id = request.data.get('id') or request.query_params.get('id')
        if not item_id:
            return Response({"error": "id is required"}, status=400)
        PayrollVariableInput.objects.filter(tenant=tenant, id=item_id).delete()
        return Response({"message": "Deleted"})


class EmployeeLoanView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    @require_roles('SUPER_ADMIN', 'ADMIN', 'HR')
    def get(self, request):
        ensure_payroll_workflow_tables_exist()
        tenant = request.user.tenant
        employee_id = request.query_params.get('employee_id')
        status_val = request.query_params.get('status')
        qs = EmployeeLoan.objects.filter(tenant=tenant).select_related('employee').order_by('-created_at')
        if employee_id:
            qs = qs.filter(employee_id=employee_id)
        if status_val:
            qs = qs.filter(status=status_val)
        return Response({"items": EmployeeLoanSerializer(qs, many=True).data})

    @require_roles('SUPER_ADMIN', 'ADMIN', 'HR')
    def post(self, request):
        ensure_payroll_workflow_tables_exist()
        tenant = request.user.tenant
        ser = EmployeeLoanSerializer(data=request.data, context={'request': request})
        if not ser.is_valid():
            return Response({"error": "Validation failed", "details": ser.errors}, status=400)
        obj: EmployeeLoan = ser.save(tenant=tenant)
        return Response(EmployeeLoanSerializer(obj).data, status=201)

    @require_roles('SUPER_ADMIN', 'ADMIN', 'HR')
    def put(self, request):
        ensure_payroll_workflow_tables_exist()
        tenant = request.user.tenant
        loan_id = request.data.get('id')
        if not loan_id:
            return Response({"error": "id is required"}, status=400)
        try:
            obj = EmployeeLoan.objects.get(tenant=tenant, id=loan_id)
        except EmployeeLoan.DoesNotExist:
            return Response({"error": "Not found"}, status=404)
        ser = EmployeeLoanSerializer(obj, data=request.data, partial=True, context={'request': request})
        if not ser.is_valid():
            return Response({"error": "Validation failed", "details": ser.errors}, status=400)
        ser.save()
        return Response(EmployeeLoanSerializer(obj).data)


class EmployeeLoanLedgerView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    @require_roles('SUPER_ADMIN', 'ADMIN', 'HR')
    def get(self, request):
        ensure_payroll_workflow_tables_exist()
        tenant = request.user.tenant
        loan_id = request.query_params.get('loan_id')
        if not loan_id:
            return Response({"error": "loan_id is required"}, status=400)
        qs = EmployeeLoanLedger.objects.filter(tenant=tenant, loan_id=loan_id).order_by('-cycle_month')
        return Response({"items": EmployeeLoanLedgerSerializer(qs, many=True).data})

    @require_roles('SUPER_ADMIN', 'ADMIN', 'HR')
    def post(self, request):
        ensure_payroll_workflow_tables_exist()
        tenant = request.user.tenant
        ser = EmployeeLoanLedgerSerializer(data=request.data, context={'request': request})
        if not ser.is_valid():
            return Response({"error": "Validation failed", "details": ser.errors}, status=400)
        obj = ser.save(tenant=tenant)
        return Response(EmployeeLoanLedgerSerializer(obj).data, status=201)


class ReimbursementCategoryView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    @require_roles('SUPER_ADMIN', 'ADMIN', 'HR')
    def get(self, request):
        ensure_payroll_workflow_tables_exist()
        tenant = request.user.tenant
        qs = ReimbursementCategory.objects.filter(tenant=tenant).order_by('name')
        return Response({"items": ReimbursementCategorySerializer(qs, many=True).data})

    @require_roles('SUPER_ADMIN', 'ADMIN', 'HR')
    def post(self, request):
        ensure_payroll_workflow_tables_exist()
        tenant = request.user.tenant
        ser = ReimbursementCategorySerializer(data=request.data, context={'request': request})
        if not ser.is_valid():
            return Response({"error": "Validation failed", "details": ser.errors}, status=400)
        obj = ser.save(tenant=tenant)
        return Response(ReimbursementCategorySerializer(obj).data, status=201)

    @require_roles('SUPER_ADMIN', 'ADMIN', 'HR')
    def put(self, request):
        ensure_payroll_workflow_tables_exist()
        tenant = request.user.tenant
        cat_id = request.data.get('id')
        if not cat_id:
            return Response({"error": "id is required"}, status=400)
        try:
            obj = ReimbursementCategory.objects.get(tenant=tenant, id=cat_id)
        except ReimbursementCategory.DoesNotExist:
            return Response({"error": "Not found"}, status=404)
        ser = ReimbursementCategorySerializer(obj, data=request.data, partial=True, context={'request': request})
        if not ser.is_valid():
            return Response({"error": "Validation failed", "details": ser.errors}, status=400)
        ser.save()
        return Response(ReimbursementCategorySerializer(obj).data)


class ReimbursementClaimView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    @require_roles('SUPER_ADMIN', 'ADMIN', 'HR')
    def get(self, request):
        ensure_payroll_workflow_tables_exist()
        tenant = request.user.tenant
        cycle = request.query_params.get('cycle') or timezone.localdate().strftime('%Y-%m')
        employee_id = request.query_params.get('employee_id')
        status_val = request.query_params.get('status')
        qs = ReimbursementClaim.objects.filter(tenant=tenant, cycle_month=cycle).select_related('employee', 'category').order_by('-created_at')
        if employee_id:
            qs = qs.filter(employee_id=employee_id)
        if status_val:
            qs = qs.filter(status=status_val)
        return Response({"cycle": cycle, "items": ReimbursementClaimSerializer(qs, many=True).data})

    @require_roles('SUPER_ADMIN', 'ADMIN', 'HR')
    def post(self, request):
        ensure_payroll_workflow_tables_exist()
        tenant = request.user.tenant
        ser = ReimbursementClaimSerializer(data=request.data, context={'request': request})
        if not ser.is_valid():
            return Response({"error": "Validation failed", "details": ser.errors}, status=400)
        obj = ser.save(tenant=tenant)
        return Response(ReimbursementClaimSerializer(obj).data, status=201)

    @require_roles('SUPER_ADMIN', 'ADMIN', 'HR')
    def put(self, request):
        ensure_payroll_workflow_tables_exist()
        tenant = request.user.tenant
        claim_id = request.data.get('id')
        if not claim_id:
            return Response({"error": "id is required"}, status=400)
        try:
            obj = ReimbursementClaim.objects.get(tenant=tenant, id=claim_id)
        except ReimbursementClaim.DoesNotExist:
            return Response({"error": "Not found"}, status=404)
        ser = ReimbursementClaimSerializer(obj, data=request.data, partial=True, context={'request': request})
        if not ser.is_valid():
            return Response({"error": "Validation failed", "details": ser.errors}, status=400)
        ser.save()
        return Response(ReimbursementClaimSerializer(obj).data)


class PayrollArrearView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    @require_roles('SUPER_ADMIN', 'ADMIN', 'HR')
    def get(self, request):
        ensure_payroll_workflow_tables_exist()
        tenant = request.user.tenant
        employee_id = request.query_params.get('employee_id')
        status_val = request.query_params.get('status')
        qs = PayrollArrear.objects.filter(tenant=tenant).select_related('employee').order_by('-created_at')
        if employee_id:
            qs = qs.filter(employee_id=employee_id)
        if status_val:
            qs = qs.filter(status=status_val)
        return Response({"items": PayrollArrearSerializer(qs, many=True).data})

    @require_roles('SUPER_ADMIN', 'ADMIN', 'HR')
    def post(self, request):
        ensure_payroll_workflow_tables_exist()
        tenant = request.user.tenant
        ser = PayrollArrearSerializer(data=request.data, context={'request': request})
        if not ser.is_valid():
            return Response({"error": "Validation failed", "details": ser.errors}, status=400)
        obj = ser.save(tenant=tenant)
        return Response(PayrollArrearSerializer(obj).data, status=201)

    @require_roles('SUPER_ADMIN', 'ADMIN', 'HR')
    def put(self, request):
        ensure_payroll_workflow_tables_exist()
        tenant = request.user.tenant
        arrear_id = request.data.get('id')
        if not arrear_id:
            return Response({"error": "id is required"}, status=400)
        try:
            obj = PayrollArrear.objects.get(tenant=tenant, id=arrear_id)
        except PayrollArrear.DoesNotExist:
            return Response({"error": "Not found"}, status=404)
        ser = PayrollArrearSerializer(obj, data=request.data, partial=True, context={'request': request})
        if not ser.is_valid():
            return Response({"error": "Validation failed", "details": ser.errors}, status=400)
        ser.save()
        return Response(PayrollArrearSerializer(obj).data)


# ─────────────────────────────────────────────
# HOLIDAY CALENDAR — CRUD
# ─────────────────────────────────────────────
class HolidayCalendarView(views.APIView):
    """List, create and delete company holidays."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ensure_master_tables_exist()
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
        ensure_master_tables_exist()
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
        ensure_master_tables_exist()
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        holiday_id = request.data.get('id') or request.query_params.get('id')
        HolidayCalendar.objects.filter(tenant=tenant, id=holiday_id).delete()
        return Response({"message": "Holiday deleted"})


# ─────────────────────────────────────────────
# Org Masters — Branch / Shift
# ─────────────────────────────────────────────
class BranchMasterView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ensure_branch_shift_tables_exist()
        qs = Branch.objects.filter(tenant=request.user.tenant, is_active=True).order_by('name')
        return Response({"branches": BranchSerializer(qs, many=True).data})

    def post(self, request):
        ensure_branch_shift_tables_exist()
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)
        data = request.data.copy()
        data['tenant'] = request.user.tenant.id
        ser = BranchSerializer(data=data)
        if not ser.is_valid():
            return Response(ser.errors, status=400)
        obj = Branch.objects.create(
            tenant=request.user.tenant,
            code=ser.validated_data.get('code', ''),
            name=ser.validated_data.get('name'),
            address=ser.validated_data.get('address'),
            city=ser.validated_data.get('city'),
            state=ser.validated_data.get('state'),
            country=ser.validated_data.get('country', 'India'),
            is_active=True,
        )
        return Response({"message": "Branch created", "branch": BranchSerializer(obj).data}, status=201)

    def put(self, request):
        ensure_branch_shift_tables_exist()
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)
        bid = request.data.get('id')
        obj = Branch.objects.filter(tenant=request.user.tenant, id=safe_int(bid)).first()
        if not obj:
            return Response({"error": "Branch not found"}, status=404)
        for f in ['code', 'name', 'address', 'city', 'state', 'country']:
            if f in request.data:
                setattr(obj, f, request.data.get(f))
        if 'is_active' in request.data:
            obj.is_active = bool(request.data.get('is_active'))
        obj.save()
        return Response({"message": "Branch updated", "branch": BranchSerializer(obj).data})

    def delete(self, request):
        ensure_branch_shift_tables_exist()
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)
        bid = request.query_params.get('id') or request.data.get('id')
        obj = Branch.objects.filter(tenant=request.user.tenant, id=safe_int(bid)).first()
        if not obj:
            return Response({"error": "Branch not found"}, status=404)
        obj.is_active = False
        obj.save(update_fields=['is_active'])
        return Response({"message": "Branch deactivated"})


class ShiftMasterView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ensure_branch_shift_tables_exist()
        qs = Shift.objects.filter(tenant=request.user.tenant, is_active=True).order_by('name')
        return Response({"shifts": ShiftSerializer(qs, many=True).data})

    def post(self, request):
        ensure_branch_shift_tables_exist()
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)
        ser = ShiftSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=400)
        obj = Shift.objects.create(
            tenant=request.user.tenant,
            code=ser.validated_data.get('code', ''),
            name=ser.validated_data.get('name'),
            start_time=ser.validated_data.get('start_time'),
            end_time=ser.validated_data.get('end_time'),
            grace_minutes=ser.validated_data.get('grace_minutes', 0),
            is_night_shift=ser.validated_data.get('is_night_shift', False),
            is_active=True,
        )
        return Response({"message": "Shift created", "shift": ShiftSerializer(obj).data}, status=201)

    def put(self, request):
        ensure_branch_shift_tables_exist()
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)
        sid = request.data.get('id')
        obj = Shift.objects.filter(tenant=request.user.tenant, id=safe_int(sid)).first()
        if not obj:
            return Response({"error": "Shift not found"}, status=404)
        for f in ['code', 'name', 'start_time', 'end_time', 'grace_minutes', 'is_night_shift']:
            if f in request.data:
                setattr(obj, f, request.data.get(f))
        if 'is_active' in request.data:
            obj.is_active = bool(request.data.get('is_active'))
        obj.save()
        return Response({"message": "Shift updated", "shift": ShiftSerializer(obj).data})

    def delete(self, request):
        ensure_branch_shift_tables_exist()
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)
        sid = request.query_params.get('id') or request.data.get('id')
        obj = Shift.objects.filter(tenant=request.user.tenant, id=safe_int(sid)).first()
        if not obj:
            return Response({"error": "Shift not found"}, status=404)
        obj.is_active = False
        obj.save(update_fields=['is_active'])
        return Response({"message": "Shift deactivated"})


class EmployeeShiftAssignmentsView(views.APIView):
    """HR/Admin: list/create employee shift assignment history."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, employee_id):
        ensure_employee_shift_assignment_tables_exist()
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR', 'MANAGER', 'EMPLOYEE']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        emp = Employee.objects.filter(tenant=tenant, id=employee_id).first()
        if not emp:
            return Response({"error": "Employee not found"}, status=404)
        if not _is_employee_in_scope(request, emp):
            return _forbidden_employee_access()
        with connection.cursor() as cursor:
            vendor = getattr(connection, "vendor", "")
            if vendor == "sqlite":
                cursor.execute(
                    """
                    SELECT esa.id, esa.shift_id, s.name, esa.effective_from, esa.effective_to, esa.is_active, esa.created_at
                    FROM t_employee_shift_assignment esa
                    LEFT JOIN t_shift s ON s.id = esa.shift_id
                    WHERE esa.tenant_id = ? AND esa.employee_id = ?
                    ORDER BY esa.effective_from DESC, esa.created_at DESC
                    LIMIT 200
                    """,
                    [str(tenant.id), int(employee_id)],
                )
            else:
                cursor.execute(
                    """
                    SELECT esa.id, esa.shift_id, s.name, esa.effective_from, esa.effective_to, esa.is_active, esa.created_at
                    FROM t_employee_shift_assignment esa
                    LEFT JOIN t_shift s ON s.id = esa.shift_id
                    WHERE esa.tenant_id = %s AND esa.employee_id = %s
                    ORDER BY esa.effective_from DESC, esa.created_at DESC
                    LIMIT 200
                    """,
                    [tenant.id, employee_id],
                )
            rows = cursor.fetchall()
        data = []
        for r in rows:
            data.append({
                "id": r[0],
                "shift_id": r[1],
                "shift_name": r[2] or "",
                "effective_from": str(r[3]) if r[3] else None,
                "effective_to": str(r[4]) if r[4] else None,
                "is_active": bool(r[5]),
                "created_at": str(r[6]) if r[6] else None,
            })
        return Response({"assignments": data})

    def post(self, request, employee_id):
        ensure_employee_shift_assignment_tables_exist()
        ensure_hr_lifecycle_tables_exist()
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR', 'MANAGER']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        try:
            e = Employee.objects.get(tenant=tenant, id=employee_id)
        except Employee.DoesNotExist:
            return Response({"error": "Employee not found"}, status=404)
        actor_role = str(getattr(request.user, "system_role", "") or "").upper()
        if not _is_employee_in_scope(request, e):
            return _forbidden_employee_access("You are not authorized to modify this employee.")
        if actor_role == 'MANAGER':
            return _forbidden_employee_access("Manager role cannot assign shifts from this endpoint.")

        shift_id = request.data.get('shift_id')
        effective_from = request.data.get('effective_from') or request.data.get('shift_effective_from') or e.joining_date or timezone.localdate()
        if not shift_id:
            return Response({"error": "shift_id is required"}, status=400)

        try:
            from datetime import date
            eff_date = effective_from if isinstance(effective_from, date) else date.fromisoformat(str(effective_from))
        except Exception:
            eff_date = timezone.localdate()

        sft = Shift.objects.filter(tenant=tenant, id=safe_int(shift_id)).first()
        if not sft:
            return Response({"error": "Shift not found"}, status=404)

        _upsert_employee_shift_assignment(tenant.id, e.id, sft.id, eff_date, request.user.id)
        try:
            _insert_lifecycle_event(
                tenant.id,
                e.id,
                "SHIFT_CHANGE",
                eff_date,
                {"shift_id": sft.id, "shift_name": sft.name},
                request.user.id,
            )
        except Exception:
            pass
        return Response({"message": "Shift assigned", "shift_id": sft.id, "effective_from": str(eff_date)})


# ─────────────────────────────────────────────
# Attendance — Tenant-wide shift assignments
# ─────────────────────────────────────────────
class AttendanceShiftAssignmentsView(views.APIView):
    """
    Admin/HR: view and assign shifts tenant-wide.

    - GET: list latest active shift assignment per employee (optionally filter by employee_id)
    - POST: assign shift to an employee (effective-dated); uses the same underlying
            table as the per-employee endpoint: t_employee_shift_assignment.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ensure_employee_shift_assignment_tables_exist()
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR', 'MANAGER']:
            return Response({"error": "Permission denied"}, status=403)

        tenant = request.user.tenant
        employee_id = request.query_params.get('employee_id')

        with connection.cursor() as cursor:
            vendor = getattr(connection, "vendor", "")
            params = [str(tenant.id)]
            emp_clause = ""
            if employee_id:
                emp_clause = " AND e.id = %s " if vendor != "sqlite" else " AND e.id = ? "
                params.append(int(employee_id))

            if vendor == "sqlite":
                cursor.execute(
                    f"""
                    SELECT e.id as employee_id, e.name as employee_name,
                           esa.shift_id, s.name as shift_name,
                           esa.effective_from, esa.effective_to, esa.is_active
                    FROM t_employee e
                    LEFT JOIN t_employee_shift_assignment esa
                      ON esa.tenant_id = e.tenant_id
                     AND esa.employee_id = e.id
                     AND esa.is_active = 1
                    LEFT JOIN t_shift s ON s.id = esa.shift_id
                    WHERE e.tenant_id = ?
                    {emp_clause}
                    ORDER BY e.name ASC
                    LIMIT 2000
                    """,
                    params,
                )
            else:
                cursor.execute(
                    f"""
                    SELECT e.id as employee_id, e.name as employee_name,
                           esa.shift_id, s.name as shift_name,
                           esa.effective_from, esa.effective_to, esa.is_active
                    FROM t_employee e
                    LEFT JOIN t_employee_shift_assignment esa
                      ON esa.tenant_id = e.tenant_id
                     AND esa.employee_id = e.id
                     AND esa.is_active = 1
                    LEFT JOIN t_shift s ON s.id = esa.shift_id
                    WHERE e.tenant_id = %s
                    {emp_clause}
                    ORDER BY e.name ASC
                    LIMIT 2000
                    """,
                    params,
                )
            rows = cursor.fetchall()

        data = []
        for r in rows:
            data.append({
                "employee_id": r[0],
                "employee_name": r[1] or "",
                "shift_id": r[2],
                "shift_name": r[3] or "",
                "effective_from": str(r[4]) if r[4] else None,
                "effective_to": str(r[5]) if r[5] else None,
                "is_active": bool(r[6]) if r[6] is not None else False,
            })
        return Response({"assignments": data})

    def post(self, request):
        ensure_employee_shift_assignment_tables_exist()
        ensure_hr_lifecycle_tables_exist()
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)

        tenant = request.user.tenant
        employee_id = request.data.get('employee_id')
        shift_id = request.data.get('shift_id')
        effective_from = request.data.get('effective_from')

        if not employee_id:
            return Response({"error": "employee_id is required"}, status=400)
        if not shift_id:
            return Response({"error": "shift_id is required"}, status=400)

        try:
            e = Employee.objects.get(tenant=tenant, id=safe_int(employee_id))
        except Employee.DoesNotExist:
            return Response({"error": "Employee not found"}, status=404)

        try:
            from datetime import date
            eff_date = effective_from if isinstance(effective_from, date) else date.fromisoformat(str(effective_from))
        except Exception:
            eff_date = timezone.localdate()

        sft = Shift.objects.filter(tenant=tenant, id=safe_int(shift_id)).first()
        if not sft:
            return Response({"error": "Shift not found"}, status=404)

        _upsert_employee_shift_assignment(tenant.id, e.id, sft.id, eff_date, request.user.id)
        try:
            _insert_lifecycle_event(
                tenant.id,
                e.id,
                "SHIFT_CHANGE",
                eff_date,
                {"shift_id": sft.id, "shift_name": sft.name},
                request.user.id,
            )
        except Exception:
            pass

        return Response({"message": "Shift assigned", "employee_id": e.id, "shift_id": sft.id, "effective_from": str(eff_date)})


# ─────────────────────────────────────────────
# LEAVE TYPES — GET/POST (settings page)
# ─────────────────────────────────────────────
class LeaveTypeMasterView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ensure_master_tables_exist()
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
        ensure_master_tables_exist()
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

    def put(self, request):
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        p = request.data
        try:
            lt = LeaveType.objects.get(tenant=tenant, id=p.get('id'))
            lt.name = p.get('name', lt.name)
            lt.code = p.get('code', lt.code).upper()
            lt.days_per_year = p.get('days_per_year', lt.days_per_year)
            lt.is_paid = p.get('is_paid', lt.is_paid)
            lt.carry_forward = p.get('carry_forward', lt.carry_forward)
            lt.max_carry_forward = p.get('max_carry_forward', lt.max_carry_forward)
            lt.save()
            return Response({"message": "Leave type updated"})
        except LeaveType.DoesNotExist:
            return Response({"error": "Leave type not found"}, status=404)

    def delete(self, request):
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)
        tenant = request.user.tenant
        lt_id = request.query_params.get('id')
        try:
            lt = LeaveType.objects.get(tenant=tenant, id=lt_id)
            lt.delete()
            return Response({"message": "Leave type deleted"})
        except LeaveType.DoesNotExist:
            return Response({"error": "Leave type not found"}, status=404)


# ─────────────────────────────────────────────
# LEAVE BALANCE — per employee
# ─────────────────────────────────────────────
class LeaveBalanceView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ensure_master_tables_exist()
        tenant = request.user.tenant
        sr = getattr(request.user, 'system_role', None)
        employee_id = request.query_params.get('employee_id')
        year = request.query_params.get('year', str(timezone.localdate().year))

        qs = LeaveBalance.objects.filter(tenant=tenant, year=year).select_related('leave_type', 'employee')
        if sr in ['SUPER_ADMIN', 'ADMIN']:
            if employee_id:
                qs = qs.filter(employee_id=employee_id)
        else:
            scope_q = _employee_scope_filter(request)
            if scope_q is None:
                return Response({"error": "Employee mapping not found for your account."}, status=403)
            scoped_employee_ids = list(Employee.objects.filter(tenant=tenant).filter(scope_q).values_list('id', flat=True))
            if employee_id:
                employee_id_num = safe_int(employee_id)
                if employee_id_num is None or employee_id_num not in [int(x) for x in scoped_employee_ids]:
                    return Response({"error": "You are not authorized to access this employee record."}, status=403)
                qs = qs.filter(employee_id=employee_id)
            else:
                qs = qs.filter(employee_id__in=scoped_employee_ids)

        # Deduplicate: one row per leave_type per employee
        seen = set()
        data = []
        for lb in qs:
            key = (lb.employee_id, lb.leave_type_id)
            if key in seen:
                continue
            seen.add(key)
            data.append({
                "id": lb.id,
                "employee_id": str(lb.employee_id),
                "employee_name": lb.employee.name,
                "leave_type_id": lb.leave_type_id,
                "leave_type": lb.leave_type.name,
                "leave_code": lb.leave_type.code,
                "allocated": float(lb.allocated),
                "used": float(lb.used),
                "carried_forward": float(lb.carried_forward),
                "remaining": float(lb.remaining),
            })
        return Response({"balances": data, "year": year})


# ─────────────────────────────────────────────
# LEAVE BALANCE RECONCILIATION — Admin tool
# ─────────────────────────────────────────────
class ReconcileBalancesView(views.APIView):
    """Admin tool to initialize or roll-over leave balances for all employees."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)
        
        tenant = request.user.tenant
        year = request.data.get('year', str(timezone.localdate().year))
        
        try:
            employees = Employee.objects.filter(tenant=tenant, status='Active')
            leave_types = LeaveType.objects.filter(tenant=tenant)
            
            created_count = 0
            updated_count = 0
            
            with transaction.atomic():
                for emp in employees:
                    for lt in leave_types:
                        balance, created = LeaveBalance.objects.get_or_create(
                            tenant=tenant,
                            employee=emp,
                            leave_type=lt,
                            year=year,
                            defaults={'allocated': lt.days_per_year, 'used': 0, 'carried_forward': 0}
                        )
                        if created:
                            created_count += 1
                        else:
                            # Update allocation if it changed in master
                            if balance.allocated != lt.days_per_year:
                                balance.allocated = lt.days_per_year
                                balance.save()
                                updated_count += 1
                                
            return Response({
                "message": f"Reconciliation successful for {year}.",
                "details": f"Created {created_count} new records, updated {updated_count} existing records."
            })
        except Exception as e:
            return Response({"error": str(e)}, status=500)


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


class AdminFreshResetView(views.APIView):
    """
    Admin-only destructive reset.

    Deletes all tenant-scoped data, tenant/user accounts, and onboarding records
    while leaving global master tables intact (m_industry / m_department / m_role
    and other system lookup tables that are not tenant-owned).
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN']:
            return Response({"error": "Permission denied"}, status=403)

        confirm = str(request.data.get('confirm') or '').strip().upper()
        if confirm != 'RESET_ALL_TENANT_DATA':
            return Response(
                {"error": "Confirmation required. Send confirm=RESET_ALL_TENANT_DATA to proceed."},
                status=400,
            )

        actor = request.user.username
        tenant_name = getattr(getattr(request.user, 'tenant', None), 'name', None)

        try:
            with transaction.atomic():
                # Remove all tenant/user data. Everything tenant-owned cascades from Tenant.
                User.objects.all().delete()
                Tenant.objects.all().delete()

            return Response({
                "status": "success",
                "message": "All tenant data has been cleared. Master data has been preserved.",
                "actor": actor,
                "tenant": tenant_name,
                "next_step": "Register a new tenant/company to start fresh.",
            })
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
            
            # 5. Mark onboarding as complete for dashboard access (aligns with UI wizard finalize).
            tenant.onboarding_step = 10
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

        # Also reconcile category for *all* existing tenant roles (including custom roles),
        # so the UI can reliably read `system_role_category`.
        reconciled = 0
        for r in Role.objects.filter(tenant=tenant).only('id', 'name', 'system_role_category'):
            if not str(getattr(r, 'system_role_category', '') or '').strip():
                r.system_role_category = self._derive_category(r.name)
                r.save(update_fields=['system_role_category'])
                reconciled += 1

        return Response({
            "message": "Seeded roles and default admin permissions",
            "admin_route_keys": ADMIN_ROUTE_KEYS,
            "reconciled_role_categories": reconciled,
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


class AdminReconcileRoleCategoriesView(views.APIView):
    """
    Reconcile `t_role.system_role_category` for the current tenant.

    This is safe and idempotent. It is useful for older tenants whose roles were created
    before `system_role_category` was populated consistently.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        sr = str(getattr(request.user, 'system_role', '') or '').upper()
        if sr not in ('SUPER_ADMIN', 'ADMIN'):
            return Response({"error": "Permission denied"}, status=403)

        tenant = request.user.tenant
        updated = 0
        for r in Role.objects.filter(tenant=tenant).only('id', 'name', 'system_role_category'):
            derived = AdminSeedRolePermissionsView._derive_category(r.name)
            current = str(getattr(r, 'system_role_category', '') or '').strip().upper()
            if current != derived:
                r.system_role_category = derived
                r.save(update_fields=['system_role_category'])
                updated += 1

        return Response({
            "message": "Role categories reconciled",
            "updated": updated,
        })

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
class NotificationView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ensure_master_tables_exist()
        limit = int(request.query_params.get('limit', 10))
        notifications = Notification.objects.filter(
            tenant=request.user.tenant,
            user=request.user
        ).order_by('-created_at')[:limit]
        
        data = [
            {
                "id": n.id, "title": n.title, "message": n.message,
                "type": n.notify_type, "is_read": n.is_read,
                "url": n.action_url, "time": n.created_at.isoformat()
            }
            for n in notifications
        ]
        
        unread_count = Notification.objects.filter(
            tenant=request.user.tenant, user=request.user, is_read=False
        ).count()
        
        return Response({"notifications": data, "unread_count": unread_count})

    def post(self, request):
        ensure_master_tables_exist()
        notify_id = request.data.get('id')
        if notify_id == 'all':
            Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
        else:
            Notification.objects.filter(id=notify_id, user=request.user).update(is_read=True)
        return Response({"message": "Marked as read"})


import os

# ─────────────────────────────────────────────
# MASTER LOOKUP (Dropdown Data Store)
# ─────────────────────────────────────────────

def _load_default_lookup_seed() -> dict:
    """
    Loads default lookup seed values from a JSON file to avoid hardcoding values in Python.
    The table `t_master_lookup` will generate IDs automatically on insert.
    """
    try:
        here = os.path.dirname(__file__)
        path = os.path.join(here, "seed_lookups.default.json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def seed_lookups_for_tenant(tenant_id):
    """Insert default lookup values for a tenant if they don't already exist."""
    seed = _load_default_lookup_seed()
    with connection.cursor() as cursor:
        for category, items in seed.items():
            if not isinstance(items, list):
                continue
            for sort_order, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                code = str(item.get("code", "")).strip()
                label = str(item.get("label", "")).strip()
                if not code or not label:
                    continue
                cursor.execute(
                    """
                    INSERT IGNORE INTO t_master_lookup
                        (tenant_id, category, code, label, is_active, sort_order)
                    VALUES (%s, %s, %s, %s, 1, %s)
                    """,
                    [tenant_id, category, code, label, sort_order]
                )


class MasterLookupView(views.APIView):
    """
    GET  /api/lookups/           — returns all active lookups grouped by category
    GET  /api/lookups/?category=BANK — returns only that category
    POST /api/lookups/           — upsert a lookup item (admin only)
    POST /api/lookups/seed/      — seed default values for this tenant
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ensure_master_tables_exist()
        tenant_id = str(request.user.tenant_id)

        # Auto-seed on first access if empty
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM t_master_lookup WHERE tenant_id = %s", [tenant_id]
            )
            count = cursor.fetchone()[0]
        if count == 0:
            seed_lookups_for_tenant(tenant_id)

        category = request.query_params.get('category')
        with connection.cursor() as cursor:
            if category:
                cursor.execute(
                    """SELECT id, category, code, label, sort_order
                       FROM t_master_lookup
                       WHERE tenant_id = %s AND category = %s AND is_active = 1
                       ORDER BY sort_order, label""",
                    [tenant_id, category.upper()]
                )
            else:
                cursor.execute(
                    """SELECT id, category, code, label, sort_order
                       FROM t_master_lookup
                       WHERE tenant_id = %s AND is_active = 1
                       ORDER BY category, sort_order, label""",
                    [tenant_id]
                )
            rows = cursor.fetchall()

        # Group by category
        grouped: dict = {}
        for row_id, cat, code, label, sort_order in rows:
            grouped.setdefault(cat, []).append({
                'id': row_id, 'code': code, 'label': label, 'sort_order': sort_order
            })

        if category:
            return Response(grouped.get(category.upper(), []))
        return Response(grouped)

    def post(self, request):
        ensure_master_tables_exist()
        if request.user.system_role not in ['ADMIN', 'SUPER_ADMIN', 'HR']:
            return Response({"error": "Permission denied"}, status=403)

        action = request.data.get('action')
        tenant_id = str(request.user.tenant_id)

        if action == 'seed':
            seed_lookups_for_tenant(tenant_id)
            return Response({"message": "Default lookups seeded successfully"})

        category = request.data.get('category', '').upper()
        code = request.data.get('code', '').upper()
        label = request.data.get('label', '').strip()
        sort_order = request.data.get('sort_order', 0)
        is_active = request.data.get('is_active', True)

        if not category or not code or not label:
            return Response({"error": "category, code and label are required"}, status=400)

        with connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO t_master_lookup (tenant_id, category, code, label, is_active, sort_order)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE label = VALUES(label), is_active = VALUES(is_active),
                   sort_order = VALUES(sort_order)""",
                [tenant_id, category, code, label, is_active, sort_order]
            )
            cursor.execute(
                "SELECT id FROM t_master_lookup WHERE tenant_id=%s AND category=%s AND code=%s",
                [tenant_id, category, code]
            )
            row = cursor.fetchone()

        return Response({"id": row[0], "category": category, "code": code, "label": label})
