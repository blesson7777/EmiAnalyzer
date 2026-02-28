import csv
import json
import os
import random
import re
import string
from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta
from functools import wraps

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from .email_utils import send_advisory_email, send_otp_email
from .models import (
    AuditLog,
    Budget,
    CreditCardAccount,
    CreditCardEntry,
    Income,
    Loan,
    SystemSetting,
    UserProfile,
)

USERNAME_REGEX = re.compile(r'^[A-Za-z0-9_.@+-]{3,30}$')
ALLOWED_PROFILE_PHOTO_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
MAX_PROFILE_PHOTO_BYTES = 2 * 1024 * 1024


def _template_for(request, relative_path):
    folder = 'admin' if request.user.is_authenticated and request.user.is_superuser else 'user'
    return f'{folder}/{relative_path}'


def _template_for_user(user, relative_path):
    folder = 'admin' if user and user.is_superuser else 'user'
    return f'{folder}/{relative_path}'


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_phone_number(value):
    digits = ''.join(ch for ch in (value or '') if ch.isdigit())
    return digits


def _is_valid_email(value):
    try:
        validate_email((value or '').strip())
        return True
    except ValidationError:
        return False


def _validate_username(value):
    username = (value or '').strip()
    if not username:
        return '', 'Username is required.'
    if not USERNAME_REGEX.match(username):
        return '', 'Username must be 3-30 chars and can use letters, numbers, ., _, @, +, -.'
    return username, ''


def _validate_password(value, field_name='Password'):
    password = value or ''
    if not password:
        return f'{field_name} is required.'
    if len(password) < 8:
        return f'{field_name} must be at least 8 characters long.'
    if password.strip() != password:
        return f'{field_name} must not start or end with spaces.'
    return ''


def _validate_otp(value):
    otp = (value or '').strip()
    if not otp:
        return otp, 'OTP is required.'
    if not otp.isdigit() or len(otp) != 6:
        return otp, 'OTP must be a 6-digit number.'
    return otp, ''


def _validate_integer_field(raw_value, label, min_value=0, max_value=10_000_000_000):
    raw = (raw_value or '').strip()
    if raw == '':
        return None, f'{label} is required.'
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, f'{label} must be a valid whole number.'
    if value < min_value:
        return None, f'{label} must be at least {min_value}.'
    if value > max_value:
        return None, f'{label} is too large.'
    return value, ''


def _validate_float_field(raw_value, label, min_value=0.0, max_value=1000.0):
    raw = (raw_value or '').strip()
    if raw == '':
        return None, f'{label} is required.'
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, f'{label} must be a valid number.'
    if value < min_value:
        return None, f'{label} must be at least {min_value}.'
    if value > max_value:
        return None, f'{label} is too large.'
    return value, ''


def _validate_optional_integer_field(raw_value, label, min_value=0, max_value=10_000_000_000):
    raw = (raw_value or '').strip()
    if raw == '':
        return None, ''
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, f'{label} must be a valid whole number.'
    if value < min_value:
        return None, f'{label} must be at least {min_value}.'
    if value > max_value:
        return None, f'{label} is too large.'
    return value, ''


def _parse_statement_month(raw_value):
    raw = (raw_value or '').strip()
    if not raw:
        return None, 'Statement month is required.'
    try:
        year_str, month_str = raw.split('-', 1)
        year = int(year_str)
        month = int(month_str)
        if month < 1 or month > 12:
            raise ValueError
        return date(year, month, 1), ''
    except (ValueError, TypeError):
        return None, 'Statement month must be in YYYY-MM format.'


def _loan_period_months(start_date, end_date):
    months = (end_date.year - start_date.year) * 12 + (end_date.month - start_date.month)
    if end_date.day >= start_date.day:
        months += 1
    return max(1, months)


