from django.urls import path
from . import views

urlpatterns = [
    path('dashboard/', views.dashboard, name='dashboard'),
    path('activate/<uidb64>/<token>/', views.activate_account, name='activate'),
    path("ajax/zonas/", views.ajax_zonas_by_sector, name="ajax_zonas_by_sector"),
    path("ajax/grupos/", views.ajax_grupos_by_zona, name="ajax_grupos_by_zona"),
    path("perfil/", views.profile, name="profile"),
    path("perfil/", views.my_profile, name="my_profile"),
    path("perfil/editar/", views.edit_my_profile, name="edit_my_profile"),
    path("miembros/<int:user_id>/", views.member_profile, name="member_profile"),
    path("home/banners/", views.manage_banners, name="manage_banners"),
    path("home/banners/nuevo/", views.create_banner, name="create_banner"),
    path("home/banners/<int:banner_id>/editar/", views.edit_banner, name="edit_banner"),
    path("home/banners/<int:banner_id>/eliminar/", views.delete_banner, name="delete_banner"),
    path("home/actividades/", views.manage_events, name="manage_events"),
    path("home/actividades/nueva/", views.create_event, name="create_event"),
    path("home/actividades/<int:event_id>/editar/", views.edit_event, name="edit_event"),
    path("home/actividades/<int:event_id>/eliminar/", views.delete_event, name="delete_event"),


]
