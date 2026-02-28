# EMI Analyzer

A Django-based EMI and debt management platform with separate user and admin modules.
The app tracks income, loans, budgets, and credit card obligations (monthly spend + card EMI),
then provides dashboard insights, risk signals, and exports.

## Core Features

- User authentication: register, login, logout, lock/unlock screen, password reset with OTP email.
- Loan management: add, edit, list, delete loans with lender, EMI, and date range.
- Credit card management with card account setup (issuer, limit, interest, rewards).
- Credit card monthly spend and EMI entry tracking with tenure-based outstanding logic.
- Monthly payment planner: dedicated view of dues for current month (loans + cards).
- Dashboard analytics: debt mix, card-aware cashflow, and loan timeline charts.
- AI-style debt burden suggestions and risk messaging.
- Budget planner with overspending suggestions.
- Admin module for user management and loan/income/budget overviews.
- Admin risk monitor with advisory message/email workflows.
- Admin charts, exports (CSV/PDF), and audit logs.

## Tech Stack

- Python 3.x
- Django
- SQLite (default)
- ReportLab (PDF generation)
- Chart.js (frontend charts)

## Project Structure

- `emianalyzer/` Django project settings and root URL config.
- `myapp/` main application (models, views, templates, static, urls, tests).
- `media/` uploaded files (user profile photos, runtime media).
- `scripts/` utility scripts (for example PDF documentation generator).
- `manage.py` Django management entrypoint.

## Database Models (myapp)

- `Income`
- `Loan`
- `Budget`
- `CreditCardAccount`
- `CreditCardEntry`
- `CreditCardSpend` (legacy model)
- `UserProfile`
- `SystemSetting`
- `AuditLog`

The project also uses `django.contrib.auth.models.User` for authentication and role checks.

## Setup (Local)

1. Create and activate a virtual environment.
2. Install dependencies.
3. Run migrations.
4. Create admin user.
5. Start server.

Example commands:

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/Mac
source .venv/bin/activate

pip install django reportlab
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Open: `http://127.0.0.1:8000/`

## Running Tests

```bash
python manage.py test
```

## Useful Routes

- User dashboard: `/`
- Register: `/register/`
- Login: `/login/`
- Loans: `/loans/`
- Credit cards: `/credit-cards/`
- Monthly payments: `/monthly-payments/`
- User charts: `/charts/`
- Admin users: `/admin/users/`
- Admin risk: `/admin/system-risk/`

## Security Notes

- Do not commit real email passwords or secret keys.
- Move `SECRET_KEY` and SMTP credentials from `settings.py` to environment variables before production deployment.
- Set `DEBUG = False` and restrict `ALLOWED_HOSTS` in production.

## Optional Documentation Artifact

If present, table-structure PDF can be regenerated using:

```bash
python scripts/generate_table_structure_pdf.py
```

This creates:

- `docs/project_table_structure.pdf`
