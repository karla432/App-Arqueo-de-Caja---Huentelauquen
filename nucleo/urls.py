from django.urls import path

from nucleo import views

urlpatterns = [
    path("", views.ventas_dashboard, name="ventas_dashboard"),
    path("excepciones/<str:fecha>/", views.bandeja_excepciones, name="bandeja_excepciones"),
    path("excepciones/<str:fecha>/reconciliar/", views.reconciliar_dia, name="reconciliar_dia"),
    path("excepciones/resolver/<int:excepcion_id>/", views.resolver_excepcion_view, name="resolver_excepcion"),
    path("arqueo/<str:fecha>/", views.arqueo_efectivo, name="arqueo_efectivo"),
    path("arqueo/conteo/<int:arqueo_id>/", views.registrar_conteo_view, name="registrar_conteo"),
    path("arqueo/autorizar/<int:arqueo_id>/", views.autorizar_diferencia_view, name="autorizar_diferencia"),
    path("arqueo/<str:fecha>/deposito/", views.registrar_deposito_view, name="registrar_deposito"),
    path("cierre/<str:fecha>/", views.cierre_y_exportacion, name="cierre_y_exportacion"),
    path("cierre/<str:fecha>/aprobar/", views.aprobar_dia_view, name="aprobar_dia"),
    path("cierre/<str:fecha>/reabrir/", views.reabrir_dia_view, name="reabrir_dia"),
    path("cierre/<str:fecha>/exportar/", views.generar_exportacion_view, name="generar_exportacion"),
    path("dashboard/<str:fecha>/", views.dashboard_dia, name="dashboard_dia"),
    path("historico/", views.historico, name="historico"),
    path("calidad/", views.calidad_registro, name="calidad_registro"),
]
