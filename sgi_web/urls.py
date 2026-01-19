from django.contrib import admin
from django.urls import path, include
from accounts import views as accounts_views
from django.conf import settings
from django.conf.urls.static import static
from django.shortcuts import redirect

def root_redirect(request):
    return redirect("login")  


urlpatterns = [
    path('admin/', admin.site.urls),
    path("", root_redirect, name="root"),
    path("home/", accounts_views.home, name="home"),
    path('accounts/', include('django.contrib.auth.urls')),  # login/logout/password
    path('accounts/', include('accounts.urls')),             # dashboard, activate
    path('kofu/', accounts_views.kofu_view, name='kofu'),
    path('kofu/informe/', accounts_views.kofu_report, name='kofu_report'),
    path('kofu/historial/', accounts_views.kofu_history, name='kofu_history'),
    path('kofu/activos/', accounts_views.kofu_active_members, name='kofu_active_members'),
    path('kofu/gestion-informes/', accounts_views.kofu_admin_reports, name='kofu_admin_reports'),
    path('notificaciones/', accounts_views.notifications_center, name='notifications_center'),
    path('kofu/activos/exportar/', accounts_views.kofu_active_members_export, name='kofu_active_members_export'),
    path("miembros/nuevo/", accounts_views.create_member, name="create_member"),
    path("miembros/<int:user_id>/editar/", accounts_views.edit_member, name="edit_member"),
    path("miembros/", accounts_views.members_list, name="members_list"),
    path("miembros/exportar/", accounts_views.members_export, name="members_export"),


]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

