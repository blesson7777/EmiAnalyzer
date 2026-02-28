from django.contrib.auth.models import User
from django.db import models


class Income(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='incomes')
    monthly_salary = models.IntegerField(default=0)
    other_income = models.IntegerField(default=0)

    def __str__(self):
        return f"{self.user.username} Income"

    @property
    def total_income(self):
        return self.monthly_salary + self.other_income


class Loan(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='loans')
    loan_type = models.CharField(max_length=120)
    lender = models.CharField(max_length=120, blank=True, default='')
    principal = models.IntegerField(default=0)
    monthly_emi = models.IntegerField(default=0)
    interest_rate = models.FloatField(default=0.0)
    start_date = models.DateField()
    end_date = models.DateField()

    def __str__(self):
        return f"{self.loan_type} - {self.user.username}"


class Budget(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='budgets')
    grocery = models.IntegerField(default=0)
    rent = models.IntegerField(default=0)
    transport = models.IntegerField(default=0)
    entertainment = models.IntegerField(default=0)

    def __str__(self):
        return f"Budget - {self.user.username}"

    @property
    def total_expense(self):
        return self.grocery + self.rent + self.transport + self.entertainment


class CreditCardSpend(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='credit_card_spends')
    card_name = models.CharField(max_length=120)
    statement_month = models.DateField()
    total_spend = models.IntegerField(default=0)
    amount_paid = models.IntegerField(default=0)
    minimum_due = models.IntegerField(default=0)
    annual_interest_rate = models.FloatField(default=0.0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-statement_month', '-id']

    def __str__(self):
        return f"{self.card_name} - {self.user.username}"

    @property
    def outstanding_amount(self):
        return max(0, self.total_spend - self.amount_paid)

    @property
    def monthly_interest_estimate(self):
        return round(self.outstanding_amount * (self.annual_interest_rate / 1200.0), 2)


class CreditCardAccount(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='credit_cards')
    card_name = models.CharField(max_length=120)
    issuer = models.CharField(max_length=120, blank=True, default='')
    credit_limit = models.IntegerField(default=0)
    emi_interest_rate = models.FloatField(default=18.0)
    monthly_spend_interest_rate = models.FloatField(default=0.0)
    reward_percent = models.FloatField(default=0.0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['card_name', 'id']

    def __str__(self):
        issuer_part = f" ({self.issuer})" if self.issuer else ''
        return f"{self.card_name}{issuer_part} - {self.user.username}"


class CreditCardEntry(models.Model):
    TYPE_EMI = 'emi'
    TYPE_MONTHLY_SPEND = 'monthly'
    ENTRY_TYPE_CHOICES = (
        (TYPE_EMI, 'EMI'),
        (TYPE_MONTHLY_SPEND, 'Monthly Spend'),
    )

    card = models.ForeignKey(CreditCardAccount, on_delete=models.CASCADE, related_name='entries')
    entry_month = models.DateField()
    entry_type = models.CharField(max_length=20, choices=ENTRY_TYPE_CHOICES)
    amount = models.IntegerField(default=0)
    tenure_months = models.IntegerField(default=1)
    description = models.CharField(max_length=200, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-entry_month', '-id']

    def __str__(self):
        return f"{self.card.card_name} {self.get_entry_type_display()} - {self.amount}"

    @property
    def annual_rate(self):
        if self.entry_type == self.TYPE_EMI:
            return self.card.emi_interest_rate
        return self.card.monthly_spend_interest_rate

    @property
    def monthly_interest_estimate(self):
        return round(self.amount * (self.annual_rate / 1200.0), 2)

    @property
    def reward_estimate(self):
        if self.entry_type != self.TYPE_MONTHLY_SPEND:
            return 0.0
        return round(self.amount * (self.card.reward_percent / 100.0), 2)


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    phone_number = models.CharField(max_length=20, unique=True, null=True, blank=True)
    profile_photo = models.FileField(upload_to='profile_photos/', null=True, blank=True)

    def __str__(self):
        return f"Profile - {self.user.username}"


class SystemSetting(models.Model):
    emi_green_limit = models.FloatField(default=30.0)
    emi_yellow_limit = models.FloatField(default=50.0)
    high_interest_rate_limit = models.FloatField(default=12.0)
    savings_target_percent = models.FloatField(default=20.0)
    advisory_message = models.TextField(blank=True, default='')
    admin_theme = models.CharField(max_length=10, default='light')
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return 'System Settings'

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(
            id=1,
            defaults={
                'emi_green_limit': 30.0,
                'emi_yellow_limit': 50.0,
                'high_interest_rate_limit': 12.0,
                'savings_target_percent': 20.0,
                'advisory_message': '',
                'admin_theme': 'light',
            },
        )
        return obj


class AuditLog(models.Model):
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='actor_logs')
    target_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='target_logs',
    )
    action = models.CharField(max_length=120)
    details = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        actor_name = self.actor.username if self.actor else 'Unknown'
        return f'{actor_name} - {self.action}'
