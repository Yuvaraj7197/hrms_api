from rest_framework import status, views, permissions
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import authenticate
from .models import User, Tenant, OTP, Department, Role, Employee
from .serializers import RegisterSerializer, OTPVerifySerializer, OnboardingSerializer, UserSerializer, DepartmentSerializer, RoleSerializer, EmployeeSerializer
import random

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
                level=role.get('level', 1)
            )
        
        return Response({"message": "Roles saved successfully"})

class OnboardingEmployeesView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        tenant = request.user.tenant
        employees_data = request.data.get('employees', [])
        
        # We handle employee creation and hierarchy
        # 1. Create employees first
        for emp in employees_data:
            dept_name = emp.get('department')
            role_name = emp.get('role')
            
            dept = Department.objects.filter(tenant=tenant, name=dept_name).first()
            role = Role.objects.filter(tenant=tenant, name=role_name).first()
            
            Employee.objects.update_or_create(
                tenant=tenant,
                email=emp.get('email'),
                defaults={
                    'name': emp.get('name'),
                    'employee_code': emp.get('employeeCode'),
                    'department': dept,
                    'designation': role,
                }
            )
        
        # 2. Setup Reporting Hierarchy
        for emp in employees_data:
            manager_email = emp.get('reportingTo')
            if manager_email:
                manager = Employee.objects.filter(tenant=tenant, email=manager_email).first()
                if manager:
                    Employee.objects.filter(tenant=tenant, email=emp.get('email')).update(reporting_to=manager)
        
        return Response({"message": "Employees and Hierarchy saved successfully"})

class OnboardingSetupView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        tenant = request.user.tenant
        serializer = OnboardingSerializer(tenant, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            step = request.data.get('onboarding_step', 2)
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
        tenant.save()
        
        return Response({"message": "Departments saved successfully"})

class LoginView(views.APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        username = request.data.get('username')
        password = request.data.get('password')
        user = authenticate(username=username, password=password)

        if user:
            if not user.is_verified:
                return Response({"error": "Please verify your email first", "is_verified": False, "email": user.email}, status=status.HTTP_403_FORBIDDEN)
            
            refresh = RefreshToken.for_user(user)
            return Response({
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": UserSerializer(user).data
            })
        
        return Response({"error": "Invalid credentials"}, status=status.HTTP_401_UNAUTHORIZED)
