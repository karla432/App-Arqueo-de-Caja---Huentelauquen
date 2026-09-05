from django.urls import path

from nucleo import views

urlpatterns = [
    path("excepciones/<str:fecha>/", views.bandeja_excepciones, name="bandeja_excepciones"),
    path("excepciones/<str:fecha>/reconciliar/", views.reconciliar_dia, name="reconciliar_dia"),
    path("excepciones/resolver/<int:excepcion_id>/", views.resolver_excepcion_view, name="resolver_excepcion"),
]
