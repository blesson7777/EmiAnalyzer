from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path

from myapp import views as myapp_views

urlpatterns = [
    path('admin/', myapp_views.admin_root_redirect, name='admin_root'),
    path('', include('myapp.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