def _shift_date_by_months(base_date, months):
    target_month_index = (base_date.month - 1) + months
    target_year = base_date.year + (target_month_index // 12)
    target_month = (target_month_index % 12) + 1
    target_day = min(base_date.day, monthrange(target_year, target_month)[1])
    return date(target_year, target_month, target_day)


def _elapsed_months(start_date, reference_date=None):
    reference_date = reference_date or timezone.localdate()
    if reference_date <= start_date:
        return 0
    months = (reference_date.year - start_date.year) * 12 + (reference_date.month - start_date.month)
    if reference_date.day < start_date.day:
        months -= 1
    return max(0, months)


def _emi_from_rate(principal, monthly_rate, tenure_months):
    if tenure_months <= 0:
        return None
    if monthly_rate <= 0:
        return principal / tenure_months
    factor = (1 + monthly_rate) ** tenure_months
    denominator = factor - 1
    if denominator <= 0:
        return None
    return principal * monthly_rate * factor / denominator


def _calculate_monthly_emi(principal, monthly_rate, tenure_months):
    emi = _emi_from_rate(principal, monthly_rate, tenure_months)
    if emi is None:
        return None
    return max(1, int(round(emi)))


def _infer_monthly_rate(principal, monthly_emi, tenure_months):
    if principal <= 0 or monthly_emi <= 0 or tenure_months <= 0:
        return None

    minimum_emi = principal / tenure_months
    if monthly_emi < minimum_emi:
        return None
    if abs(monthly_emi - minimum_emi) < 1e-8:
        return 0.0

    low = 0.0
    high = 1.0
    for _ in range(120):
        mid = (low + high) / 2
        candidate_emi = _emi_from_rate(principal, mid, tenure_months)
        if candidate_emi is None:
            return None
        if candidate_emi > monthly_emi:
            high = mid
        else:
            low = mid
    return (low + high) / 2


def _default_loan_form_values(loan=None):
    values = {
        'loan_type': '',
        'lender': '',
        'principal': '',
        'monthly_emi': '',
        'interest_rate': '',
        'interest_rate_mode': 'annual',
        'loan_period_months': '',
        'months_paid': '',
        'start_date': '',
        'end_date': '',
    }

    if not loan:
        return values

    period_months = _loan_period_months(loan.start_date, loan.end_date)
    elapsed = _elapsed_months(loan.start_date)
    values.update(
        {
            'loan_type': loan.loan_type,
            'lender': loan.lender,
            'principal': str(loan.principal),
            'monthly_emi': str(loan.monthly_emi),
            'interest_rate': f'{loan.interest_rate:.2f}',
            'interest_rate_mode': 'annual',
            'loan_period_months': str(period_months),
            'months_paid': str(min(elapsed, max(0, period_months - 1))),
            'start_date': loan.start_date.isoformat(),
            'end_date': loan.end_date.isoformat(),
        }
    )
    return values


def _income_total_for_user(user):
    income_obj = Income.objects.filter(user=user).first()
    return income_obj.total_income if income_obj else 0


def _other_loans_emi_total(user, exclude_loan_id=None):
    loans_qs = Loan.objects.filter(user=user)
    if exclude_loan_id is not None:
        loans_qs = loans_qs.exclude(id=exclude_loan_id)
    return sum(loans_qs.values_list('monthly_emi', flat=True))


def _validate_loan_form_submission(request):
    form_values = {
        'loan_type': request.POST.get('loan_type', '').strip(),
        'lender': request.POST.get('lender', '').strip(),
        'principal': request.POST.get('principal', '').strip(),
        'monthly_emi': request.POST.get('monthly_emi', '').strip(),
        'interest_rate': request.POST.get('interest_rate', '').strip(),
        'interest_rate_mode': request.POST.get('interest_rate_mode', 'annual').strip().lower(),
        'loan_period_months': request.POST.get('loan_period_months', '').strip(),
        'months_paid': request.POST.get('months_paid', '').strip(),
        'start_date': request.POST.get('start_date', '').strip(),
        'end_date': request.POST.get('end_date', '').strip(),
    }
    if not form_values['interest_rate_mode'] in {'annual', 'monthly'}:
        form_values['interest_rate_mode'] = 'annual'
    errors = []
    start_auto_calculated = False
    end_auto_calculated = False
    months_paid_auto_calculated = False

    loan_type = form_values['loan_type']
    lender = form_values['lender']
    if not loan_type:
        errors.append('Loan type is required.')
    elif len(loan_type) > 120:
        errors.append('Loan type is too long.')
    if len(lender) > 120:
        errors.append('Lender name must be 120 characters or less.')

    principal, principal_error = _validate_integer_field(
        form_values['principal'],
        'Principal',
        min_value=1,
    )
    if principal_error:
        errors.append(principal_error)

    parsed_start_date = None
    parsed_end_date = None
    start_date_raw = form_values['start_date']
    end_date_raw = form_values['end_date']
    if start_date_raw:
        try:
            parsed_start_date = date.fromisoformat(start_date_raw)
        except ValueError:
            errors.append('Start date is invalid.')
    if end_date_raw:
        try:
            parsed_end_date = date.fromisoformat(end_date_raw)
        except ValueError:
            errors.append('End date is invalid.')

    start_min_date, start_max_date = _loan_start_window()
    is_edit_flow = bool(getattr(request, 'resolver_match', None)) and request.resolver_match.url_name == 'edit_loan'
    if (
        parsed_start_date
        and not is_edit_flow
        and (parsed_start_date < start_min_date or parsed_start_date > start_max_date)
    ):
        errors.append(
            f"Start date must be between {start_min_date.isoformat()} and {start_max_date.isoformat()}."
        )

    period_months, period_error = _validate_optional_integer_field(
        form_values['loan_period_months'],
        'Loan period (months)',
        min_value=1,
        max_value=600,
    )
    if period_error:
        errors.append(period_error)

    if period_months is not None:
        if not parsed_start_date:
            errors.append('Start date is required.')
        else:
            calculated_end_date = _shift_date_by_months(parsed_start_date, period_months - 1)
            if parsed_end_date != calculated_end_date:
                end_auto_calculated = True
            parsed_end_date = calculated_end_date
            form_values['end_date'] = parsed_end_date.isoformat()
    elif parsed_start_date and parsed_end_date:
        if parsed_end_date < parsed_start_date:
            errors.append('End date cannot be before start date.')
        else:
            period_months = _loan_period_months(parsed_start_date, parsed_end_date)
            form_values['loan_period_months'] = str(period_months)
    elif parsed_start_date and not parsed_end_date:
        errors.append('Loan period (months) is required to auto-calculate end date.')
    elif not parsed_start_date and parsed_end_date:
        errors.append('Start date is required.')
    else:
        errors.append('Provide start date and loan period details.')

    months_paid_input_empty = not form_values['months_paid']
    months_paid = None
    if not months_paid_input_empty:
        months_paid, months_paid_error = _validate_optional_integer_field(
            form_values['months_paid'],
            'EMIs already paid',
            min_value=0,
            max_value=600,
        )
        if months_paid_error:
            errors.append(months_paid_error)

    if months_paid is None and months_paid_input_empty:
        if parsed_start_date:
            months_paid = _elapsed_months(parsed_start_date, reference_date=timezone.localdate())
            months_paid_auto_calculated = True
        else:
            months_paid = 0
    elif months_paid is None:
        months_paid = 0

    if period_months is not None:
        months_paid = min(months_paid, max(0, period_months - 1))

    if months_paid_input_empty or months_paid_auto_calculated:
        form_values['months_paid'] = str(months_paid)

    if period_months is not None and months_paid >= period_months:
        errors.append('EMIs already paid must be less than total loan period.')

    remaining_months = None
    if period_months is not None:
        remaining_months = period_months - months_paid
        if remaining_months <= 0:
            errors.append('Remaining loan period must be at least 1 month.')

    monthly_emi, emi_error = _validate_optional_integer_field(
        form_values['monthly_emi'],
        'Monthly EMI',
        min_value=1,
        max_value=10_000_000_000,
    )
    if emi_error:
        errors.append(emi_error)

    interest_rate_annual = None
    interest_rate_monthly = None
    interest_rate_raw = form_values['interest_rate']
    if interest_rate_raw:
        if form_values['interest_rate_mode'] == 'monthly':
            monthly_rate_percent, rate_error = _validate_float_field(
                interest_rate_raw,
                'Monthly interest rate',
                min_value=0.0,
                max_value=8.33,
            )
            if rate_error:
                errors.append(rate_error)
            else:
                interest_rate_monthly = monthly_rate_percent / 100.0
                interest_rate_annual = monthly_rate_percent * 12
        else:
            annual_rate_percent, rate_error = _validate_float_field(
                interest_rate_raw,
                'Interest rate',
                min_value=0.0,
                max_value=100.0,
            )
            if rate_error:
                errors.append(rate_error)
            else:
                interest_rate_annual = annual_rate_percent
                interest_rate_monthly = annual_rate_percent / 1200.0

    emi_auto_calculated = False
    rate_auto_calculated = False
    if monthly_emi is None and interest_rate_monthly is None:
        errors.append('Enter either Monthly EMI or Interest rate to auto-calculate the other.')
    elif monthly_emi is None and interest_rate_monthly is not None:
        if remaining_months is None:
            errors.append('Loan period is required to auto-calculate EMI.')
        else:
            monthly_emi = _calculate_monthly_emi(principal or 0, interest_rate_monthly, remaining_months)
            if monthly_emi is None:
                errors.append('Unable to auto-calculate EMI from the provided values.')
            else:
                emi_auto_calculated = True
                form_values['monthly_emi'] = str(monthly_emi)
    elif interest_rate_monthly is None and monthly_emi is not None:
        if remaining_months is None:
            errors.append('Loan period is required to auto-calculate interest rate.')
        else:
            inferred_rate = _infer_monthly_rate(principal or 0, monthly_emi, remaining_months)
            if inferred_rate is None:
                errors.append('Monthly EMI is too low for the selected principal and period.')
            else:
                interest_rate_monthly = inferred_rate
                interest_rate_annual = inferred_rate * 1200.0
                rate_auto_calculated = True
                form_values['interest_rate_mode'] = 'annual'
                form_values['interest_rate'] = f'{interest_rate_annual:.2f}'

    if interest_rate_annual is not None and interest_rate_annual > 100:
        errors.append('Calculated annual interest rate is above 100%. Please verify inputs.')

    if errors:
        return None, form_values, errors

    cleaned = {
        'loan_type': loan_type,
        'lender': lender,
        'principal': principal,
        'monthly_emi': monthly_emi,
        'interest_rate': round(interest_rate_annual, 2),
        'start_date': parsed_start_date,
        'end_date': parsed_end_date,
        'loan_period_months': period_months,
        'months_paid': months_paid,
        'remaining_months': remaining_months,
        'emi_auto_calculated': emi_auto_calculated,
        'rate_auto_calculated': rate_auto_calculated,
        'start_auto_calculated': start_auto_calculated,
        'end_auto_calculated': end_auto_calculated,
        'months_paid_auto_calculated': months_paid_auto_calculated,
    }
    return cleaned, form_values, []


def _validate_phone_number(value):
    normalized = _normalize_phone_number(value)
    if not normalized:
        return '', 'Phone number is required.'
    if len(normalized) < 10 or len(normalized) > 15:
        return '', 'Phone number must be between 10 and 15 digits.'
    return normalized, ''


def _profile_photo_url(user):
    if not user or not getattr(user, 'is_authenticated', False):
        return ''
    profile = UserProfile.objects.filter(user=user).only('profile_photo').first()
    if profile and profile.profile_photo:
        return profile.profile_photo.url
    return ''


def _validate_profile_photo(uploaded_photo):
    if not uploaded_photo:
        return ''

    extension = os.path.splitext(uploaded_photo.name or '')[1].lower()
    if extension not in ALLOWED_PROFILE_PHOTO_EXTENSIONS:
        return 'Profile photo must be JPG, JPEG, PNG, or WEBP.'
    if uploaded_photo.size > MAX_PROFILE_PHOTO_BYTES:
        return 'Profile photo size must be 2 MB or less.'

    content_type = (getattr(uploaded_photo, 'content_type', '') or '').lower()
    if content_type and not content_type.startswith('image/'):
        return 'Uploaded file must be a valid image.'
    return ''


def _get_or_create_profile(user):
    profile, _ = UserProfile.objects.get_or_create(user=user)
    return profile


def _find_user_by_identifier(identifier, superuser_only=None):
    token = (identifier or '').strip()
    if not token:
        return None

    users = User.objects.all()
    if superuser_only is not None:
        users = users.filter(is_superuser=superuser_only)

    if '@' in token:
        return users.filter(email__iexact=token).first()

    normalized_phone = _normalize_phone_number(token)
    if normalized_phone:
        profiles = UserProfile.objects.select_related('user').filter(phone_number=normalized_phone)
        if superuser_only is not None:
            profiles = profiles.filter(user__is_superuser=superuser_only)
        profile = profiles.first()
        if profile:
            return profile.user

    return users.filter(username__iexact=token).first()


def _get_system_settings():
    return SystemSetting.get_solo()


def _resolve_theme(request, settings_obj=None, user_override=None):
    settings_obj = settings_obj or _get_system_settings()
    session_theme = request.session.get('ui_theme')
    if session_theme in {'light', 'dark'}:
        return session_theme

    target_user = user_override if user_override is not None else getattr(request, 'user', None)
    if target_user and getattr(target_user, 'is_authenticated', False) and target_user.is_superuser:
        return settings_obj.admin_theme if settings_obj.admin_theme in {'light', 'dark'} else 'light'
    return 'light'


def _log_admin_action(actor, action, target_user=None, details=''):
    AuditLog.objects.create(
        actor=actor,
        target_user=target_user,
        action=action,
        details=details,
    )


def _render(request, relative_path, context=None, user_override=None):
    settings_obj = _get_system_settings()
    payload = {
        'system_advisory_message': settings_obj.advisory_message.strip(),
        'admin_theme': settings_obj.admin_theme,
        'current_theme': _resolve_theme(request, settings_obj=settings_obj, user_override=user_override),
    }
    if context:
        payload.update(context)
    template_user = user_override if user_override is not None else request.user
    payload['profile_photo_url'] = _profile_photo_url(template_user)
    return render(request, _template_for_user(template_user, relative_path), payload)


def _render_admin_public(request, relative_path, context=None):
    settings_obj = _get_system_settings()
    payload = {
        'system_advisory_message': settings_obj.advisory_message.strip(),
        'admin_theme': settings_obj.admin_theme,
        'current_theme': _resolve_theme(request, settings_obj=settings_obj),
    }
    if context:
        payload.update(context)
    return render(request, f'admin/{relative_path}', payload)


def _absolute_reset_url(request, is_admin=False):
    target = 'admin_reset_password' if is_admin else 'reset_password'
    return request.build_absolute_uri(reverse(target))


def admin_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not request.user.is_superuser:
            messages.error(request, 'Admin access required.')
            return redirect('dashboard')
        return view_func(request, *args, **kwargs)

    return _wrapped


def _block_admin_from_user_modules(request):
    if request.user.is_superuser:
        messages.info(request, 'This module is for normal users.')
        return redirect('dashboard')
    return None


def _month_start(reference_date, months_back):
    year = reference_date.year
    month = reference_date.month - months_back
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def _next_month_start(month_start):
    if month_start.month == 12:
        return date(month_start.year + 1, 1, 1)
    return date(month_start.year, month_start.month + 1, 1)


def _month_start_value(value):
    if not value:
        return None
    return value.replace(day=1)


def _month_gap(start_month, end_month):
    return ((end_month.year - start_month.year) * 12) + (end_month.month - start_month.month)


def _card_emi_monthly_due(principal, annual_rate_percent, tenure_months):
    tenure = max(1, int(tenure_months or 1))
    monthly_rate = max(0.0, float(annual_rate_percent or 0.0)) / 1200.0
    calculated = _calculate_monthly_emi(principal, monthly_rate, tenure)
    if calculated is None:
        return max(1, int(round(principal / tenure)))
    return calculated


def _card_emi_remaining_balance(principal, annual_rate_percent, tenure_months, months_paid):
    amount = max(0.0, float(principal or 0.0))
    tenure = max(1, int(tenure_months or 1))
    paid = max(0, int(months_paid or 0))
    if paid >= tenure:
        return 0.0

    monthly_rate = max(0.0, float(annual_rate_percent or 0.0)) / 1200.0
    if monthly_rate <= 0:
        remaining = amount * ((tenure - paid) / tenure)
        return round(max(0.0, remaining), 2)

    growth_total = (1 + monthly_rate) ** tenure
    growth_paid = (1 + monthly_rate) ** paid
    denominator = growth_total - 1
    if denominator <= 0:
        return round(amount, 2)
    remaining = amount * ((growth_total - growth_paid) / denominator)
    return round(max(0.0, remaining), 2)


def _loan_runtime_breakdown(loans, reference_date=None):
    reference_date = reference_date or timezone.localdate()
    active_loans = []
    upcoming_loans = []
    closed_loans = []
    runtime_rows = []

    for loan in loans:
        total_months = _loan_period_months(loan.start_date, loan.end_date)
        if reference_date < loan.start_date:
            status = 'upcoming'
            elapsed_months = 0
            remaining_months = total_months
            upcoming_loans.append(loan)
        elif reference_date > loan.end_date:
            status = 'closed'
            elapsed_months = total_months
            remaining_months = 0
            closed_loans.append(loan)
        else:
            status = 'active'
            elapsed_months = min(total_months, _elapsed_months(loan.start_date, reference_date))
            remaining_months = max(1, total_months - elapsed_months)
            active_loans.append(loan)

        runtime_rows.append(
            {
                'loan': loan,
                'status': status,
                'total_months': total_months,
                'elapsed_months': elapsed_months,
                'remaining_months': remaining_months,
            }
        )

    return {
        'active_loans': active_loans,
        'upcoming_loans': upcoming_loans,
        'closed_loans': closed_loans,
        'runtime_rows': runtime_rows,
    }


def _loan_remaining_balance_at_month(loan, month_start):
    principal = float(max(0, loan.principal or 0))
    if principal <= 0:
        return 0.0

    start_month = _month_start_value(loan.start_date)
    end_month = _month_start_value(loan.end_date)
    if month_start < start_month:
        return 0.0
    if month_start > end_month:
        return 0.0

    months_elapsed = max(0, _month_gap(start_month, month_start))
    tenure_months = _loan_period_months(loan.start_date, loan.end_date)
    if months_elapsed >= tenure_months:
        return 0.0

    monthly_rate = max(0.0, float(loan.interest_rate or 0.0)) / 1200.0
    monthly_emi = float(max(0, loan.monthly_emi or 0))
    balance = principal

    for _ in range(months_elapsed):
        if monthly_rate > 0:
            balance += balance * monthly_rate
        balance -= monthly_emi
        if balance <= 0:
            return 0.0

    return round(max(0.0, balance), 2)


def _months_to_date(reference_date, target_date):
    if target_date <= reference_date:
        return 0
    months = (target_date.year - reference_date.year) * 12 + (target_date.month - reference_date.month)
    if target_date.day >= reference_date.day:
        months += 1
    return max(0, months)


def _loan_start_window(reference_date=None):
    today = reference_date or timezone.localdate()
    min_start = _shift_date_by_months(today, -2)
    return min_start, today


def _credit_card_snapshot(user, reference_date=None):
    reference_date = reference_date or timezone.localdate()
    reference_month = _month_start_value(reference_date)

    cards = list(CreditCardAccount.objects.filter(user=user).order_by('card_name', 'id'))
    entries = list(
        CreditCardEntry.objects.filter(card__user=user)
        .select_related('card')
        .order_by('-entry_month', '-id')
    )

    total_emi_monthly_due = 0.0
    total_monthly_spend_amount = 0.0
    total_emi_remaining_balance = 0.0
    total_amount = 0.0
    total_interest_estimate = 0.0
    total_reward_estimate = 0.0
    weighted_rate_numerator = 0.0

    per_card_data = defaultdict(
        lambda: {
            'card': None,
            'emi_monthly_due': 0.0,
            'emi_remaining_balance': 0.0,
            'monthly_spend_amount': 0.0,
            'total_amount': 0.0,
            'interest_estimate': 0.0,
            'reward_estimate': 0.0,
            'entry_count': 0,
            'spend_entry_count': 0,
            'emi_entry_count': 0,
            'closed_emi_entry_count': 0,
            'upcoming_emi_entry_count': 0,
        }
    )

    for entry in entries:
        card_row = per_card_data[entry.card_id]
        card_row['card'] = entry.card
        card_row['entry_count'] += 1
        entry_month = _month_start_value(entry.entry_month)

        if entry.entry_type == CreditCardEntry.TYPE_EMI:
            tenure_months = max(1, int(entry.tenure_months or 1))
            elapsed_months = _month_gap(entry_month, reference_month)
            if elapsed_months < 0:
                card_row['upcoming_emi_entry_count'] += 1
                continue
            if elapsed_months >= tenure_months:
                card_row['closed_emi_entry_count'] += 1
                continue

            monthly_due = _card_emi_monthly_due(
                principal=entry.amount,
                annual_rate_percent=entry.card.emi_interest_rate,
                tenure_months=tenure_months,
            )
            remaining_balance = _card_emi_remaining_balance(
                principal=entry.amount,
                annual_rate_percent=entry.card.emi_interest_rate,
                tenure_months=tenure_months,
                months_paid=elapsed_months,
            )
            monthly_interest = round(remaining_balance * (entry.card.emi_interest_rate / 1200.0), 2)

            card_row['emi_entry_count'] += 1
            card_row['emi_monthly_due'] += monthly_due
            card_row['emi_remaining_balance'] += remaining_balance
            card_row['total_amount'] += remaining_balance
            card_row['interest_estimate'] += monthly_interest

            total_emi_monthly_due += monthly_due
            total_emi_remaining_balance += remaining_balance
            total_amount += remaining_balance
            total_interest_estimate += monthly_interest
            weighted_rate_numerator += remaining_balance * entry.card.emi_interest_rate
            continue

        # Monthly spend is statement-month specific; older months are treated as paid.
        if entry_month != reference_month:
            continue

        spend_amount = float(max(0, entry.amount))
        reward_estimate = round(spend_amount * (entry.card.reward_percent / 100.0), 2)

        card_row['spend_entry_count'] += 1
        card_row['monthly_spend_amount'] += spend_amount
        card_row['total_amount'] += spend_amount
        card_row['reward_estimate'] += reward_estimate

        total_monthly_spend_amount += spend_amount
        total_amount += spend_amount
        total_reward_estimate += reward_estimate

    weighted_apr = round(weighted_rate_numerator / total_amount, 2) if total_amount > 0 else 0.0

    per_card_rows = []
    for item in per_card_data.values():
        total_for_card = round(item['total_amount'], 2)
        credit_limit = max(0, getattr(item['card'], 'credit_limit', 0) or 0)
        available_limit = max(0, credit_limit - total_for_card)
        utilization_percent = round((total_for_card / credit_limit) * 100, 1) if credit_limit > 0 else 0.0
        if total_for_card > 0:
            emi_share_percent = round((item['emi_remaining_balance'] / total_for_card) * 100, 1)
            spend_share_percent = round((item['monthly_spend_amount'] / total_for_card) * 100, 1)
        else:
            emi_share_percent = 0.0
            spend_share_percent = 0.0

        per_card_rows.append(
            {
                **item,
                'emi_monthly_due': round(item['emi_monthly_due'], 2),
                'emi_remaining_balance': round(item['emi_remaining_balance'], 2),
                'monthly_spend_amount': round(item['monthly_spend_amount'], 2),
                'total_amount': total_for_card,
                'interest_estimate': round(item['interest_estimate'], 2),
                'reward_estimate': round(item['reward_estimate'], 2),
                'emi_share_percent': emi_share_percent,
                'spend_share_percent': spend_share_percent,
                'net_cost': round(item['interest_estimate'] - item['reward_estimate'], 2),
                'credit_limit': credit_limit,
                'available_limit': available_limit,
                'utilization_percent': utilization_percent,
            }
        )

    for card in cards:
        if card.id in per_card_data:
            continue
        per_card_rows.append(
            {
                'card': card,
                'emi_monthly_due': 0.0,
                'emi_remaining_balance': 0.0,
                'monthly_spend_amount': 0.0,
                'total_amount': 0.0,
                'interest_estimate': 0.0,
                'reward_estimate': 0.0,
                'entry_count': 0,
                'spend_entry_count': 0,
                'emi_entry_count': 0,
                'closed_emi_entry_count': 0,
                'upcoming_emi_entry_count': 0,
                'emi_share_percent': 0.0,
                'spend_share_percent': 0.0,
                'net_cost': 0.0,
                'credit_limit': max(0, card.credit_limit or 0),
                'available_limit': max(0, card.credit_limit or 0),
                'utilization_percent': 0.0,
            }
        )
    per_card_rows.sort(key=lambda row: (-row['total_amount'], row['card'].card_name.lower()))

    return {
        'cards': cards,
        'entries': entries,
        'per_card_rows': per_card_rows,
        'total_emi_amount': round(total_emi_monthly_due, 2),
        'total_monthly_spend_amount': round(total_monthly_spend_amount, 2),
        'total_emi_remaining_balance': round(total_emi_remaining_balance, 2),
        'total_amount': round(total_amount, 2),
        'weighted_apr': weighted_apr,
        'monthly_interest_estimate': round(total_interest_estimate, 2),
        'monthly_reward_estimate': round(total_reward_estimate, 2),
        'monthly_net_cost': round(total_interest_estimate - total_reward_estimate, 2),
        'legacy_entry_count': 0,
        'legacy_current_entry_count': 0,
        'legacy_current_outstanding': 0.0,
        'legacy_min_due': 0.0,
        'active_emi_entry_count': sum(row['emi_entry_count'] for row in per_card_rows),
        'current_statement_month': reference_month,
    }


def _financial_snapshot(user, settings_obj=None):
    settings_obj = settings_obj or _get_system_settings()
    today = timezone.localdate()

    income_obj = Income.objects.filter(user=user).first()
    total_income = income_obj.total_income if income_obj else 0

    loans = list(Loan.objects.filter(user=user).order_by('end_date', 'id'))
    loan_breakdown = _loan_runtime_breakdown(loans, reference_date=today)
    active_loans = loan_breakdown['active_loans']
    upcoming_loans = loan_breakdown['upcoming_loans']
    closed_loans = loan_breakdown['closed_loans']
    loan_runtime_rows = loan_breakdown['runtime_rows']

    total_emi = sum(loan.monthly_emi for loan in active_loans)
    cc_snapshot = _credit_card_snapshot(user, reference_date=today)
    credit_card_total_emi = round(cc_snapshot['total_emi_amount'], 2)
    credit_card_total_spend = round(cc_snapshot['total_monthly_spend_amount'], 2)
    credit_card_total_outstanding = round(cc_snapshot['total_amount'], 2)
    credit_card_total_emi_remaining_balance = round(cc_snapshot.get('total_emi_remaining_balance', 0.0), 2)
    credit_card_current_month = cc_snapshot.get('current_statement_month', today.replace(day=1))
    credit_card_weighted_apr = cc_snapshot['weighted_apr']
    credit_card_monthly_interest = cc_snapshot['monthly_interest_estimate']
    credit_card_monthly_rewards = cc_snapshot['monthly_reward_estimate']
    credit_card_monthly_net_cost = cc_snapshot['monthly_net_cost']
    credit_card_min_due_total = cc_snapshot['legacy_min_due']
    credit_card_total_limit = sum(max(0, row.get('credit_limit', 0)) for row in cc_snapshot['per_card_rows'])
    credit_card_available_limit = max(0, credit_card_total_limit - credit_card_total_outstanding)
    credit_card_utilization_ratio = (
        round((credit_card_total_outstanding / credit_card_total_limit) * 100, 2)
        if credit_card_total_limit > 0
        else 0.0
    )
    credit_card_utilization_progress = max(0, min(100, round(credit_card_utilization_ratio, 1)))
    credit_card_min_due_estimate = round(credit_card_total_emi + (credit_card_total_spend * 0.05), 2)
    credit_card_due_estimate = round(credit_card_total_emi + credit_card_total_spend, 2)
    total_monthly_obligation = round(total_emi + credit_card_due_estimate, 2)
    if total_income > 0:
        overall_burden_ratio = round((total_monthly_obligation / total_income) * 100, 2)
        emi_ratio = round((total_emi / total_income) * 100, 2)
    else:
        overall_burden_ratio = 100.0 if total_monthly_obligation > 0 else 0.0
        emi_ratio = 100.0 if total_emi > 0 else 0.0
    overall_burden_progress = max(0, min(100, round(overall_burden_ratio, 1)))

    if emi_ratio < settings_obj.emi_green_limit:
        health_class = 'green'
        health_zone = 'Green Zone'
        smart_suggestion = 'Safe zone: continue good discipline.'
    elif emi_ratio <= settings_obj.emi_yellow_limit:
        health_class = 'yellow'
        health_zone = 'Yellow Zone'
        smart_suggestion = 'Risky zone: reduce expenses 10-15%, consider refinancing.'
    else:
        health_class = 'red'
        health_zone = 'Red Zone'
        smart_suggestion = 'Danger zone: use avalanche/snowball method.'

    if not active_loans and upcoming_loans:
        smart_suggestion = 'No active EMI right now. Build buffer before upcoming loans start.'

    if overall_burden_ratio < settings_obj.emi_green_limit:
        overall_health_class = 'green'
        overall_health_zone = 'Green Zone'
        overall_suggestion = 'Safe zone: overall debt burden is manageable.'
    elif overall_burden_ratio <= settings_obj.emi_yellow_limit:
        overall_health_class = 'yellow'
        overall_health_zone = 'Yellow Zone'
        overall_suggestion = 'Risky zone: cut discretionary spend and reduce card utilization.'
    else:
        overall_health_class = 'red'
        overall_health_zone = 'Red Zone'
        overall_suggestion = 'Danger zone: prioritize debt repayment and avoid new card spends.'

    if credit_card_utilization_ratio >= 80:
        credit_card_alert = 'Card utilization is above 80%. Focus on repayment and pause new spends.'
    elif credit_card_utilization_ratio >= 50:
        credit_card_alert = 'Card utilization is moderate-high. Target utilization below 30%.'
    elif credit_card_total_limit > 0:
        credit_card_alert = 'Card utilization is in a healthier range. Maintain timely payments.'
    else:
        credit_card_alert = 'No card limit configured yet. Add card limits for better debt tracking.'

    budget_obj = Budget.objects.filter(user=user).first()
    total_budget_expense = budget_obj.total_expense if budget_obj else 0

    remaining_after_emi = total_income - total_emi
    remaining_after_obligations = total_income - total_monthly_obligation
    net_savings = remaining_after_emi - total_budget_expense
    net_savings_after_cards = remaining_after_obligations - total_budget_expense
    savings_target = round(total_income * (settings_obj.savings_target_percent / 100.0), 2)

    if total_income > 0:
        emi_progress = max(0, min(100, round((total_emi / total_income) * 100, 1)))
    else:
        emi_progress = 0

    if savings_target > 0:
        savings_progress = max(0, min(100, round((net_savings / savings_target) * 100, 1)))
        savings_progress_after_cards = max(
            0,
            min(100, round((net_savings_after_cards / savings_target) * 100, 1)),
        )
    else:
        savings_progress = 0
        savings_progress_after_cards = 0

    high_interest_loans = [
        loan for loan in active_loans if loan.interest_rate > settings_obj.high_interest_rate_limit
    ]
    top_priority_loan = max(high_interest_loans, key=lambda loan: loan.interest_rate, default=None)

    if top_priority_loan:
        priority_suggestion = (
            f"Prioritize {top_priority_loan.loan_type} first at "
            f"{top_priority_loan.interest_rate:.2f}% interest."
        )
        refinancing_suggestion = (
            f"Consider refinancing {len(high_interest_loans)} high-interest loan(s) "
            f"above {settings_obj.high_interest_rate_limit:.1f}%."
        )
    else:
        priority_suggestion = 'No high-interest loan priority right now.'
        refinancing_suggestion = 'No refinancing alert. Current rates are within threshold.'

    if not active_loans:
        if upcoming_loans:
            next_start = min(loan.start_date for loan in upcoming_loans)
            repayment_strategy = (
                f'No active EMI. Upcoming loan starts on {next_start.strftime("%d %b %Y")}; '
                'prepare cash buffer and avoid new debt.'
            )
        else:
            repayment_strategy = 'No active loans. Keep saving and avoid new high-interest debt.'
    elif emi_ratio > settings_obj.emi_yellow_limit:
        repayment_strategy = (
            'Use avalanche method: pay minimum on all loans and put extra payment on '
            'the highest-interest loan first.'
        )
    elif len(active_loans) > 1:
        repayment_strategy = (
            'Use snowball for motivation or avalanche for lower total interest. '
            'Pick one method and stay consistent.'
        )
    else:
        repayment_strategy = (
            'Single loan detected. Continue EMI and prepay principal when cashflow allows.'
        )

    debt_free_text = 'No active loans'
    planning_loans = active_loans + upcoming_loans
    if planning_loans:
        latest_end_date = max(loan.end_date for loan in planning_loans)
        month_gap = _months_to_date(today, latest_end_date)
        if not active_loans and upcoming_loans:
            next_start = min(loan.start_date for loan in upcoming_loans)
            debt_free_text = (
                f"Starts {next_start.strftime('%d %b %Y')} | "
                f"Debt-free by {latest_end_date.strftime('%d %b %Y')} (~{month_gap} months)"
            )
        else:
            debt_free_text = f"{latest_end_date.strftime('%d %b %Y')} (~{month_gap} months)"

    return {
        'income_obj': income_obj,
        'budget_obj': budget_obj,
        'loans': loans,
        'active_loans': active_loans,
        'upcoming_loans': upcoming_loans,
        'closed_loans': closed_loans,
        'loan_runtime_rows': loan_runtime_rows,
        'active_loan_count': len(active_loans),
        'upcoming_loan_count': len(upcoming_loans),
        'closed_loan_count': len(closed_loans),
        'credit_card_spends': cc_snapshot['entries'],
        'credit_card_accounts': cc_snapshot['cards'],
        'credit_card_card_rows': cc_snapshot['per_card_rows'],
        'credit_card_total_spend': credit_card_total_spend,
        'credit_card_total_spend_display': f'{credit_card_total_spend:,.0f}',
        'credit_card_current_month_label': credit_card_current_month.strftime('%b %Y'),
        'credit_card_total_emi': credit_card_total_emi,
        'credit_card_active_emi_count': cc_snapshot.get('active_emi_entry_count', 0),
        'credit_card_total_emi_remaining_balance': credit_card_total_emi_remaining_balance,
        'credit_card_total_outstanding': credit_card_total_outstanding,
        'credit_card_total_limit': credit_card_total_limit,
        'credit_card_available_limit': credit_card_available_limit,
        'credit_card_utilization_ratio': credit_card_utilization_ratio,
        'credit_card_utilization_progress': credit_card_utilization_progress,
        'credit_card_min_due_estimate': credit_card_min_due_estimate,
        'credit_card_due_estimate': credit_card_due_estimate,
        'credit_card_min_due_total': credit_card_min_due_total,
        'credit_card_weighted_apr': credit_card_weighted_apr,
        'credit_card_monthly_interest': credit_card_monthly_interest,
        'credit_card_monthly_rewards': credit_card_monthly_rewards,
        'credit_card_monthly_net_cost': credit_card_monthly_net_cost,
        'credit_card_legacy_count': cc_snapshot['legacy_entry_count'],
        'credit_card_legacy_current_count': cc_snapshot.get('legacy_current_entry_count', 0),
        'credit_card_legacy_current_outstanding': cc_snapshot.get('legacy_current_outstanding', 0),
        'total_income': total_income,
        'total_emi': total_emi,
        'total_monthly_obligation': total_monthly_obligation,
        'emi_ratio': emi_ratio,
        'overall_burden_ratio': overall_burden_ratio,
        'overall_burden_progress': overall_burden_progress,
        'overall_health_class': overall_health_class,
        'overall_health_zone': overall_health_zone,
        'overall_suggestion': overall_suggestion,
        'credit_card_alert': credit_card_alert,
        'health_class': health_class,
        'health_zone': health_zone,
        'smart_suggestion': smart_suggestion,
        'total_budget_expense': total_budget_expense,
        'remaining_after_emi': remaining_after_emi,
        'remaining_after_obligations': remaining_after_obligations,
        'net_savings': net_savings,
        'net_savings_after_cards': net_savings_after_cards,
        'savings_target': savings_target,
        'emi_progress': emi_progress,
        'savings_progress': savings_progress,
        'savings_progress_after_cards': savings_progress_after_cards,
        'high_interest_loans': high_interest_loans,
        'priority_suggestion': priority_suggestion,
        'refinancing_suggestion': refinancing_suggestion,
        'repayment_strategy': repayment_strategy,
        'debt_free_text': debt_free_text,
        'green_limit': settings_obj.emi_green_limit,
        'yellow_limit': settings_obj.emi_yellow_limit,
        'high_interest_limit': settings_obj.high_interest_rate_limit,
        'savings_target_percent': settings_obj.savings_target_percent,
    }


def _build_chart_payload(snapshot):
    active_loans = list(snapshot.get('active_loans', []))
    upcoming_loans = list(snapshot.get('upcoming_loans', []))
    chart_loans = active_loans if active_loans else upcoming_loans
    timeline_loans = sorted(active_loans + upcoming_loans, key=lambda loan: loan.end_date)

    debt_labels = []
    debt_values = []
    if chart_loans:
        debt_labels.extend([loan.loan_type for loan in chart_loans])
        debt_values.extend([loan.monthly_emi for loan in chart_loans])

    card_emi = snapshot.get('credit_card_total_emi', 0)
    card_spend = snapshot.get('credit_card_total_spend', 0)
    if card_emi > 0:
        debt_labels.append('Card EMI (Active)')
        debt_values.append(card_emi)
    if card_spend > 0:
        debt_labels.append('Card Spend (Current Month)')
        debt_values.append(card_spend)
    if not debt_labels:
        debt_labels = ['No Active Debt']
        debt_values = [1]

    if timeline_loans:
        cursor = timezone.localdate().replace(day=1)
        max_end_month = max(_month_start_value(loan.end_date) for loan in timeline_loans)
        timeline_labels = []
        timeline_values = []
        while cursor <= max_end_month:
            month_total = sum(
                _loan_remaining_balance_at_month(loan, cursor)
                for loan in timeline_loans
            )
            timeline_labels.append(cursor.strftime('%b %Y'))
            timeline_values.append(round(month_total, 2))
            cursor = _next_month_start(cursor)
    else:
        timeline_labels = [timezone.localdate().strftime('%b %Y')]
        timeline_values = [0]

    return {
        'emi_distribution': {
            'labels': debt_labels,
            'values': debt_values,
        },
        'cashflow': {
            'labels': [
                'Income',
                'Loan EMI',
                'Card EMI (Active)',
                'Card Spend (Current Month)',
                'Budget Expenses',
                'Net Savings',
            ],
            'values': [
                snapshot['total_income'],
                snapshot['total_emi'],
                card_emi,
                card_spend,
                snapshot['total_budget_expense'],
                snapshot.get('net_savings_after_cards', snapshot['net_savings']),
            ],
        },
        'loan_timeline': {
            'labels': timeline_labels,
            'values': timeline_values,
        },
    }


def _admin_user_queryset():
    return User.objects.filter(is_superuser=False).order_by('-date_joined')


def _admin_user_rows(users, settings_obj):
    rows = []
    for user in users:
        snapshot = _financial_snapshot(user, settings_obj=settings_obj)
        risk = _risk_profile(snapshot)
        rows.append(
            {
                'user': user,
                'emi_ratio': snapshot['emi_ratio'],
                'overall_burden_ratio': snapshot['overall_burden_ratio'],
                'health_class': snapshot['health_class'],
                'health_zone': snapshot['health_zone'],
                'loan_count': snapshot.get('active_loan_count', len(snapshot['loans'])),
                'high_interest_count': len(snapshot['high_interest_loans']),
                'is_active': user.is_active,
                'last_login': user.last_login,
                'masked_email': _mask_email(user.email),
                'risk_level': risk['level'],
                'risk_label': risk['label'],
                'risk_reasons': risk['reasons'],
            }
        )
    return rows


def _monthly_signup_trend(user_queryset, months=6):
    today = timezone.localdate().replace(day=1)
    labels = []
    values = []

    for offset in range(months - 1, -1, -1):
        month_start = _month_start(today, offset)
        next_month = _next_month_start(month_start)
        labels.append(month_start.strftime('%b %Y'))
        values.append(
            user_queryset.filter(
                date_joined__date__gte=month_start,
                date_joined__date__lt=next_month,
            ).count()
        )

    return labels, values


def _zone_counts(user_rows):
    safe = sum(1 for row in user_rows if row['health_class'] == 'green')
    risky = sum(1 for row in user_rows if row['health_class'] == 'yellow')
    danger = sum(1 for row in user_rows if row['health_class'] == 'red')
    return safe, risky, danger


def _loan_mix_counts():
    loan_mix = (
        Loan.objects.filter(user__is_superuser=False)
        .values('loan_type')
        .annotate(total=Count('id'))
        .order_by('-total')[:8]
    )
    labels = [entry['loan_type'] for entry in loan_mix]
    values = [entry['total'] for entry in loan_mix]
    if not labels:
        labels = ['No Loans']
        values = [1]
    return labels, values


def _build_admin_chart_payload(user_rows, signup_labels, signup_values):
    safe_count, risky_count, danger_count = _zone_counts(user_rows)
    loan_labels, loan_values = _loan_mix_counts()
    return {
        'zone_distribution': {
            'labels': ['Safe', 'Risky', 'Danger'],
            'values': [safe_count, risky_count, danger_count],
        },
        'signup_trend': {
            'labels': signup_labels,
            'values': signup_values,
        },
        'loan_mix': {
            'labels': loan_labels,
            'values': loan_values,
        },
    }


def _mask_email(email):
    if not email or '@' not in email:
        return 'Not set'
    local, domain = email.split('@', 1)
    if len(local) <= 2:
        masked_local = local[:1] + '*'
    else:
        masked_local = local[:2] + ('*' * (len(local) - 2))
    return f'{masked_local}@{domain}'


def _risk_profile(snapshot):
    emi_ratio = snapshot['emi_ratio']
    overall_burden_ratio = snapshot.get('overall_burden_ratio', emi_ratio)
    debt_load_ratio = max(emi_ratio, overall_burden_ratio)
    loans = snapshot.get('active_loans') or snapshot['loans']
    upcoming_loans = snapshot.get('upcoming_loans', [])
    budget_total = snapshot['total_budget_expense']
    income_total = snapshot['total_income']
    high_interest_extreme = any(loan.interest_rate > 18 for loan in loans)
    has_many_loans = len(loans) > 3
    high_emi = debt_load_ratio > 60
    medium_emi = 40 <= debt_load_ratio <= 60
    expense_spike = income_total > 0 and budget_total > (income_total * 0.75)

    reasons = []
    if high_emi:
        reasons.append('Overall debt obligation ratio above 60%.')
    if has_many_loans:
        reasons.append('More than 3 active loans.')
    if high_interest_extreme:
        reasons.append('At least one loan above 18% interest.')

    if high_emi or has_many_loans or high_interest_extreme:
        level = 'high'
        label = 'High Risk'
    elif medium_emi or expense_spike:
        level = 'medium'
        label = 'Medium Risk'
        if medium_emi:
            reasons.append('Overall debt obligation ratio between 40% and 60%.')
        if expense_spike:
            reasons.append('Expense spike detected in budget categories.')
    else:
        level = 'low'
        label = 'Low Risk'
        if debt_load_ratio < 30:
            reasons.append('Overall debt obligation ratio is under 30%.')
            if not loans and upcoming_loans:
                reasons.append('No active EMI yet; upcoming loans are scheduled.')
        else:
            reasons.append('Debt load is manageable with current inputs.')

    return {
        'level': level,
        'label': label,
        'reasons': reasons,
        'expense_spike': expense_spike,
    }


def _escape_pdf_text(text):
    return text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')


def _pdf_wrap_lines(text, max_chars=88):
    words = str(text or '').split()
    if not words:
        return ['']

    lines = []
    current = words[0]
    for word in words[1:]:
        candidate = f'{current} {word}'
        if len(candidate) <= max_chars:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _build_structured_pdf_bytes(title, subtitle, sections):
    page_width = 612
    margin_left = 50
    margin_right = 50
    content_top = 728
    content_bottom = 64
    line_height = 15
    section_gap = 12

    streams = []

    def begin_page(page_number):
        page_commands = [
            'q',
            '0.93 0.95 0.99 rg',
            f'{margin_left} 742 {page_width - (margin_left + margin_right)} 26 re f',
            'Q',
            'q',
            '0.74 0.79 0.90 RG',
            '1 w',
            f'{margin_left} 742 {page_width - (margin_left + margin_right)} 26 re S',
            'Q',
            f'BT /F2 16 Tf {margin_left + 8} 750 Td ({_escape_pdf_text(title)}) Tj ET',
            f'BT /F1 10 Tf {margin_left + 8} 736 Td ({_escape_pdf_text(subtitle)}) Tj ET',
            (
                f'BT /F1 9 Tf {page_width - margin_right - 72} 736 Td '
                f'(Page {page_number}) Tj ET'
            ),
        ]
        return page_commands, content_top

    page_number = 1
    current_page, cursor_y = begin_page(page_number)

    for section in sections:
        heading = section.get('heading', '').strip()
        rows = section.get('rows', [])
        if cursor_y <= content_bottom + 28:
            streams.append('\n'.join(current_page))
            page_number += 1
            current_page, cursor_y = begin_page(page_number)

        if heading:
            current_page.extend(
                [
                    'q',
                    '0.22 0.33 0.53 rg',
                    f'{margin_left} {cursor_y - 3} 6 6 re f',
                    'Q',
                    f'BT /F2 12 Tf {margin_left + 12} {cursor_y - 1} Td ({_escape_pdf_text(heading)}) Tj ET',
                ]
            )
            cursor_y -= (line_height + 2)

        for row in rows:
            bullet_prefix = '- '
            wrapped_rows = _pdf_wrap_lines(str(row), max_chars=86)
            for wrapped_index, wrapped_line in enumerate(wrapped_rows):
                if cursor_y <= content_bottom:
                    streams.append('\n'.join(current_page))
                    page_number += 1
                    current_page, cursor_y = begin_page(page_number)
                prefix = bullet_prefix if wrapped_index == 0 else '  '
                current_page.append(
                    f'BT /F1 10 Tf {margin_left} {cursor_y} Td ({_escape_pdf_text(prefix + wrapped_line)}) Tj ET'
                )
                cursor_y -= line_height

        cursor_y -= section_gap

    streams.append('\n'.join(current_page))

    stream_objects = []
    for stream in streams:
        stream_bytes = stream.encode('latin-1', errors='replace')
        stream_objects.append(
            f'<< /Length {len(stream_bytes)} >>\nstream\n{stream}\nendstream'
        )

    page_count = len(stream_objects)
    catalog_id = 1
    first_page_id = 5
    first_stream_id = first_page_id + page_count

    kids_refs = ' '.join(f'{first_page_id + idx} 0 R' for idx in range(page_count))
    objects = [
        '<< /Type /Catalog /Pages 2 0 R >>',
        f'<< /Type /Pages /Kids [{kids_refs}] /Count {page_count} >>',
        '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>',
    ]

    for idx in range(page_count):
        page_obj = (
            '<< /Type /Page /Parent 2 0 R '
            '/MediaBox [0 0 612 792] '
            f'/Contents {first_stream_id + idx} 0 R '
            '/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> >>'
        )
        objects.append(page_obj)

    objects.extend(stream_objects)

    chunks = [b'%PDF-1.4\n']
    offsets = [0]
    current_offset = len(chunks[0])

    for index, obj in enumerate(objects, start=1):
        obj_bytes = f'{index} 0 obj\n{obj}\nendobj\n'.encode('latin-1', errors='replace')
        offsets.append(current_offset)
        chunks.append(obj_bytes)
        current_offset += len(obj_bytes)

    xref_start = current_offset
    xref_lines = [f'xref\n0 {len(objects) + 1}\n', '0000000000 65535 f \n']
    for offset in offsets[1:]:
        xref_lines.append(f'{offset:010} 00000 n \n')

    trailer = (
        ''.join(xref_lines)
        + f'trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n'
        f'startxref\n{xref_start}\n%%EOF'
    )
    chunks.append(trailer.encode('latin-1', errors='replace'))
    return b''.join(chunks)


def admin_root_redirect(request):
    if request.user.is_authenticated and request.user.is_superuser:
        return redirect('dashboard')
    if request.user.is_authenticated and not request.user.is_superuser:
        messages.error(request, 'Admin access required.')
        return redirect('dashboard')
    return redirect('admin_login')


def register_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':
        username_raw = request.POST.get('username', '').strip()
        email = request.POST.get('email', '').strip()
        phone_number = request.POST.get('phone_number', '').strip()
        profile_photo = request.FILES.get('profile_photo')
        password = request.POST.get('password', '')
        confirm_password = request.POST.get('confirm_password', '')
        username, username_error = _validate_username(username_raw)
        normalized_phone, phone_error = _validate_phone_number(phone_number)
        password_error = _validate_password(password)
        profile_photo_error = _validate_profile_photo(profile_photo)

        if not username_raw or not email or not password or not phone_number:
            messages.error(request, 'All fields are required.')
        elif username_error:
            messages.error(request, username_error)
        elif not _is_valid_email(email):
            messages.error(request, 'Please enter a valid email address.')
        elif phone_error:
            messages.error(request, phone_error)
        elif profile_photo_error:
            messages.error(request, profile_photo_error)
        elif password_error:
            messages.error(request, password_error)
        elif password != confirm_password:
            messages.error(request, 'Passwords do not match.')
        elif User.objects.filter(username=username).exists():
            messages.error(request, 'Username already exists.')
        elif User.objects.filter(email=email).exists():
            messages.error(request, 'Email already registered.')
        elif UserProfile.objects.filter(phone_number=normalized_phone).exists():
            messages.error(request, 'Phone number already registered.')
        else:
            user = User.objects.create_user(username=username, email=email, password=password)
            UserProfile.objects.create(
                user=user,
                phone_number=normalized_phone,
                profile_photo=profile_photo if profile_photo else None,
            )
            messages.success(request, 'Registration successful. Please login.')
            return redirect('login')

    return _render(request, 'register.html')


def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':
        identifier = request.POST.get('identifier', '').strip() or request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        if not identifier or not password:
            messages.error(request, 'Login ID and password are required.')
            return _render(request, 'login.html')
        user_obj = _find_user_by_identifier(identifier)

        user = None
        if user_obj:
            user = authenticate(request, username=user_obj.username, password=password)

        if user is not None:
            if not user.is_active:
                messages.error(request, 'Your account is currently inactive.')
                return _render(request, 'login.html')
            login(request, user)
            return redirect('dashboard')

        messages.error(request, 'Invalid login credentials. Use username, email, or phone.')

    return _render(request, 'login.html')


def admin_login_view(request):
    if request.user.is_authenticated:
        if request.user.is_superuser:
            return redirect('dashboard')
        return redirect('dashboard')

    if request.method == 'POST':
        identifier = request.POST.get('identifier', '').strip() or request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        if not identifier or not password:
            messages.error(request, 'Login ID and password are required.')
            return _render_admin_public(request, 'login.html')
        user_obj = _find_user_by_identifier(identifier, superuser_only=True)
        user = None
        if user_obj:
            user = authenticate(request, username=user_obj.username, password=password)

        if user is None:
            messages.error(request, 'Invalid admin credentials. Use username, email, or phone.')
        elif not user.is_superuser:
            messages.error(request, 'Admin access required.')
        elif not user.is_active:
            messages.error(request, 'Your admin account is inactive.')
        else:
            login(request, user)
            return redirect('dashboard')

    return _render_admin_public(request, 'login.html')


@login_required
def logout_view(request):
    request.session.pop('locked_user_id', None)
    logout(request)
    messages.info(request, 'Logged out successfully.')
    return redirect('login')


def toggle_theme_view(request):
    if request.method != 'POST':
        if request.user.is_authenticated:
            return redirect('dashboard')
        return redirect('login')

    settings_obj = _get_system_settings()
    current_theme = _resolve_theme(request, settings_obj=settings_obj)
    requested_theme = (request.POST.get('theme') or '').strip().lower()
    if requested_theme in {'light', 'dark'}:
        next_theme = requested_theme
    else:
        next_theme = 'dark' if current_theme == 'light' else 'light'

    request.session['ui_theme'] = next_theme

    next_url = request.POST.get('next', '').strip()
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)

    referrer = request.META.get('HTTP_REFERER', '').strip()
    if referrer and url_has_allowed_host_and_scheme(
        referrer,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(referrer)

    if request.user.is_authenticated:
        return redirect('dashboard')
    return redirect('login')


def forgot_password_view(request):
    if request.method == 'POST':
        email = request.POST.get('email', '').strip()
        if not _is_valid_email(email):
            messages.error(request, 'Please enter a valid email address.')
            return _render(request, 'forgot_password.html')
        user = User.objects.filter(email=email).first()

        if not user:
            messages.error(request, 'No account found with this email.')
        else:
            otp = str(random.randint(100000, 999999))
            expires_at = (timezone.now() + timedelta(minutes=10)).isoformat()
            request.session['reset_otp_data'] = {
                'email': email,
                'otp': otp,
                'expires_at': expires_at,
            }
            send_otp_email(
                email=email,
                otp=otp,
                recipient_name=user.username,
                account_role='User',
                reset_url=_absolute_reset_url(request, is_admin=False),
                valid_minutes=10,
            )
            messages.success(request, 'OTP sent to your email address.')
            return redirect('reset_password')

    return _render(request, 'forgot_password.html')


def admin_forgot_password_view(request):
    if request.method == 'POST':
        email = request.POST.get('email', '').strip()
        if not _is_valid_email(email):
            messages.error(request, 'Please enter a valid email address.')
            return _render_admin_public(request, 'forgot_password.html')
        user = User.objects.filter(email=email, is_superuser=True).first()

        if not user:
            messages.error(request, 'No admin account found with this email.')
        else:
            otp = str(random.randint(100000, 999999))
            expires_at = (timezone.now() + timedelta(minutes=10)).isoformat()
            request.session['admin_reset_otp_data'] = {
                'email': email,
                'otp': otp,
                'expires_at': expires_at,
            }
            send_otp_email(
                email=email,
                otp=otp,
                recipient_name=user.username,
                account_role='Admin',
                reset_url=_absolute_reset_url(request, is_admin=True),
                valid_minutes=10,
            )
            messages.success(request, 'OTP sent to your email address.')
            return redirect('admin_reset_password')

    return _render_admin_public(request, 'forgot_password.html')


def reset_password_view(request):
    if request.method == 'POST':
        email = request.POST.get('email', '').strip()
        otp, otp_error = _validate_otp(request.POST.get('otp', '').strip())
        new_password = request.POST.get('new_password', '')
        confirm_password = request.POST.get('confirm_password', '')
        new_password_error = _validate_password(new_password, 'New password')

        otp_data = request.session.get('reset_otp_data')
        if not otp_data:
            messages.error(request, 'No OTP request found. Please request a new OTP.')
            return redirect('forgot_password')

        try:
            expires_at = timezone.datetime.fromisoformat(otp_data.get('expires_at', ''))
        except ValueError:
            expires_at = timezone.now() - timedelta(seconds=1)

        if timezone.now() > expires_at:
            request.session.pop('reset_otp_data', None)
            messages.error(request, 'OTP has expired. Please request a new OTP.')
            return redirect('forgot_password')

        if not _is_valid_email(email):
            messages.error(request, 'Please enter a valid email address.')
        elif otp_error:
            messages.error(request, otp_error)
        elif new_password_error:
            messages.error(request, new_password_error)
        elif email.lower() != otp_data.get('email', '').lower() or otp != otp_data.get('otp'):
            messages.error(request, 'Invalid email or OTP.')
        elif new_password != confirm_password:
            messages.error(request, 'Passwords do not match.')
        else:
            user = User.objects.filter(email=email).first()
            if not user:
                messages.error(request, 'User not found.')
            else:
                user.set_password(new_password)
                user.save()
                request.session.pop('reset_otp_data', None)
                messages.success(request, 'Password reset successful. Please login.')
                return redirect('login')

    return _render(request, 'reset_password.html')


def admin_reset_password_view(request):
    if request.method == 'POST':
        email = request.POST.get('email', '').strip()
        otp, otp_error = _validate_otp(request.POST.get('otp', '').strip())
        new_password = request.POST.get('new_password', '')
        confirm_password = request.POST.get('confirm_password', '')
        new_password_error = _validate_password(new_password, 'New password')

        otp_data = request.session.get('admin_reset_otp_data')
        if not otp_data:
            messages.error(request, 'No OTP request found. Please request a new OTP.')
            return redirect('admin_forgot_password')

        try:
            expires_at = timezone.datetime.fromisoformat(otp_data.get('expires_at', ''))
        except ValueError:
            expires_at = timezone.now() - timedelta(seconds=1)

        if timezone.now() > expires_at:
            request.session.pop('admin_reset_otp_data', None)
            messages.error(request, 'OTP has expired. Please request a new OTP.')
            return redirect('admin_forgot_password')

        if not _is_valid_email(email):
            messages.error(request, 'Please enter a valid email address.')
        elif otp_error:
            messages.error(request, otp_error)
        elif new_password_error:
            messages.error(request, new_password_error)
        elif email.lower() != otp_data.get('email', '').lower() or otp != otp_data.get('otp'):
            messages.error(request, 'Invalid email or OTP.')
        elif new_password != confirm_password:
            messages.error(request, 'Passwords do not match.')
        else:
            user = User.objects.filter(email=email, is_superuser=True).first()
            if not user:
                messages.error(request, 'Admin user not found.')
            else:
                user.set_password(new_password)
                user.save()
                request.session.pop('admin_reset_otp_data', None)
                messages.success(request, 'Password reset successful. Please login.')
                return redirect('admin_login')

    return _render_admin_public(request, 'reset_password.html')


@login_required
def add_income(request):
    blocked = _block_admin_from_user_modules(request)
    if blocked:
        return blocked

    income = Income.objects.filter(user=request.user).first()

    if request.method == 'POST':
        monthly_salary, salary_error = _validate_integer_field(
            request.POST.get('monthly_salary'),
            'Monthly salary',
            min_value=0,
        )
        other_income, other_income_error = _validate_integer_field(
            request.POST.get('other_income'),
            'Other income',
            min_value=0,
        )
        if salary_error or other_income_error:
            if salary_error:
                messages.error(request, salary_error)
            if other_income_error:
                messages.error(request, other_income_error)
            context = {'income': income, 'is_edit': bool(income)}
            return _render(request, 'add_income.html', context)

        if income:
            income.monthly_salary = monthly_salary
            income.other_income = other_income
            income.save()
            messages.success(request, 'Income updated successfully.')
        else:
            Income.objects.create(
                user=request.user,
                monthly_salary=monthly_salary,
                other_income=other_income,
            )
            messages.success(request, 'Income added successfully.')

        return redirect('dashboard')

    context = {'income': income, 'is_edit': bool(income)}
    return _render(request, 'add_income.html', context)


@login_required
def edit_income(request):
    blocked = _block_admin_from_user_modules(request)
    if blocked:
        return blocked

    income = Income.objects.filter(user=request.user).first()
    if not income:
        messages.info(request, 'Please add income first.')
        return redirect('add_income')

    if request.method == 'POST':
        monthly_salary, salary_error = _validate_integer_field(
            request.POST.get('monthly_salary'),
            'Monthly salary',
            min_value=0,
        )
        other_income, other_income_error = _validate_integer_field(
            request.POST.get('other_income'),
            'Other income',
            min_value=0,
        )
        if salary_error or other_income_error:
            if salary_error:
                messages.error(request, salary_error)
            if other_income_error:
                messages.error(request, other_income_error)
            context = {'income': income, 'is_edit': True}
            return _render(request, 'add_income.html', context)

        income.monthly_salary = monthly_salary
        income.other_income = other_income
        income.save()
        messages.success(request, 'Income updated successfully.')
        return redirect('dashboard')

    context = {'income': income, 'is_edit': True}
    return _render(request, 'add_income.html', context)


@login_required
def add_loan(request):
    blocked = _block_admin_from_user_modules(request)
    if blocked:
        return blocked

    settings_obj = _get_system_settings()
    start_date_min, start_date_max = _loan_start_window()
    income_total = _income_total_for_user(request.user)
    other_loans_emi = _other_loans_emi_total(request.user)
    form_values = _default_loan_form_values()
    if request.method == 'POST':
        cleaned, form_values, errors = _validate_loan_form_submission(request)
        if errors:
            for error in errors:
                messages.error(request, error)
            return _render(
                request,
                'add_loan.html',
                {
                    'loan': None,
                    'form_values': form_values,
                    'is_edit': False,
                    'income_total': income_total,
                    'other_loans_emi': other_loans_emi,
                    'green_limit': settings_obj.emi_green_limit,
                    'yellow_limit': settings_obj.emi_yellow_limit,
                    'start_date_min': start_date_min.isoformat(),
                    'start_date_max': start_date_max.isoformat(),
                },
            )

        Loan.objects.create(
            user=request.user,
            loan_type=cleaned['loan_type'],
            lender=cleaned['lender'],
            principal=cleaned['principal'],
            monthly_emi=cleaned['monthly_emi'],
            interest_rate=cleaned['interest_rate'],
            start_date=cleaned['start_date'],
            end_date=cleaned['end_date'],
        )
        auto_notes = []
        if cleaned['lender']:
            auto_notes.append(f"lender: {cleaned['lender']}")
        if cleaned['start_auto_calculated']:
            auto_notes.append(f"start date set to {cleaned['start_date'].isoformat()}")
        if cleaned['end_auto_calculated']:
            auto_notes.append(f"end date set to {cleaned['end_date'].isoformat()}")
        if cleaned['emi_auto_calculated']:
            auto_notes.append(f"EMI auto-calculated as Rs. {cleaned['monthly_emi']}")
        if cleaned['rate_auto_calculated']:
            auto_notes.append(f"interest auto-calculated as {cleaned['interest_rate']:.2f}% yearly")
        if cleaned['months_paid_auto_calculated']:
            auto_notes.append(
                f"EMIs already paid auto-set to {cleaned['months_paid']} using current date"
            )
        if income_total > 0:
            loan_share = (cleaned['monthly_emi'] / income_total) * 100
            projected_ratio = ((other_loans_emi + cleaned['monthly_emi']) / income_total) * 100
            auto_notes.append(f"this EMI is {loan_share:.1f}% of monthly income")
            auto_notes.append(f"projected total EMI ratio is {projected_ratio:.1f}%")
        else:
            auto_notes.append('add income details to track EMI percentage')
        if auto_notes:
            messages.success(request, f"Loan added successfully. {'; '.join(auto_notes)}.")
        else:
            messages.success(request, 'Loan added successfully.')
        return redirect('loan_list')

    return _render(
        request,
        'add_loan.html',
        {
            'loan': None,
            'form_values': form_values,
            'is_edit': False,
            'income_total': income_total,
            'other_loans_emi': other_loans_emi,
            'green_limit': settings_obj.emi_green_limit,
            'yellow_limit': settings_obj.emi_yellow_limit,
            'start_date_min': start_date_min.isoformat(),
            'start_date_max': start_date_max.isoformat(),
        },
    )


@login_required
def edit_loan(request, loan_id):
    blocked = _block_admin_from_user_modules(request)
    if blocked:
        return blocked

    loan = get_object_or_404(Loan, id=loan_id, user=request.user)
    settings_obj = _get_system_settings()
    base_start_date_min, start_date_max = _loan_start_window()
    start_date_min = min(base_start_date_min, loan.start_date)
    income_total = _income_total_for_user(request.user)
    other_loans_emi = _other_loans_emi_total(request.user, exclude_loan_id=loan.id)

    if request.method == 'POST':
        cleaned, form_values, errors = _validate_loan_form_submission(request)
        if errors:
            for error in errors:
                messages.error(request, error)
            return _render(
                request,
                'add_loan.html',
                {
                    'loan': loan,
                    'is_edit': True,
                    'form_values': form_values,
                    'income_total': income_total,
                    'other_loans_emi': other_loans_emi,
                    'green_limit': settings_obj.emi_green_limit,
                    'yellow_limit': settings_obj.emi_yellow_limit,
                    'start_date_min': start_date_min.isoformat(),
                    'start_date_max': start_date_max.isoformat(),
                },
            )

        loan.loan_type = cleaned['loan_type']
        loan.lender = cleaned['lender']
        loan.principal = cleaned['principal']
        loan.monthly_emi = cleaned['monthly_emi']
        loan.interest_rate = cleaned['interest_rate']
        loan.start_date = cleaned['start_date']
        loan.end_date = cleaned['end_date']
        loan.save()
        auto_notes = []
        if cleaned['lender']:
            auto_notes.append(f"lender: {cleaned['lender']}")
        if cleaned['start_auto_calculated']:
            auto_notes.append(f"start date set to {cleaned['start_date'].isoformat()}")
        if cleaned['end_auto_calculated']:
            auto_notes.append(f"end date set to {cleaned['end_date'].isoformat()}")
        if cleaned['emi_auto_calculated']:
            auto_notes.append(f"EMI auto-calculated as Rs. {cleaned['monthly_emi']}")
        if cleaned['rate_auto_calculated']:
            auto_notes.append(f"interest auto-calculated as {cleaned['interest_rate']:.2f}% yearly")
        if cleaned['months_paid_auto_calculated']:
            auto_notes.append(
                f"EMIs already paid auto-set to {cleaned['months_paid']} using current date"
            )
        if income_total > 0:
            loan_share = (cleaned['monthly_emi'] / income_total) * 100
            projected_ratio = ((other_loans_emi + cleaned['monthly_emi']) / income_total) * 100
            auto_notes.append(f"this EMI is {loan_share:.1f}% of monthly income")
            auto_notes.append(f"projected total EMI ratio is {projected_ratio:.1f}%")
        else:
            auto_notes.append('add income details to track EMI percentage')
        if auto_notes:
            messages.success(request, f"Loan updated successfully. {'; '.join(auto_notes)}.")
        else:
            messages.success(request, 'Loan updated successfully.')
        return redirect('loan_list')

    context = {
        'loan': loan,
        'is_edit': True,
        'form_values': _default_loan_form_values(loan),
        'income_total': income_total,
        'other_loans_emi': other_loans_emi,
        'green_limit': settings_obj.emi_green_limit,
        'yellow_limit': settings_obj.emi_yellow_limit,
        'start_date_min': start_date_min.isoformat(),
        'start_date_max': start_date_max.isoformat(),
    }
    return _render(request, 'add_loan.html', context)


@login_required
def loan_list(request):
    blocked = _block_admin_from_user_modules(request)
    if blocked:
        return blocked

    loans = Loan.objects.filter(user=request.user).order_by('-start_date')
    return _render(request, 'loan_list.html', {'loans': loans})


@login_required
def delete_loan(request, loan_id):
    blocked = _block_admin_from_user_modules(request)
    if blocked:
        return blocked

    loan = get_object_or_404(Loan, id=loan_id, user=request.user)
    if request.method == 'POST':
        loan.delete()
        messages.success(request, 'Loan deleted successfully.')
    return redirect('loan_list')


@login_required
def credit_cards_view(request):
    blocked = _block_admin_from_user_modules(request)
    if blocked:
        return blocked

    if request.method == 'POST':
        action = request.POST.get('action', '').strip().lower()
        if action == 'delete_card':
            card_id = _to_int(request.POST.get('card_id'), default=0)
            card = get_object_or_404(CreditCardAccount, id=card_id, user=request.user)
            card.delete()
            messages.success(request, 'Credit card removed successfully.')
            return redirect('credit_cards')
        else:
            messages.error(request, 'Unsupported action.')

    cc_snapshot = _credit_card_snapshot(request.user)
    context = {
        'cards': cc_snapshot['cards'],
        'card_rows': cc_snapshot['per_card_rows'],
        'current_month_label': cc_snapshot['current_statement_month'].strftime('%b %Y'),
    }
    return _render(request, 'credit_cards.html', context)


def _credit_card_form_defaults():
    return {
        'card_name': '',
        'issuer': '',
        'credit_limit': '',
        'emi_interest_rate': '',
        'monthly_spend_interest_rate': '0',
        'reward_percent': '0',
    }


def _credit_card_form_from_instance(card):
    return {
        'card_name': card.card_name,
        'issuer': card.issuer,
        'credit_limit': str(card.credit_limit),
        'emi_interest_rate': f'{card.emi_interest_rate:.2f}',
        'monthly_spend_interest_rate': f'{card.monthly_spend_interest_rate:.2f}',
        'reward_percent': f'{card.reward_percent:.2f}',
    }


def _validate_credit_card_form_payload(payload):
    errors = []
    card_name = payload['card_name']
    issuer = payload['issuer']
    if not card_name:
        errors.append('Card name is required.')
    elif len(card_name) > 120:
        errors.append('Card name is too long.')
    if len(issuer) > 120:
        errors.append('Issuer name is too long.')

    credit_limit, credit_limit_error = _validate_integer_field(
        payload['credit_limit'],
        'Card total limit',
        min_value=1,
        max_value=1_000_000_000,
    )
    if credit_limit_error:
        errors.append(credit_limit_error)

    emi_interest_rate, emi_rate_error = _validate_float_field(
        payload['emi_interest_rate'],
        'EMI interest rate',
        min_value=0.0,
        max_value=100.0,
    )
    if emi_rate_error:
        errors.append(emi_rate_error)

    monthly_spend_interest_rate, spend_rate_error = _validate_float_field(
        payload['monthly_spend_interest_rate'],
        'Monthly spend interest rate',
        min_value=0.0,
        max_value=100.0,
    )
    if spend_rate_error:
        errors.append(spend_rate_error)

    reward_percent, reward_error = _validate_float_field(
        payload['reward_percent'],
        'Reward percent',
        min_value=0.0,
        max_value=100.0,
    )
    if reward_error:
        errors.append(reward_error)

    return {
        'card_name': card_name,
        'issuer': issuer,
        'credit_limit': credit_limit,
        'emi_interest_rate': emi_interest_rate,
        'monthly_spend_interest_rate': monthly_spend_interest_rate,
        'reward_percent': reward_percent,
        'errors': errors,
    }


@login_required
def credit_card_add_view(request):
    blocked = _block_admin_from_user_modules(request)
    if blocked:
        return blocked

    card_form = _credit_card_form_defaults()
    if request.method == 'POST':
        card_form = {
            'card_name': request.POST.get('card_name', '').strip(),
            'issuer': request.POST.get('issuer', '').strip(),
            'credit_limit': request.POST.get('credit_limit', '').strip(),
            'emi_interest_rate': request.POST.get('emi_interest_rate', '').strip(),
            'monthly_spend_interest_rate': request.POST.get('monthly_spend_interest_rate', '').strip(),
            'reward_percent': request.POST.get('reward_percent', '').strip(),
        }
        cleaned = _validate_credit_card_form_payload(card_form)
        if cleaned['errors']:
            for error in cleaned['errors']:
                messages.error(request, error)
        else:
            card = CreditCardAccount.objects.create(
                user=request.user,
                card_name=cleaned['card_name'],
                issuer=cleaned['issuer'],
                credit_limit=cleaned['credit_limit'],
                emi_interest_rate=cleaned['emi_interest_rate'],
                monthly_spend_interest_rate=cleaned['monthly_spend_interest_rate'],
                reward_percent=cleaned['reward_percent'],
            )
            messages.success(request, 'Credit card saved successfully.')
            return redirect('credit_card_spend', card_id=card.id)

    return _render(
        request,
        'credit_card_form.html',
        {
            'card_form': card_form,
            'is_card_edit': False,
        },
    )


@login_required
def credit_card_edit_view(request, card_id):
    blocked = _block_admin_from_user_modules(request)
    if blocked:
        return blocked

    card = get_object_or_404(CreditCardAccount, id=card_id, user=request.user)
    card_form = _credit_card_form_from_instance(card)
    if request.method == 'POST':
        card_form = {
            'card_name': request.POST.get('card_name', '').strip(),
            'issuer': request.POST.get('issuer', '').strip(),
            'credit_limit': request.POST.get('credit_limit', '').strip(),
            'emi_interest_rate': request.POST.get('emi_interest_rate', '').strip(),
            'monthly_spend_interest_rate': request.POST.get('monthly_spend_interest_rate', '').strip(),
            'reward_percent': request.POST.get('reward_percent', '').strip(),
        }
        cleaned = _validate_credit_card_form_payload(card_form)
        if cleaned['errors']:
            for error in cleaned['errors']:
                messages.error(request, error)
        else:
            card.card_name = cleaned['card_name']
            card.issuer = cleaned['issuer']
            card.credit_limit = cleaned['credit_limit']
            card.emi_interest_rate = cleaned['emi_interest_rate']
            card.monthly_spend_interest_rate = cleaned['monthly_spend_interest_rate']
            card.reward_percent = cleaned['reward_percent']
            card.save()
            messages.success(request, 'Credit card updated successfully.')
            return redirect('credit_cards')

    return _render(
        request,
        'credit_card_form.html',
        {
            'card_form': card_form,
            'is_card_edit': True,
            'card': card,
        },
    )


@login_required
def credit_card_spend_view(request, card_id):
    blocked = _block_admin_from_user_modules(request)
    if blocked:
        return blocked

    selected_card = get_object_or_404(CreditCardAccount, id=card_id, user=request.user)
    current_month = timezone.localdate().replace(day=1)
    edit_entry_id = _to_int(request.GET.get('edit_entry'), default=0)

    entry_form = {
        'entry_id': '',
        'entry_type': CreditCardEntry.TYPE_MONTHLY_SPEND,
        'entry_month': current_month.strftime('%Y-%m'),
        'amount': '',
        'tenure_months': '',
        'description': '',
    }

    if request.method == 'POST':
        action = request.POST.get('action', '').strip().lower()

        if action == 'delete_entry':
            entry_id = _to_int(request.POST.get('entry_id'), default=0)
            entry = get_object_or_404(
                CreditCardEntry,
                id=entry_id,
                card_id=selected_card.id,
                card__user=request.user,
            )
            entry.delete()
            messages.success(request, 'Entry deleted successfully.')
            return redirect('credit_card_spend', card_id=selected_card.id)

        if action in {'add_spend', 'save_spend', 'save_entry'}:
            entry_form = {
                'entry_id': request.POST.get('entry_id', '').strip(),
                'entry_type': request.POST.get('entry_type', CreditCardEntry.TYPE_MONTHLY_SPEND).strip(),
                'entry_month': request.POST.get('entry_month', '').strip(),
                'amount': request.POST.get('amount', '').strip(),
                'tenure_months': request.POST.get('tenure_months', '').strip(),
                'description': request.POST.get('description', '').strip(),
            }
            errors = []
            edit_target_id = _to_int(entry_form['entry_id'], default=0)
            edit_entry = None
            if edit_target_id:
                edit_entry = CreditCardEntry.objects.filter(
                    id=edit_target_id,
                    card_id=selected_card.id,
                    card__user=request.user,
                ).first()
                if not edit_entry:
                    errors.append('Entry not found for update.')

            entry_type = entry_form['entry_type']
            if entry_type not in {
                CreditCardEntry.TYPE_MONTHLY_SPEND,
                CreditCardEntry.TYPE_EMI,
            }:
                errors.append('Entry type is invalid.')

            entry_month, entry_month_error = _parse_statement_month(entry_form['entry_month'])
            if entry_month_error:
                errors.append(entry_month_error)

            amount, amount_error = _validate_integer_field(
                entry_form['amount'],
                'Spend amount',
                min_value=1,
            )
            if amount_error:
                errors.append(amount_error)

            tenure_months = 1
            if entry_type == CreditCardEntry.TYPE_EMI:
                tenure_months, tenure_error = _validate_integer_field(
                    entry_form['tenure_months'],
                    'EMI tenure (months)',
                    min_value=1,
                    max_value=240,
                )
                if tenure_error:
                    errors.append(tenure_error)

            description = entry_form['description']
            if len(description) > 200:
                errors.append('Description must be 200 characters or less.')

            if errors:
                for error in errors:
                    messages.error(request, error)
            else:
                success_label = 'EMI entry' if entry_type == CreditCardEntry.TYPE_EMI else 'Monthly spend entry'
                if edit_entry:
                    edit_entry.entry_month = entry_month
                    edit_entry.amount = amount
                    edit_entry.description = description
                    edit_entry.entry_type = entry_type
                    edit_entry.tenure_months = tenure_months if entry_type == CreditCardEntry.TYPE_EMI else 1
                    edit_entry.save()
                    messages.success(request, f'{success_label} updated successfully.')
                else:
                    CreditCardEntry.objects.create(
                        card=selected_card,
                        entry_month=entry_month,
                        entry_type=entry_type,
                        amount=amount,
                        tenure_months=tenure_months if entry_type == CreditCardEntry.TYPE_EMI else 1,
                        description=description,
                    )
                    messages.success(request, f'{success_label} saved successfully.')
                return redirect('credit_card_spend', card_id=selected_card.id)

        else:
            messages.error(request, 'Unsupported action.')

    if request.method == 'GET' and edit_entry_id:
        editing_entry = CreditCardEntry.objects.filter(
            id=edit_entry_id,
            card_id=selected_card.id,
            card__user=request.user,
        ).first()
        if editing_entry:
            entry_form = {
                'entry_id': str(editing_entry.id),
                'entry_type': editing_entry.entry_type,
                'entry_month': editing_entry.entry_month.strftime('%Y-%m'),
                'amount': str(editing_entry.amount),
                'tenure_months': (
                    str(editing_entry.tenure_months)
                    if editing_entry.entry_type == CreditCardEntry.TYPE_EMI
                    else ''
                ),
                'description': editing_entry.description,
            }
        else:
            messages.warning(request, 'Requested entry was not found.')

    cc_snapshot = _credit_card_snapshot(request.user)
    card_row_lookup = {row['card'].id: row for row in cc_snapshot['per_card_rows']}
    selected_card_row = card_row_lookup.get(
        selected_card.id,
        {
            'card': selected_card,
            'emi_monthly_due': 0.0,
            'emi_remaining_balance': 0.0,
            'monthly_spend_amount': 0.0,
            'total_amount': 0.0,
            'interest_estimate': 0.0,
            'reward_estimate': 0.0,
            'entry_count': 0,
            'spend_entry_count': 0,
            'emi_entry_count': 0,
            'closed_emi_entry_count': 0,
            'upcoming_emi_entry_count': 0,
            'emi_share_percent': 0.0,
            'spend_share_percent': 0.0,
            'net_cost': 0.0,
            'credit_limit': max(0, selected_card.credit_limit or 0),
            'available_limit': max(0, selected_card.credit_limit or 0),
            'utilization_percent': 0.0,
        },
    )

    def _entry_display_row(entry):
        entry_month = _month_start_value(entry.entry_month)
        entry_type = entry.entry_type
        if entry_type == CreditCardEntry.TYPE_EMI:
            tenure_months = max(1, int(entry.tenure_months or 1))
            elapsed_months = _month_gap(entry_month, current_month)
            monthly_due = _card_emi_monthly_due(
                principal=entry.amount,
                annual_rate_percent=entry.card.emi_interest_rate,
                tenure_months=tenure_months,
            )
            if elapsed_months < 0:
                status = 'Upcoming'
                remaining_months = tenure_months
                remaining_balance = float(entry.amount)
                interest_estimate = 0.0
            elif elapsed_months >= tenure_months:
                status = 'Completed'
                remaining_months = 0
                remaining_balance = 0.0
                interest_estimate = 0.0
            else:
                status = 'Active'
                remaining_months = tenure_months - elapsed_months
                remaining_balance = _card_emi_remaining_balance(
                    principal=entry.amount,
                    annual_rate_percent=entry.card.emi_interest_rate,
                    tenure_months=tenure_months,
                    months_paid=elapsed_months,
                )
                interest_estimate = round(remaining_balance * (entry.card.emi_interest_rate / 1200.0), 2)
            reward_estimate = 0.0
        else:
            monthly_due = entry.amount
            remaining_months = 0
            remaining_balance = float(entry.amount) if entry_month == current_month else 0.0
            interest_estimate = 0.0
            reward_estimate = round(entry.amount * (entry.card.reward_percent / 100.0), 2)
            status = 'Current Month' if entry_month == current_month else 'Settled'
            tenure_months = 1

        return {
            'entry': entry,
            'entry_type_label': 'EMI' if entry_type == CreditCardEntry.TYPE_EMI else 'Monthly Spend',
            'monthly_due': round(float(monthly_due), 2),
            'remaining_months': remaining_months,
            'remaining_balance': round(float(remaining_balance), 2),
            'interest_estimate': round(float(interest_estimate), 2),
            'reward_estimate': round(float(reward_estimate), 2),
            'status': status,
            'tenure_months': tenure_months,
        }

    selected_entries = list(
        CreditCardEntry.objects.filter(
            card_id=selected_card.id,
            card__user=request.user,
        )
        .select_related('card')
        .order_by('-entry_month', '-id')
    )
    all_entries = list(
        CreditCardEntry.objects.filter(card__user=request.user)
        .select_related('card')
        .order_by('-entry_month', '-id')
    )

    context = {
        'cards': cc_snapshot['cards'],
        'card_rows': cc_snapshot['per_card_rows'],
        'selected_card': selected_card,
        'selected_card_row': selected_card_row,
        'current_month_label': current_month.strftime('%b %Y'),
        'entry_form': entry_form,
        'is_entry_edit': bool(entry_form['entry_id']),
        'entries': [_entry_display_row(entry) for entry in selected_entries],
        'all_entries': [_entry_display_row(entry) for entry in all_entries],
    }
    return _render(request, 'credit_card_spend.html', context)


@login_required
def budget_view(request):
    blocked = _block_admin_from_user_modules(request)
    if blocked:
        return blocked

    budget, _ = Budget.objects.get_or_create(user=request.user)

    if request.method == 'POST':
        grocery, grocery_error = _validate_integer_field(
            request.POST.get('grocery'),
            'Grocery',
            min_value=0,
        )
        rent, rent_error = _validate_integer_field(
            request.POST.get('rent'),
            'Rent',
            min_value=0,
        )
        transport, transport_error = _validate_integer_field(
            request.POST.get('transport'),
            'Transport',
            min_value=0,
        )
        entertainment, entertainment_error = _validate_integer_field(
            request.POST.get('entertainment'),
            'Entertainment',
            min_value=0,
        )
        if grocery_error or rent_error or transport_error or entertainment_error:
            for err in [grocery_error, rent_error, transport_error, entertainment_error]:
                if err:
                    messages.error(request, err)
            snapshot = _financial_snapshot(request.user)
            remaining_after_obligations = snapshot.get('remaining_after_obligations', snapshot['remaining_after_emi'])
            savings_after_obligations = remaining_after_obligations - budget.total_expense
            context = {
                'budget': budget,
                'total_expenses': budget.total_expense,
                'remaining_after_emi': remaining_after_obligations,
                'remaining_after_obligations': remaining_after_obligations,
                'savings_after_emi': savings_after_obligations,
                'savings_after_obligations': savings_after_obligations,
                'overspending': budget.total_expense > remaining_after_obligations,
                'emi_manageable': snapshot['overall_burden_ratio'] <= snapshot['yellow_limit'],
                'emi_ratio': snapshot['overall_burden_ratio'],
                'card_due_estimate': snapshot.get('credit_card_due_estimate', 0),
                'suggestions': [],
                'expense_categories': [
                    ('Rent', budget.rent),
                    ('Grocery', budget.grocery),
                    ('Transport', budget.transport),
                    ('Entertainment', budget.entertainment),
                ],
            }
            return _render(request, 'budget.html', context)

        budget.grocery = grocery
        budget.rent = rent
        budget.transport = transport
        budget.entertainment = entertainment
        budget.save()
        messages.success(request, 'Budget saved successfully.')
        return redirect('budget')

    snapshot = _financial_snapshot(request.user)
    total_expenses = budget.total_expense
    remaining_after_obligations = snapshot.get('remaining_after_obligations', snapshot['remaining_after_emi'])
    savings_after_emi = remaining_after_obligations - total_expenses
    overspending = total_expenses > remaining_after_obligations

    suggestions = []
    if overspending:
        suggestions.append(
            f"Overspending detected by Rs. {abs(savings_after_emi):,.0f}. Start reducing top categories."
        )
        category_values = {
            'Rent': budget.rent,
            'Grocery': budget.grocery,
            'Transport': budget.transport,
            'Entertainment': budget.entertainment,
        }
        sorted_categories = sorted(category_values.items(), key=lambda item: item[1], reverse=True)
        for category, amount in sorted_categories:
            if amount > 0:
                suggestions.append(
                    f"Reduce {category} by about Rs. {max(1, int(amount * 0.15))} this month."
                )
    else:
        suggestions.append('Spending is controlled after debt obligations.')
        if savings_after_emi < snapshot['savings_target']:
            gap = snapshot['savings_target'] - savings_after_emi
            suggestions.append(
                f"Increase savings by Rs. {gap:,.0f} to hit your "
                f"{snapshot['savings_target_percent']:.0f}% monthly target."
            )

    context = {
        'budget': budget,
        'total_expenses': total_expenses,
        'remaining_after_emi': remaining_after_obligations,
        'remaining_after_obligations': remaining_after_obligations,
        'savings_after_emi': savings_after_emi,
        'savings_after_obligations': savings_after_emi,
        'overspending': overspending,
        'emi_manageable': snapshot['overall_burden_ratio'] <= snapshot['yellow_limit'],
        'emi_ratio': snapshot['overall_burden_ratio'],
        'card_due_estimate': snapshot.get('credit_card_due_estimate', 0),
        'suggestions': suggestions,
        'expense_categories': [
            ('Rent', budget.rent),
            ('Grocery', budget.grocery),
            ('Transport', budget.transport),
            ('Entertainment', budget.entertainment),
        ],
    }
    return _render(request, 'budget.html', context)


@login_required
def dashboard(request):
    if request.user.is_superuser:
        settings_obj = _get_system_settings()
        users_qs = _admin_user_queryset()
        user_rows = _admin_user_rows(users_qs, settings_obj=settings_obj)
        safe_count, risky_count, danger_count = _zone_counts(user_rows)
        signup_labels, signup_values = _monthly_signup_trend(users_qs, months=6)
        high_interest_user_count = sum(1 for row in user_rows if row['high_interest_count'] > 0)
        active_user_count = sum(1 for row in user_rows if row['is_active'])
        total_loans = Loan.objects.filter(user__is_superuser=False).count()
        average_emi_ratio = (
            round(sum(row['emi_ratio'] for row in user_rows) / len(user_rows), 2) if user_rows else 0
        )
        today = timezone.localdate()

        context = {
            'total_users': len(user_rows),
            'active_users': active_user_count,
            'inactive_users': len(user_rows) - active_user_count,
            'total_loans': total_loans,
            'average_emi_ratio': average_emi_ratio,
            'new_users_30d': users_qs.filter(date_joined__date__gte=today - timedelta(days=30)).count(),
            'safe_count': safe_count,
            'risky_count': risky_count,
            'danger_count': danger_count,
            'high_interest_user_count': high_interest_user_count,
            'recent_user_rows': user_rows[:8],
            'chart_payload': _build_admin_chart_payload(user_rows, signup_labels, signup_values),
        }
        return _render(request, 'dashboard.html', context)

    snapshot = _financial_snapshot(request.user)
    context = {
        **snapshot,
        'chart_payload': _build_chart_payload(snapshot),
    }
    return _render(request, 'dashboard.html', context)


@login_required
def charts_view(request):
    if request.user.is_superuser:
        messages.info(request, 'Use the admin analytics module for platform-level charts.')
        return redirect('admin_charts')

    snapshot = _financial_snapshot(request.user)
    context = {
        **snapshot,
        'chart_payload': _build_chart_payload(snapshot),
    }
    return _render(request, 'charts.html', context)


@login_required
def monthly_payments_view(request):
    blocked = _block_admin_from_user_modules(request)
    if blocked:
        return blocked

    snapshot = _financial_snapshot(request.user)
    current_month = timezone.localdate().replace(day=1)
    month_label = current_month.strftime('%B %Y')

    payment_rows = []

    for loan in snapshot.get('active_loans', []):
        payment_rows.append(
            {
                'category': 'Loan EMI',
                'source': loan.loan_type,
                'lender': loan.lender or '-',
                'amount': float(loan.monthly_emi),
                'status': 'Due this month',
                'note': f'Ends {loan.end_date.strftime("%d %b %Y")}',
            }
        )

    for card_row in snapshot.get('credit_card_card_rows', []):
        card_name = card_row['card'].card_name
        card_issuer = (card_row['card'].issuer or '').strip() or '-'
        card_emi_due = float(card_row.get('emi_monthly_due', 0) or 0)
        card_spend_due = float(card_row.get('monthly_spend_amount', 0) or 0)

        if card_emi_due > 0:
            payment_rows.append(
                {
                    'category': 'Card EMI',
                    'source': card_name,
                    'lender': card_issuer,
                    'amount': card_emi_due,
                    'status': 'Due this month',
                    'note': (
                        f"Remaining EMI balance: Rs. "
                        f"{card_row.get('emi_remaining_balance', 0):,.0f}"
                    ),
                }
            )

        if card_spend_due > 0:
            payment_rows.append(
                {
                    'category': 'Card Spend',
                    'source': card_name,
                    'lender': card_issuer,
                    'amount': card_spend_due,
                    'status': f'Statement month: {snapshot.get("credit_card_current_month_label", current_month.strftime("%b %Y"))}',
                    'note': 'Current month statement spend.',
                }
            )

    payment_rows.sort(key=lambda row: row['amount'], reverse=True)
    total_due = round(sum(row['amount'] for row in payment_rows), 2)

    context = {
        'month_label': month_label,
        'payment_rows': payment_rows,
        'payment_count': len(payment_rows),
        'loan_due_total': snapshot.get('total_emi', 0),
        'card_due_total': snapshot.get('credit_card_due_estimate', 0),
        'total_due': total_due,
        'net_after_due': snapshot.get('remaining_after_obligations', snapshot.get('remaining_after_emi', 0)),
    }
    return _render(request, 'monthly_payments.html', context)


@admin_required
def admin_user_management(request):
    settings_obj = _get_system_settings()
    query = request.GET.get('q', '').strip()

    users_qs = _admin_user_queryset()
    if query:
        users_qs = users_qs.filter(Q(username__icontains=query) | Q(email__icontains=query))

    user_rows = _admin_user_rows(users_qs, settings_obj=settings_obj)
    active_count = sum(1 for row in user_rows if row['is_active'])

    context = {
        'query': query,
        'user_rows': user_rows,
        'total_users': len(user_rows),
        'active_users': active_count,
        'inactive_users': len(user_rows) - active_count,
    }
    return _render(request, 'users.html', context)


@admin_required
def admin_user_detail(request, user_id):
    target_user = get_object_or_404(User, id=user_id, is_superuser=False)
    target_profile = _get_or_create_profile(target_user)
    settings_obj = _get_system_settings()
    snapshot = _financial_snapshot(target_user, settings_obj=settings_obj)
    risk = _risk_profile(snapshot)

    income_obj = snapshot['income_obj']
    budget_obj = snapshot['budget_obj']
    loans = sorted(
        snapshot.get('active_loans', []) + snapshot.get('upcoming_loans', []),
        key=lambda loan: loan.end_date,
    )
    budget_total = budget_obj.total_expense if budget_obj else 0
    remaining_after_obligations = snapshot.get('remaining_after_obligations', snapshot['remaining_after_emi'])
    savings_after_emi = remaining_after_obligations - budget_total
    overspending = budget_total > remaining_after_obligations

    context = {
        'target_user': target_user,
        'target_phone_number': target_profile.phone_number or 'Not set',
        'income_obj': income_obj,
        'budget_obj': budget_obj,
        'loans': loans,
        'total_income': snapshot['total_income'],
        'total_emi': snapshot['total_emi'],
        'card_due_estimate': snapshot['credit_card_due_estimate'],
        'total_monthly_obligation': snapshot['total_monthly_obligation'],
        'emi_ratio': snapshot['emi_ratio'],
        'overall_burden_ratio': snapshot['overall_burden_ratio'],
        'health_zone': snapshot['health_zone'],
        'health_class': snapshot['health_class'],
        'smart_suggestion': snapshot['smart_suggestion'],
        'debt_free_text': snapshot['debt_free_text'],
        'net_savings': snapshot['net_savings'],
        'net_savings_after_cards': snapshot['net_savings_after_cards'],
        'credit_card_total_outstanding': snapshot['credit_card_total_outstanding'],
        'credit_card_total_emi_remaining_balance': snapshot['credit_card_total_emi_remaining_balance'],
        'high_interest_loans': snapshot['high_interest_loans'],
        'member_since': target_user.date_joined,
        'last_login': target_user.last_login,
        'is_active': target_user.is_active,
        'loan_count': snapshot.get('active_loan_count', len(loans)),
        'high_interest_count': len(snapshot['high_interest_loans']),
        'risk_level': risk['level'],
        'risk_label': risk['label'],
        'risk_reasons': risk['reasons'],
        'budget_total': budget_total,
        'savings_after_emi': savings_after_emi,
        'overspending': overspending,
    }
    return _render(request, 'user_details.html', context)


@admin_required
def admin_toggle_user_active(request, user_id):
    if request.method != 'POST':
        return redirect('admin_users')

    target_user = get_object_or_404(User, id=user_id, is_superuser=False)
    target_user.is_active = not target_user.is_active
    target_user.save(update_fields=['is_active'])

    action = 'activated_user' if target_user.is_active else 'deactivated_user'
    _log_admin_action(
        request.user,
        action,
        target_user=target_user,
        details=f'Changed account status for {target_user.username}.',
    )
    messages.success(
        request,
        f'User {target_user.username} is now {"active" if target_user.is_active else "inactive"}.',
    )

    redirect_to = request.POST.get('next', '').strip() or 'admin_users'
    return redirect(redirect_to)


@admin_required
def admin_force_password_reset(request, user_id):
    if request.method != 'POST':
        return redirect('admin_user_details', user_id=user_id)

    target_user = get_object_or_404(User, id=user_id, is_superuser=False)
    temp_password = ''.join(random.choices(string.ascii_letters + string.digits, k=10))
    target_user.set_password(temp_password)
    target_user.save(update_fields=['password'])

    _log_admin_action(
        request.user,
        'force_password_reset',
        target_user=target_user,
        details=f'Issued temporary password for {target_user.username}.',
    )
    messages.success(
        request,
        f'Temporary password for {target_user.username}: {temp_password}',
    )
    return redirect('admin_user_details', user_id=user_id)


@admin_required
def admin_delete_user(request, user_id):
    if request.method != 'POST':
        return redirect('admin_users')

    target_user = get_object_or_404(User, id=user_id, is_superuser=False)
    username = target_user.username
    target_user.delete()

    _log_admin_action(
        request.user,
        'deleted_user',
        details=f'Deleted user account {username}.',
    )
    messages.success(request, f'User {username} deleted successfully.')
    return redirect('admin_users')


@admin_required
def admin_risk_monitor(request):
    settings_obj = _get_system_settings()
    allowed_modes = {'risky', 'danger', 'medium', 'low', 'all'}
    mode = (request.GET.get('mode') or request.POST.get('mode') or 'risky').strip().lower()
    if mode not in allowed_modes:
        mode = 'risky'

    query = request.GET.get('q', '').strip()
    users_qs = _admin_user_queryset()
    if query:
        users_qs = users_qs.filter(Q(username__icontains=query) | Q(email__icontains=query))
    user_rows = _admin_user_rows(users_qs, settings_obj=settings_obj)

    compose_target_group = ''
    compose_subject = ''
    compose_message = ''

    if request.method == 'POST':
        compose_target_group = request.POST.get('target_group', '').strip()
        compose_subject = request.POST.get('subject', '').strip()
        compose_message = request.POST.get('message', '').strip()
        allowed_groups = {'red', 'yellow', 'high_interest', 'high_risk', 'medium_risk', 'all'}
        validation_errors = []

        if not compose_target_group or not compose_subject or not compose_message:
            validation_errors.append('Target group, subject, and message are required.')
        if compose_target_group and compose_target_group not in allowed_groups:
            validation_errors.append('Invalid target group selected.')
        if compose_subject and (len(compose_subject) < 3 or len(compose_subject) > 150):
            validation_errors.append('Subject must be between 3 and 150 characters.')
        if compose_message and len(compose_message) < 10:
            validation_errors.append('Message must be at least 10 characters long.')

        if compose_target_group == 'red':
            target_rows = [row for row in user_rows if row['health_class'] == 'red']
        elif compose_target_group == 'yellow':
            target_rows = [row for row in user_rows if row['health_class'] == 'yellow']
        elif compose_target_group == 'high_interest':
            target_rows = [row for row in user_rows if row['high_interest_count'] > 0]
        elif compose_target_group == 'high_risk':
            target_rows = [row for row in user_rows if row['risk_level'] == 'high']
        elif compose_target_group == 'medium_risk':
            target_rows = [row for row in user_rows if row['risk_level'] == 'medium']
        else:
            target_rows = user_rows

        recipients = sorted(
            {
                row['user'].email.strip()
                for row in target_rows
                if row['user'].is_active and row['user'].email
            }
        )
        if not recipients:
            validation_errors.append('No active recipients found for the selected group.')

        if validation_errors:
            for err in validation_errors:
                messages.error(request, err)
        else:
            try:
                delivered_count = send_advisory_email(
                    recipients=recipients,
                    subject=compose_subject,
                    message_body=compose_message,
                    sent_by=request.user.username,
                )
                if delivered_count <= 0:
                    messages.error(request, 'No recipients were eligible for delivery.')
                    return redirect(f'{request.path}?mode={mode}')
                _log_admin_action(
                    request.user,
                    'bulk_email_sent',
                    details=(
                        f'Sent email to {delivered_count} recipients in group {compose_target_group}.'
                    ),
                )
                messages.success(request, f'Email sent to {delivered_count} active user(s).')
                return redirect(f'{request.path}?mode={mode}')
            except Exception as exc:
                messages.error(request, f'Email sending failed: {exc}')

    if mode == 'all':
        filtered_rows = user_rows
    elif mode == 'danger':
        filtered_rows = [row for row in user_rows if row['risk_level'] == 'high']
    elif mode == 'medium':
        filtered_rows = [row for row in user_rows if row['risk_level'] == 'medium']
    elif mode == 'low':
        filtered_rows = [row for row in user_rows if row['risk_level'] == 'low']
    else:
        filtered_rows = [row for row in user_rows if row['risk_level'] in {'high', 'medium'}]

    filtered_rows.sort(key=lambda row: row.get('overall_burden_ratio', row['emi_ratio']), reverse=True)
    high_risk_count = sum(1 for row in user_rows if row['risk_level'] == 'high')
    medium_risk_count = sum(1 for row in user_rows if row['risk_level'] == 'medium')
    low_risk_count = sum(1 for row in user_rows if row['risk_level'] == 'low')
    red_zone_count = sum(1 for row in user_rows if row['health_class'] == 'red')
    yellow_zone_count = sum(1 for row in user_rows if row['health_class'] == 'yellow')
    high_interest_user_count = sum(1 for row in user_rows if row['high_interest_count'] > 0)

    context = {
        'mode': mode,
        'query': query,
        'risk_rows': filtered_rows,
        'high_risk_count': high_risk_count,
        'medium_risk_count': medium_risk_count,
        'low_risk_count': low_risk_count,
        'red_zone_count': red_zone_count,
        'yellow_zone_count': yellow_zone_count,
        'high_interest_user_count': high_interest_user_count,
        'compose_target_group': compose_target_group,
        'compose_subject': compose_subject,
        'compose_message': compose_message,
    }
    return _render(request, 'system_risk.html', context)


@admin_required
def admin_charts(request):
    settings_obj = _get_system_settings()
    users_qs = _admin_user_queryset()
    user_rows = _admin_user_rows(users_qs, settings_obj=settings_obj)
    safe_count, risky_count, danger_count = _zone_counts(user_rows)
    signup_labels, signup_values = _monthly_signup_trend(users_qs, months=6)
    loan_labels, loan_values = _loan_mix_counts()

    loans_qs = Loan.objects.filter(user__is_superuser=False)
    high_interest_loan_count = loans_qs.filter(
        interest_rate__gt=settings_obj.high_interest_rate_limit
    ).count()
    loan_rates = list(loans_qs.values_list('interest_rate', flat=True))
    avg_interest_rate = round(sum(loan_rates) / len(loan_rates), 2) if loan_rates else 0

    interest_by_type_map = defaultdict(list)
    for loan in loans_qs:
        interest_by_type_map[loan.loan_type].append(loan.interest_rate)
    interest_labels = list(interest_by_type_map.keys())[:8]
    interest_values = [
        round(sum(interest_by_type_map[label]) / len(interest_by_type_map[label]), 2)
        for label in interest_labels
    ]
    if not interest_labels:
        interest_labels = ['No Loans']
        interest_values = [0]

    expense_totals = {
        'Grocery': 0,
        'Rent': 0,
        'Transport': 0,
        'Entertainment': 0,
    }
    for budget in Budget.objects.filter(user__is_superuser=False):
        expense_totals['Grocery'] += budget.grocery
        expense_totals['Rent'] += budget.rent
        expense_totals['Transport'] += budget.transport
        expense_totals['Entertainment'] += budget.entertainment
    expense_labels = list(expense_totals.keys())
    expense_values = list(expense_totals.values())
    if sum(expense_values) == 0:
        expense_values = [1, 0, 0, 0]

    zone_trend = {'low': [], 'medium': [], 'high': []}
    month_keys = []
    today_month_start = timezone.localdate().replace(day=1)
    month_sequence = []
    for offset in range(5, -1, -1):
        month_start = _month_start(today_month_start, offset)
        month_sequence.append(month_start)
        month_keys.append(month_start.strftime('%Y-%m'))
    for _ in month_keys:
        zone_trend['low'].append(0)
        zone_trend['medium'].append(0)
        zone_trend['high'].append(0)
    month_index = {key: idx for idx, key in enumerate(month_keys)}
    for row in user_rows:
        join_key = row['user'].date_joined.strftime('%Y-%m')
        if join_key in month_index:
            zone_trend[row['risk_level']][month_index[join_key]] += 1

    timeline_labels = []
    timeline_values = []
    cursor = timezone.localdate().replace(day=1)
    for _ in range(6):
        next_cursor = _next_month_start(cursor)
        timeline_labels.append(cursor.strftime('%b %Y'))
        timeline_values.append(
            loans_qs.filter(end_date__gte=cursor, end_date__lt=next_cursor).count()
        )
        cursor = next_cursor

    chart_payload = {
        'loan_distribution': {'labels': loan_labels, 'values': loan_values},
        'monthly_users': {'labels': signup_labels, 'values': signup_values},
        'emi_zone_trend': {
            'labels': [m.strftime('%b %Y') for m in month_sequence],
            'low': zone_trend['low'],
            'medium': zone_trend['medium'],
            'high': zone_trend['high'],
        },
        'interest_comparison': {'labels': interest_labels, 'values': interest_values},
        'expense_distribution': {'labels': expense_labels, 'values': expense_values},
        'loan_timeline': {'labels': timeline_labels, 'values': timeline_values},
    }

    context = {
        'safe_count': safe_count,
        'risky_count': risky_count,
        'danger_count': danger_count,
        'total_loans': loans_qs.count(),
        'high_interest_loan_count': high_interest_loan_count,
        'avg_interest_rate': avg_interest_rate,
        'signup_labels': signup_labels,
        'signup_values': signup_values,
        'loan_labels': loan_labels,
        'loan_values': loan_values,
        'chart_payload': chart_payload,
    }
    return _render(request, 'charts.html', context)


@admin_required
def admin_reports(request):
    return redirect('admin_charts')


@admin_required
def admin_loan_overview(request):
    settings_obj = _get_system_settings()
    loan_type_filter = request.GET.get('loan_type', '').strip()
    today = timezone.localdate()
    soon_cutoff = today + timedelta(days=90)

    loans_qs = Loan.objects.filter(user__is_superuser=False).select_related('user').order_by('end_date')
    if loan_type_filter:
        loans_qs = loans_qs.filter(loan_type=loan_type_filter)

    all_loan_types = (
        Loan.objects.filter(user__is_superuser=False)
        .values_list('loan_type', flat=True)
        .distinct()
        .order_by('loan_type')
    )

    ending_soon_ids = set(
        Loan.objects.filter(
            user__is_superuser=False,
            end_date__gte=today,
            end_date__lte=soon_cutoff,
        ).values_list('id', flat=True)
    )
    high_interest_ids = set(
        Loan.objects.filter(
            user__is_superuser=False,
            interest_rate__gt=settings_obj.high_interest_rate_limit,
        ).values_list('id', flat=True)
    )
    multi_loan_users = (
        Loan.objects.filter(user__is_superuser=False)
        .values('user__id', 'user__username')
        .annotate(loan_count=Count('id'))
        .filter(loan_count__gt=1)
        .order_by('-loan_count')
    )

    context = {
        'loan_type_filter': loan_type_filter,
        'loan_types': all_loan_types,
        'loans': loans_qs,
        'ending_soon_ids': ending_soon_ids,
        'high_interest_ids': high_interest_ids,
        'high_interest_limit': settings_obj.high_interest_rate_limit,
        'multi_loan_users': multi_loan_users,
    }
    return _render(request, 'loan_overview.html', context)


@admin_required
def admin_income_overview(request):
    users_qs = _admin_user_queryset()
    settings_obj = _get_system_settings()
    rows = []

    for user in users_qs:
        snapshot = _financial_snapshot(user, settings_obj=settings_obj)
        income_obj = snapshot['income_obj']
        rows.append(
            {
                'user': user,
                'income_obj': income_obj,
                'monthly_salary': income_obj.monthly_salary if income_obj else 0,
                'other_income': income_obj.other_income if income_obj else 0,
                'total_income': snapshot['total_income'],
                'total_emi': snapshot['total_emi'],
                'card_due_estimate': snapshot['credit_card_due_estimate'],
                'total_monthly_obligation': snapshot['total_monthly_obligation'],
                'emi_ratio': snapshot['emi_ratio'],
                'overall_burden_ratio': snapshot['overall_burden_ratio'],
                'health_zone': snapshot['health_zone'],
                'health_class': snapshot['health_class'],
            }
        )

    context = {'income_rows': rows}
    return _render(request, 'income_overview.html', context)


@admin_required
def admin_budget_overview(request):
    users_qs = _admin_user_queryset()
    settings_obj = _get_system_settings()
    rows = []
    overspending_count = 0
    negative_cashflow_count = 0

    for user in users_qs:
        snapshot = _financial_snapshot(user, settings_obj=settings_obj)
        budget_obj = snapshot['budget_obj']
        budget_total = snapshot['total_budget_expense']
        remaining_after_obligations = snapshot.get('remaining_after_obligations', snapshot['remaining_after_emi'])
        savings_after_emi = remaining_after_obligations - budget_total
        overspending = budget_total > remaining_after_obligations
        negative_cashflow = savings_after_emi < 0
        if overspending:
            overspending_count += 1
        if negative_cashflow:
            negative_cashflow_count += 1

        rows.append(
            {
                'user': user,
                'budget_obj': budget_obj,
                'grocery': budget_obj.grocery if budget_obj else 0,
                'rent': budget_obj.rent if budget_obj else 0,
                'transport': budget_obj.transport if budget_obj else 0,
                'entertainment': budget_obj.entertainment if budget_obj else 0,
                'budget_total': budget_total,
                'remaining_after_emi': remaining_after_obligations,
                'savings_after_emi': savings_after_emi,
                'card_due_estimate': snapshot['credit_card_due_estimate'],
                'overspending': overspending,
                'negative_cashflow': negative_cashflow,
            }
        )

    context = {
        'budget_rows': rows,
        'overspending_count': overspending_count,
        'negative_cashflow_count': negative_cashflow_count,
    }
    return _render(request, 'budget_overview.html', context)


@admin_required
def admin_export_report(request, export_type):
    settings_obj = _get_system_settings()

    if export_type == 'users':
        users_qs = _admin_user_queryset().order_by('username')
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="users_export.csv"'
        writer = csv.writer(response)
        writer.writerow(
            [
                'username',
                'email',
                'status',
                'monthly_salary',
                'other_income',
                'total_income',
                'loan_emi',
                'card_due',
                'total_monthly_obligation',
                'emi_ratio',
                'overall_burden_ratio',
                'zone',
                'last_login',
            ]
        )
        for user in users_qs:
            snapshot = _financial_snapshot(user, settings_obj=settings_obj)
            income_obj = snapshot['income_obj']
            writer.writerow(
                [
                    user.username,
                    user.email,
                    'active' if user.is_active else 'inactive',
                    income_obj.monthly_salary if income_obj else 0,
                    income_obj.other_income if income_obj else 0,
                    snapshot['total_income'],
                    snapshot['total_emi'],
                    snapshot['credit_card_due_estimate'],
                    snapshot['total_monthly_obligation'],
                    snapshot['emi_ratio'],
                    snapshot['overall_burden_ratio'],
                    snapshot['health_zone'],
                    user.last_login.strftime('%Y-%m-%d %H:%M:%S') if user.last_login else '',
                ]
            )
        _log_admin_action(request.user, 'export_users_csv', details='Downloaded users CSV.')
        return response

    if export_type == 'loans':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="loans_export.csv"'
        writer = csv.writer(response)
        writer.writerow(
            [
                'username',
                'loan_type',
                'lender',
                'principal',
                'monthly_emi',
                'interest_rate',
                'start_date',
                'end_date',
                'high_interest',
            ]
        )

        for loan in Loan.objects.filter(user__is_superuser=False).select_related('user').order_by('user__username'):
            writer.writerow(
                [
                    loan.user.username,
                    loan.loan_type,
                    loan.lender,
                    loan.principal,
                    loan.monthly_emi,
                    loan.interest_rate,
                    loan.start_date,
                    loan.end_date,
                    'yes' if loan.interest_rate > settings_obj.high_interest_rate_limit else 'no',
                ]
            )

        _log_admin_action(request.user, 'export_loans_csv', details='Downloaded loans CSV.')
        return response

    if export_type == 'budgets':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="budgets_export.csv"'
        writer = csv.writer(response)
        writer.writerow(
            [
                'username',
                'grocery',
                'rent',
                'transport',
                'entertainment',
                'total_expense',
                'loan_emi',
                'card_due',
                'remaining_after_obligations',
                'savings_after_obligations',
                'overspending',
                'negative_cashflow',
            ]
        )

        for user in _admin_user_queryset():
            snapshot = _financial_snapshot(user, settings_obj=settings_obj)
            budget = snapshot['budget_obj']
            total_expense = snapshot['total_budget_expense']
            remaining_after_obligations = snapshot.get('remaining_after_obligations', snapshot['remaining_after_emi'])
            net_after_cards = snapshot.get('net_savings_after_cards', snapshot['net_savings'])
            writer.writerow(
                [
                    user.username,
                    budget.grocery if budget else 0,
                    budget.rent if budget else 0,
                    budget.transport if budget else 0,
                    budget.entertainment if budget else 0,
                    total_expense,
                    snapshot['total_emi'],
                    snapshot['credit_card_due_estimate'],
                    remaining_after_obligations,
                    net_after_cards,
                    'yes' if total_expense > remaining_after_obligations else 'no',
                    'yes' if net_after_cards < 0 else 'no',
                ]
            )

        _log_admin_action(request.user, 'export_budgets_csv', details='Downloaded budgets CSV.')
        return response

    if export_type == 'emi-pdf':
        user_rows = _admin_user_rows(_admin_user_queryset(), settings_obj=settings_obj)
        safe_count, risky_count, danger_count = _zone_counts(user_rows)
        average_emi_ratio = (
            round(sum(row['emi_ratio'] for row in user_rows) / len(user_rows), 2) if user_rows else 0
        )
        average_overall_ratio = (
            round(sum(row.get('overall_burden_ratio', row['emi_ratio']) for row in user_rows) / len(user_rows), 2)
            if user_rows
            else 0
        )
        top_risk_rows = sorted(
            user_rows,
            key=lambda row: row.get('overall_burden_ratio', row['emi_ratio']),
            reverse=True,
        )[:12]
        generated_on = timezone.localtime().strftime('%d %b %Y %H:%M')

        sections = [
            {
                'heading': 'Executive Summary',
                'rows': [
                    f'Total monitored users: {len(user_rows)}',
                    f'Average loan-only EMI ratio: {average_emi_ratio}%',
                    f'Average overall debt ratio (loan + cards): {average_overall_ratio}%',
                    f'High-interest threshold: {settings_obj.high_interest_rate_limit}%',
                ],
            },
            {
                'heading': 'Zone Distribution',
                'rows': [
                    f'Green zone users: {safe_count}',
                    f'Yellow zone users: {risky_count}',
                    f'Red zone users: {danger_count}',
                ],
            },
            {
                'heading': 'Top Risk Accounts (By Overall Debt Ratio)',
                'rows': (
                    [
                        (
                            f"{index}. {row['user'].username} | "
                            f"Loan EMI {row['emi_ratio']}% | "
                            f"Overall {row.get('overall_burden_ratio', row['emi_ratio'])}% | "
                            f"Loans {row['loan_count']} | Risk {row['risk_label']}"
                        )
                        for index, row in enumerate(top_risk_rows, start=1)
                    ]
                    if top_risk_rows
                    else ['No user records available.']
                ),
            },
            {
                'heading': 'Interpretation Guide',
                'rows': [
                    'Loan EMI ratio reflects only loan obligations.',
                    'Overall debt ratio combines loan EMI and monthly credit-card obligations.',
                    'Prioritize users with high overall ratio and high-interest exposure.',
                ],
            },
        ]

        pdf_bytes = _build_structured_pdf_bytes(
            title='EMI Analyzer Risk Report',
            subtitle=f'Generated on {generated_on}',
            sections=sections,
        )
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = 'attachment; filename="emi_report.pdf"'
        _log_admin_action(request.user, 'export_emi_pdf', details='Downloaded EMI PDF report.')
        return response

    messages.error(request, 'Unsupported export type.')
    return redirect('admin_charts')


@admin_required
def admin_system_controls(request):
    settings_obj = _get_system_settings()

    if request.method == 'POST':
        green_limit = _to_float(request.POST.get('emi_green_limit'), settings_obj.emi_green_limit)
        yellow_limit = _to_float(request.POST.get('emi_yellow_limit'), settings_obj.emi_yellow_limit)
        high_interest_limit = _to_float(
            request.POST.get('high_interest_rate_limit'),
            settings_obj.high_interest_rate_limit,
        )
        savings_target = _to_float(
            request.POST.get('savings_target_percent'),
            settings_obj.savings_target_percent,
        )
        advisory_message = request.POST.get('advisory_message', '').strip()

        errors = []
        if green_limit is None or yellow_limit is None:
            errors.append('EMI zone limits must be valid numbers.')
        elif green_limit < 0 or yellow_limit > 100 or green_limit >= yellow_limit:
            errors.append('Set EMI limits as: green >= 0, yellow <= 100, and green < yellow.')

        if high_interest_limit is None or high_interest_limit <= 0:
            errors.append('High-interest threshold must be greater than 0.')

        if savings_target is None or savings_target <= 0 or savings_target >= 100:
            errors.append('Savings target percent must be between 1 and 99.')

        if len(advisory_message) > 500:
            errors.append('Global advisory message must be 500 characters or less.')

        if errors:
            for err in errors:
                messages.error(request, err)
        else:
            settings_obj.emi_green_limit = green_limit
            settings_obj.emi_yellow_limit = yellow_limit
            settings_obj.high_interest_rate_limit = high_interest_limit
            settings_obj.savings_target_percent = savings_target
            settings_obj.advisory_message = advisory_message
            settings_obj.save()

            _log_admin_action(
                request.user,
                'updated_system_controls',
                details='Updated EMI thresholds and advisory message.',
            )
            messages.success(request, 'System controls updated successfully.')
            return redirect('admin_system_controls')

    context = {'settings_obj': settings_obj}
    return _render(request, 'system_controls.html', context)


@admin_required
def admin_audit_logs(request):
    query = request.GET.get('q', '').strip()
    logs = AuditLog.objects.select_related('actor', 'target_user')

    if query:
        logs = logs.filter(
            Q(action__icontains=query)
            | Q(details__icontains=query)
            | Q(actor__username__icontains=query)
            | Q(target_user__username__icontains=query)
        )

    context = {'query': query, 'logs': logs[:200]}
    return _render(request, 'audit_logs.html', context)


@login_required
def profile_view(request):
    profile = _get_or_create_profile(request.user)

    if request.user.is_superuser:
        settings_obj = _get_system_settings()
        users_qs = _admin_user_queryset()
        user_rows = _admin_user_rows(users_qs, settings_obj=settings_obj)
        safe_count, risky_count, danger_count = _zone_counts(user_rows)
        active_count = sum(1 for row in user_rows if row['is_active'])

        context = {
            'member_since': request.user.date_joined,
            'phone_number': profile.phone_number or 'Not set',
            'managed_users': len(user_rows),
            'active_users': active_count,
            'inactive_users': len(user_rows) - active_count,
            'safe_count': safe_count,
            'risky_count': risky_count,
            'danger_count': danger_count,
            'accounts_with_loans': sum(1 for row in user_rows if row['loan_count'] > 0),
            'latest_audit': AuditLog.objects.select_related('target_user', 'actor').first(),
        }
        return _render(request, 'profile.html', context)

    snapshot = _financial_snapshot(request.user)
    context = {
        **snapshot,
        'loan_count': snapshot.get('active_loan_count', len(snapshot['loans'])),
        'high_interest_count': len(snapshot['high_interest_loans']),
        'member_since': request.user.date_joined,
        'phone_number': profile.phone_number or 'Not set',
    }
    return _render(request, 'profile.html', context)


@login_required
def settings_view(request):
    profile = _get_or_create_profile(request.user)

    if request.method == 'POST':
        email = request.POST.get('email', '').strip()
        phone_number_raw = request.POST.get('phone_number', '').strip()
        profile_photo = request.FILES.get('profile_photo')
        remove_profile_photo = request.POST.get('remove_profile_photo') == 'on'
        current_password = request.POST.get('current_password', '')
        new_password = request.POST.get('new_password', '')
        confirm_password = request.POST.get('confirm_password', '')
        new_password_error = _validate_password(new_password, 'New password') if (new_password or confirm_password) else ''
        profile_photo_error = _validate_profile_photo(profile_photo)

        has_error = False
        password_changed = False
        normalized_phone = ''

        if not email:
            messages.error(request, 'Email is required.')
            has_error = True
        elif not _is_valid_email(email):
            messages.error(request, 'Please enter a valid email address.')
            has_error = True
        elif User.objects.filter(email=email).exclude(id=request.user.id).exists():
            messages.error(request, 'This email is already used by another account.')
            has_error = True
        else:
            request.user.email = email

        normalized_phone, phone_error = _validate_phone_number(phone_number_raw)
        if phone_error:
            messages.error(request, phone_error)
            has_error = True
        elif UserProfile.objects.filter(phone_number=normalized_phone).exclude(user=request.user).exists():
            messages.error(request, 'This phone number is already used by another account.')
            has_error = True
        else:
            profile.phone_number = normalized_phone

        if profile_photo_error:
            messages.error(request, profile_photo_error)
            has_error = True
        elif profile_photo:
            if profile.profile_photo:
                profile.profile_photo.delete(save=False)
            profile.profile_photo = profile_photo
        elif remove_profile_photo and profile.profile_photo:
            profile.profile_photo.delete(save=False)
            profile.profile_photo = None

        if new_password or confirm_password:
            if not current_password:
                messages.error(request, 'Current password is required to change password.')
                has_error = True
            elif not request.user.check_password(current_password):
                messages.error(request, 'Current password is incorrect.')
                has_error = True
            elif new_password != confirm_password:
                messages.error(request, 'New password and confirm password do not match.')
                has_error = True
            elif new_password_error:
                messages.error(request, new_password_error)
                has_error = True
            else:
                request.user.set_password(new_password)
                password_changed = True

        if not has_error:
            request.user.save()
            profile.save()
            if password_changed:
                update_session_auth_hash(request, request.user)
            messages.success(request, 'Settings updated successfully.')
            return redirect('settings')

    context = {'phone_number': profile.phone_number or ''}
    return _render(request, 'settings.html', context)


@login_required
def lock_screen_view(request):
    locked_user_id = request.user.id
    logout(request)
    request.session['locked_user_id'] = locked_user_id
    return redirect('unlock_screen')


def unlock_screen_view(request):
    locked_user_id = request.session.get('locked_user_id')
    if not locked_user_id:
        return redirect('login')

    locked_user = User.objects.filter(id=locked_user_id).first()
    if not locked_user:
        request.session.pop('locked_user_id', None)
        return redirect('login')

    if request.method == 'POST':
        password = request.POST.get('password', '')
        authed_user = authenticate(request, username=locked_user.username, password=password)
        if authed_user is not None:
            login(request, authed_user)
            request.session.pop('locked_user_id', None)
            messages.success(request, 'Unlocked successfully.')
            return redirect('dashboard')
        messages.error(request, 'Incorrect password. Please try again.')

    context = {'locked_user': locked_user}
    return _render(request, 'lock_screen.html', context, user_override=locked_user)
