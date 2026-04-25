from django.urls import path
from .views import RegisterView, VerifyOTPView, OnboardingSetupView, LoginView

urlpatterns = [
    path('register/', RegisterView.as_view(), name='register'),
    path('verify-otp/', VerifyOTPView.as_view(), name='verify_otp'),
    path('onboarding/setup/', OnboardingSetupView.as_view(), name='onboarding_setup'),
    path('login/', LoginView.as_view(), name='login'),
]
# 