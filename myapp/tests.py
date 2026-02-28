from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import (
    Budget,
    CreditCardAccount,
    CreditCardEntry,
    CreditCardSpend,
    Income,
    Loan,
    SystemSetting,
    UserProfile,
)
from .views import _build_chart_payload, _financial_snapshot


class AuthFlowTests(TestCase):
    def test_register_and_login_with_username_email_phone(self):
        response = self.client.post(
            reverse('register'),
            {
                'username': 'ravi_user',
                'email': 'ravi@example.com',
                'phone_number': '98765-43210',
                'password': 'StrongPass123',
                'confirm_password': 'StrongPass123',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('login'))

        user = User.objects.get(username='ravi_user')
        profile = UserProfile.objects.get(user=user)
        self.assertEqual(profile.phone_number, '9876543210')

        for identifier in ['ravi_user', 'ravi@example.com', '9876543210']:
            login_response = self.client.post(
                reverse('login'),
                {'identifier': identifier, 'password': 'StrongPass123'},
            )
            self.assertEqual(login_response.status_code, 302)
            self.assertRedirects(login_response, reverse('dashboard'))
            self.client.get(reverse('logout'))


class IncomeLoanFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='finance_user',
            email='finance@example.com',
            password='StrongPass123',
        )
        UserProfile.objects.create(user=self.user, phone_number='9123456780')
        self.client.login(username='finance_user', password='StrongPass123')

    def test_income_add_edit_and_loan_crud(self):
        add_income_response = self.client.post(
            reverse('add_income'),
            {'monthly_salary': '50000', 'other_income': '5000'},
        )
        self.assertEqual(add_income_response.status_code, 302)
        income = Income.objects.get(user=self.user)
        self.assertEqual(income.monthly_salary, 50000)
        self.assertEqual(income.other_income, 5000)

        edit_income_response = self.client.post(
            reverse('edit_income'),
            {'monthly_salary': '55000', 'other_income': '4500'},
        )
        self.assertEqual(edit_income_response.status_code, 302)
        income.refresh_from_db()
        self.assertEqual(income.monthly_salary, 55000)
        self.assertEqual(income.other_income, 4500)

        add_loan_response = self.client.post(
            reverse('add_loan'),
            {
                'loan_type': 'Personal Loan',
                'lender': 'Axis Bank',
                'principal': '200000',
                'monthly_emi': '7000',
                'interest_rate': '15.5',
                'start_date': date.today().isoformat(),
                'end_date': (date.today() + timedelta(days=365)).isoformat(),
            },
        )
        self.assertEqual(add_loan_response.status_code, 302)
        loan = Loan.objects.get(user=self.user)
        self.assertEqual(loan.loan_type, 'Personal Loan')
        self.assertEqual(loan.lender, 'Axis Bank')

        edit_loan_response = self.client.post(
            reverse('edit_loan', args=[loan.id]),
            {
                'loan_type': 'Personal Loan Updated',
                'lender': 'HDFC Bank',
                'principal': '180000',
                'monthly_emi': '6800',
                'interest_rate': '14.2',
                'start_date': date.today().isoformat(),
                'end_date': (date.today() + timedelta(days=300)).isoformat(),
            },
        )
        self.assertEqual(edit_loan_response.status_code, 302)
        loan.refresh_from_db()
        self.assertEqual(loan.loan_type, 'Personal Loan Updated')
        self.assertEqual(loan.lender, 'HDFC Bank')
        self.assertEqual(loan.monthly_emi, 6800)

        delete_loan_response = self.client.post(reverse('delete_loan', args=[loan.id]))
        self.assertEqual(delete_loan_response.status_code, 302)
        self.assertFalse(Loan.objects.filter(id=loan.id).exists())


class DashboardBudgetSuggestionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='suggest_user',
            email='suggest@example.com',
            password='StrongPass123',
        )
        UserProfile.objects.create(user=self.user, phone_number='9000000011')
        Income.objects.create(user=self.user, monthly_salary=40000, other_income=0)
        Loan.objects.create(
            user=self.user,
            loan_type='Credit Card',
            principal=80000,
            monthly_emi=22000,
            interest_rate=24.0,
            start_date=date.today(),
            end_date=date.today() + timedelta(days=400),
        )
        Budget.objects.create(user=self.user, grocery=9000, rent=17000, transport=5000, entertainment=6000)
        card = CreditCardAccount.objects.create(
            user=self.user,
            card_name='Visa Platinum',
            issuer='Axis',
            credit_limit=200000,
            emi_interest_rate=18.0,
            monthly_spend_interest_rate=0.0,
            reward_percent=1.0,
        )
        CreditCardEntry.objects.create(
            card=card,
            entry_month=date.today().replace(day=1),
            entry_type=CreditCardEntry.TYPE_MONTHLY_SPEND,
            amount=51094,
            tenure_months=1,
            description='Current month card spend',
        )
        self.client.login(username='suggest_user', password='StrongPass123')

    def test_dashboard_has_priority_and_strategy_suggestions(self):
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Prioritize')
        self.assertContains(response, 'avalanche')
        self.assertContains(response, 'refinancing', html=False)

    def test_budget_detects_overspending(self):
        response = self.client.get(reverse('budget'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Overspending detected')

    def test_dashboard_displays_card_total_spend_in_loan_emi_card(self):
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Loan EMI (Monthly)')
        self.assertContains(response, 'Card Spend (')
        self.assertContains(response, 'Rs. 51,094')

    def test_snapshot_marks_no_income_with_debt_as_high_burden(self):
        debt_user = User.objects.create_user(
            username='no_income_user',
            email='no_income@example.com',
            password='StrongPass123',
        )
        UserProfile.objects.create(user=debt_user, phone_number='9666666666')
        Loan.objects.create(
            user=debt_user,
            loan_type='Personal Loan',
            lender='ICICI',
            principal=100000,
            monthly_emi=10000,
            interest_rate=14.0,
            start_date=date.today(),
            end_date=date.today() + timedelta(days=365),
        )

        snapshot = _financial_snapshot(debt_user)
        self.assertEqual(snapshot['total_income'], 0)
        self.assertEqual(snapshot['overall_burden_ratio'], 100.0)
        self.assertEqual(snapshot['emi_ratio'], 100.0)
        self.assertEqual(snapshot['overall_health_class'], 'red')
        self.assertEqual(snapshot['health_class'], 'red')


class CreditCardMonthlyAndEmiLogicTests(TestCase):
    def _shift_month(self, month_start, months):
        target_index = (month_start.month - 1) + months
        year = month_start.year + (target_index // 12)
        month = (target_index % 12) + 1
        return date(year, month, 1)

    def setUp(self):
        self.user = User.objects.create_user(
            username='card_user',
            email='card@example.com',
            password='StrongPass123',
        )
        UserProfile.objects.create(user=self.user, phone_number='9333333333')
        self.card = CreditCardAccount.objects.create(
            user=self.user,
            card_name='Master Gold',
            issuer='BankX',
            credit_limit=100000,
            emi_interest_rate=12.0,
            monthly_spend_interest_rate=0.0,
            reward_percent=2.0,
        )
        self.client.login(username='card_user', password='StrongPass123')

    def test_monthly_spend_counts_current_month_only(self):
        current_month = date.today().replace(day=1)
        previous_month = self._shift_month(current_month, -1)
        CreditCardEntry.objects.create(
            card=self.card,
            entry_month=previous_month,
            entry_type=CreditCardEntry.TYPE_MONTHLY_SPEND,
            amount=9000,
            tenure_months=1,
            description='Old month spend',
        )
        CreditCardEntry.objects.create(
            card=self.card,
            entry_month=current_month,
            entry_type=CreditCardEntry.TYPE_MONTHLY_SPEND,
            amount=4000,
            tenure_months=1,
            description='Current month spend',
        )
        CreditCardSpend.objects.create(
            user=self.user,
            card_name='Legacy',
            statement_month=previous_month,
            total_spend=12000,
            amount_paid=0,
            minimum_due=600,
            annual_interest_rate=36.0,
        )
        CreditCardSpend.objects.create(
            user=self.user,
            card_name='Legacy Current',
            statement_month=current_month,
            total_spend=35000,
            amount_paid=0,
            minimum_due=1750,
            annual_interest_rate=36.0,
        )

        snapshot = _financial_snapshot(self.user)
        self.assertEqual(snapshot['credit_card_total_spend'], 4000)
        self.assertEqual(snapshot['credit_card_due_estimate'], 4000)
        self.assertEqual(snapshot['credit_card_total_outstanding'], 4000)
        self.assertEqual(snapshot['credit_card_legacy_current_count'], 0)
        self.assertEqual(snapshot['credit_card_legacy_current_outstanding'], 0)

    def test_emi_entries_use_remaining_balance_and_active_tenure(self):
        current_month = date.today().replace(day=1)
        two_months_ago = self._shift_month(current_month, -2)
        nine_months_ago = self._shift_month(current_month, -9)

        CreditCardEntry.objects.create(
            card=self.card,
            entry_month=two_months_ago,
            entry_type=CreditCardEntry.TYPE_EMI,
            amount=12000,
            tenure_months=6,
            description='Laptop EMI',
        )
        CreditCardEntry.objects.create(
            card=self.card,
            entry_month=nine_months_ago,
            entry_type=CreditCardEntry.TYPE_EMI,
            amount=6000,
            tenure_months=3,
            description='Closed EMI',
        )

        snapshot = _financial_snapshot(self.user)
        self.assertEqual(snapshot['credit_card_active_emi_count'], 1)
        self.assertGreater(snapshot['credit_card_total_emi'], 0)
        self.assertGreater(snapshot['credit_card_total_emi_remaining_balance'], 0)
        self.assertLess(snapshot['credit_card_total_emi_remaining_balance'], 12000)
        self.assertEqual(
            snapshot['credit_card_due_estimate'],
            round(snapshot['credit_card_total_emi'] + snapshot['credit_card_total_spend'], 2),
        )

    def test_credit_card_spend_view_accepts_emi_entry_type(self):
        response = self.client.post(
            reverse('credit_card_spend', args=[self.card.id]),
            {
                'action': 'save_entry',
                'entry_type': CreditCardEntry.TYPE_EMI,
                'entry_month': date.today().strftime('%Y-%m'),
                'amount': '18000',
                'tenure_months': '9',
                'description': 'Phone EMI',
            },
        )
        self.assertEqual(response.status_code, 302)
        entry = CreditCardEntry.objects.get(card=self.card, description='Phone EMI')
        self.assertEqual(entry.entry_type, CreditCardEntry.TYPE_EMI)
        self.assertEqual(entry.tenure_months, 9)


class MonthlyPaymentsViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='payments_user',
            email='payments@example.com',
            password='StrongPass123',
        )
        UserProfile.objects.create(user=self.user, phone_number='9444444444')
        Income.objects.create(user=self.user, monthly_salary=70000, other_income=5000)
        Loan.objects.create(
            user=self.user,
            loan_type='Home Loan',
            lender='SBI',
            principal=2500000,
            monthly_emi=22000,
            interest_rate=8.5,
            start_date=date.today() - timedelta(days=120),
            end_date=date.today() + timedelta(days=3650),
        )
        self.card = CreditCardAccount.objects.create(
            user=self.user,
            card_name='Rewards Plus',
            issuer='HDFC',
            credit_limit=150000,
            emi_interest_rate=14.0,
            monthly_spend_interest_rate=0.0,
            reward_percent=1.5,
        )
        current_month = date.today().replace(day=1)
        CreditCardEntry.objects.create(
            card=self.card,
            entry_month=current_month,
            entry_type=CreditCardEntry.TYPE_MONTHLY_SPEND,
            amount=8000,
            tenure_months=1,
            description='Groceries',
        )
        CreditCardEntry.objects.create(
            card=self.card,
            entry_month=current_month,
            entry_type=CreditCardEntry.TYPE_EMI,
            amount=18000,
            tenure_months=6,
            description='Phone EMI',
        )
        self.client.login(username='payments_user', password='StrongPass123')

    def test_monthly_payments_page_lists_current_dues(self):
        response = self.client.get(reverse('monthly_payments'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Monthly Payments')
        self.assertContains(response, 'Due Items')
        self.assertContains(response, 'Loan EMI')
        self.assertContains(response, 'Card EMI')
        self.assertContains(response, 'Card Spend')
        self.assertContains(response, 'Home Loan')
        self.assertContains(response, 'SBI')
        self.assertContains(response, 'Rewards Plus')
        self.assertContains(response, 'HDFC')

    def test_monthly_payments_page_shows_empty_state(self):
        clean_user = User.objects.create_user(
            username='payments_empty',
            email='payments_empty@example.com',
            password='StrongPass123',
        )
        UserProfile.objects.create(user=clean_user, phone_number='9555555555')
        self.client.login(username='payments_empty', password='StrongPass123')
        response = self.client.get(reverse('monthly_payments'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No payment due for this month.')


class PasswordResetOtpTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='otp_user',
            email='otp@example.com',
            password='StrongPass123',
        )
        UserProfile.objects.create(user=self.user, phone_number='9111111111')

    @patch('myapp.views.send_otp_email')
    def test_forgot_password_and_reset_flow(self, mocked_send_otp):
        response = self.client.post(reverse('forgot_password'), {'email': self.user.email})
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('reset_password'))

        session = self.client.session
        self.assertIn('reset_otp_data', session)
        otp_value = session['reset_otp_data']['otp']
        mocked_send_otp.assert_called_once()
        sent_kwargs = mocked_send_otp.call_args.kwargs
        self.assertEqual(sent_kwargs['account_role'], 'User')
        self.assertIn('/reset-password/', sent_kwargs['reset_url'])

        reset_response = self.client.post(
            reverse('reset_password'),
            {
                'email': self.user.email,
                'otp': otp_value,
                'new_password': 'NewStrongPass123',
                'confirm_password': 'NewStrongPass123',
            },
        )
        self.assertEqual(reset_response.status_code, 302)
        self.assertRedirects(reset_response, reverse('login'))

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('NewStrongPass123'))


class ChartsAndAdminRiskTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='admin',
            email='admin@example.com',
            password='StrongPass123',
        )
        self.user = User.objects.create_user(
            username='risk_user',
            email='risk@example.com',
            password='StrongPass123',
        )
        UserProfile.objects.create(user=self.user, phone_number='9222222222')
        Income.objects.create(user=self.user, monthly_salary=20000, other_income=0)
        Loan.objects.create(
            user=self.user,
            loan_type='Personal',
            principal=120000,
            monthly_emi=14000,
            interest_rate=19.0,
            start_date=date.today(),
            end_date=date.today() + timedelta(days=360),
        )

    def test_user_charts_payload_present(self):
        self.client.login(username='risk_user', password='StrongPass123')
        response = self.client.get(reverse('charts'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'chartPayload')
        self.assertContains(response, 'Debt Mix (Loans + Cards)')

    def test_loan_timeline_payload_is_monthwise_remaining_balance(self):
        timeline_user = User.objects.create_user(
            username='timeline_user',
            email='timeline@example.com',
            password='StrongPass123',
        )
        UserProfile.objects.create(user=timeline_user, phone_number='9777777777')
        Loan.objects.create(
            user=timeline_user,
            loan_type='Vehicle Loan',
            lender='SBI',
            principal=120000,
            monthly_emi=12000,
            interest_rate=0.0,
            start_date=date.today().replace(day=1),
            end_date=date.today() + timedelta(days=365),
        )

        snapshot = _financial_snapshot(timeline_user)
        payload = _build_chart_payload(snapshot)
        labels = payload['loan_timeline']['labels']
        values = payload['loan_timeline']['values']

        self.assertGreaterEqual(len(labels), 2)
        self.assertEqual(labels[0], date.today().strftime('%b %Y'))
        self.assertGreater(values[0], values[-1])
        self.assertTrue(all(value >= 0 for value in values))

    @patch('myapp.views.send_advisory_email', return_value=1)
    def test_admin_risk_uses_html_advisory_sender(self, mocked_send_advisory):
        self.client.login(username='admin', password='StrongPass123')
        response = self.client.post(
            reverse('admin_system_risk'),
            {
                'mode': 'risky',
                'target_group': 'red',
                'subject': 'Debt Alert',
                'message': 'Please prioritize high-interest EMI and reduce expenses immediately.',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('?mode=risky', response.url)
        mocked_send_advisory.assert_called_once()
        call_kwargs = mocked_send_advisory.call_args.kwargs
        self.assertEqual(call_kwargs['subject'], 'Debt Alert')
        self.assertIn('risk@example.com', call_kwargs['recipients'])

    def test_admin_system_controls_post_without_theme_field(self):
        self.client.login(username='admin', password='StrongPass123')
        settings_obj = SystemSetting.get_solo()
        response = self.client.post(
            reverse('admin_system_controls'),
            {
                'emi_green_limit': settings_obj.emi_green_limit,
                'emi_yellow_limit': settings_obj.emi_yellow_limit,
                'high_interest_rate_limit': settings_obj.high_interest_rate_limit,
                'savings_target_percent': settings_obj.savings_target_percent,
                'advisory_message': settings_obj.advisory_message,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('admin_system_controls'))

    def test_admin_emi_pdf_export_uses_structured_layout(self):
        self.client.login(username='admin', password='StrongPass123')
        response = self.client.get(reverse('admin_export_report', args=['emi-pdf']))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn('attachment; filename="emi_report.pdf"', response['Content-Disposition'])
        self.assertIn(b'EMI Analyzer Risk Report', response.content)
        self.assertIn(b'Executive Summary', response.content)
        self.assertIn(b'Top Risk Accounts', response.content)
