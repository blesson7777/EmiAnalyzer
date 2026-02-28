from django.urls import path
from django.core.management import call_command
from django.http import HttpResponse
from . import views

# Temporary migration function
def run_migrate(request):
    call_command('migrate')
    return HttpResponse("MIGRATIONS DONE")

urlpatterns = [
    # Temporary migration URL
    path("run-migrate/", run_migrate),

    path('', views.dashboard, name='dashboard'),
    path('register/', views.register_view, name='register'),
    path('login/', views.login_view, name='login'),
    path('admin/login/', views.admin_login_view, name='admin_login'),
    path('logout/', views.logout_view, name='logout'),
    path('toggle-theme/', views.toggle_theme_view, name='toggle_theme'),
    path('lock-screen/', views.lock_screen_view, name='lock_screen'),
    path('unlock-screen/', views.unlock_screen_view, name='unlock_screen'),
    path('forgot-password/', views.forgot_password_view, name='forgot_password'),
    path('admin/forgot-password/', views.admin_forgot_password_view, name='admin_forgot_password'),
    path('reset-password/', views.reset_password_view, name='reset_password'),
    path('admin/reset-password/', views.admin_reset_password_view, name='admin_reset_password'),
    path('profile/', views.profile_view, name='profile'),
    path('settings/', views.settings_view, name='settings'),
    path('income/add/', views.add_income, name='add_income'),
    path('income/edit/', views.edit_income, name='edit_income'),
    path('loans/add/', views.add_loan, name='add_loan'),
    path('loans/<int:loan_id>/edit/', views.edit_loan, name='edit_loan'),
    path('loans/', views.loan_list, name='loan_list'),
    path('loans/delete/<int:loan_id>/', views.delete_loan, name='delete_loan'),
    path('credit-cards/', views.credit_cards_view, name='credit_cards'),
    path('credit-cards/add/', views.credit_card_add_view, name='credit_card_add'),
    path('credit-cards/<int:card_id>/edit/', views.credit_card_edit_view, name='credit_card_edit'),
    path('credit-cards/<int:card_id>/spend/', views.credit_card_spend_view, name='credit_card_spend'),
    path('budget/', views.budget_view, name='budget'),
    path('monthly-payments/', views.monthly_payments_view, name='monthly_payments'),
    path('charts/', views.charts_view, name='charts'),
    path('admin/users/', views.admin_user_management, name='admin_users'),
    path('admin/users/<int:user_id>/', views.admin_user_detail, name='admin_user_details'),
    path('admin/users/<int:user_id>/toggle-active/', views.admin_toggle_user_active, name='admin_toggle_user_active'),
    path('admin/users/<int:user_id>/force-reset/', views.admin_force_password_reset, name='admin_force_password_reset'),
    path('admin/users/<int:user_id>/delete/', views.admin_delete_user, name='admin_delete_user'),
    path('admin/loan-overview/', views.admin_loan_overview, name='admin_loan_overview'),
    path('admin/income-overview/', views.admin_income_overview, name='admin_income_overview'),
    path('admin/budget-overview/', views.admin_budget_overview, name='admin_budget_overview'),
    path('admin/system-risk/', views.admin_risk_monitor, name='admin_system_risk'),
    path('admin/charts/', views.admin_charts, name='admin_charts'),
    path('admin/reports/', views.admin_reports, name='admin_reports'),
    path('admin/exports/<str:export_type>/', views.admin_export_report, name='admin_export_report'),
    path('admin/system-controls/', views.admin_system_controls, name='admin_system_controls'),
    path('admin/audit-logs/', views.admin_audit_logs, name='admin_audit_logs'),
]