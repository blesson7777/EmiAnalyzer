from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags


def _send_html_email(subject, to_list, html_template, context, bcc_list=None):
    html_body = render_to_string(html_template, context)
    text_body = strip_tags(html_body)
    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=to_list,
        bcc=bcc_list or [],
    )
    message.attach_alternative(html_body, 'text/html')
    message.send(fail_silently=False)


def send_otp_email(
    email: str,
    otp: str,
    recipient_name: str = '',
    account_role: str = 'User',
    reset_url: str = '',
    valid_minutes: int = 10,
) -> None:
    subject = f'EMI Analyzer {account_role} Password Reset OTP'
    context = {
        'recipient_name': recipient_name or 'there',
        'account_role': account_role,
        'otp': otp,
        'valid_minutes': valid_minutes,
        'reset_url': reset_url,
    }
    _send_html_email(
        subject=subject,
        to_list=[email],
        html_template='emails/forgot_password_otp.html',
        context=context,
    )


def send_advisory_email(recipients, subject: str, message_body: str, sent_by: str = 'EMI Analyzer Team') -> int:
    clean_recipients = sorted({email.strip() for email in recipients if email and email.strip()})
    if not clean_recipients:
        return 0

    context = {
        'subject_line': subject,
        'message_body': message_body,
        'sent_by': sent_by,
        'recipient_count': len(clean_recipients),
    }
    _send_html_email(
        subject=subject,
        to_list=[settings.DEFAULT_FROM_EMAIL],
        bcc_list=clean_recipients,
        html_template='emails/advisory_notification.html',
        context=context,
    )
    return len(clean_recipients)
