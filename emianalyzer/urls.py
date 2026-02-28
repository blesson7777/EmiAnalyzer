from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path
from django.core.management import call_command
from django.http import HttpResponse
from myapp import views as myapp_views

# Temporary migration function
def run_migrate(request):
    call_command('migrate')
    return HttpResponse("MIGRATIONS DONE")

urlpatterns = [
    # TEMP MIGRATION URL
    path("run-migrate/", run_migrate),

    # Admin root redirect
    path('admin/', myapp_views.admin_root_redirect, name='admin_root'),

    # All app URLs
    path('', include('myapp.urls')),
]

# Static files (for debug mode only)
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)