from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='movies_index'),
    path('search_ajax/', views.search_ajax, name='movies_search_ajax'),
    path('movie/<int:movie_id>/detail/', views.movie_detail_ajax, name='movie_detail'),
]

