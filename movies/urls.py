from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='movies_index'),
    path('search_ajax/', views.search_ajax, name='movies_search_ajax'),
    path('movie/<int:movie_id>/detail/', views.movie_detail_ajax, name='movie_detail'),
    path('movie/<int:movie_id>/refresh/', views.movie_refresh_ajax, name='movie_refresh_ajax'),
    path('manage/', views.manage_dashboard, name='manage_dashboard'),
    path('manage/api/stats/', views.manage_stats_api, name='manage_stats_api'),
    path('manage/api/task/start/', views.manage_task_start_api, name='manage_task_start_api'),
    path('manage/api/task/status/', views.manage_task_status_api, name='manage_task_status_api'),
    path('manage/api/task/stop/', views.manage_task_stop_api, name='manage_task_stop_api'),
    path('manage/api/movies/', views.manage_movies_api, name='manage_movies_api'),
    path('manage/api/movies/batch_action/', views.manage_batch_action_api, name='manage_batch_action_api'),
    path('manage/api/database/export/', views.manage_database_export, name='manage_database_export'),
    path('manage/api/database/import/', views.manage_database_import, name='manage_database_import'),
    path('manage/api/sources/', views.manage_sources_api, name='manage_sources_api'),
    path('manage/api/sources/save/', views.manage_source_save_api, name='manage_source_save_api'),
    path('manage/api/sources/delete/', views.manage_source_delete_api, name='manage_source_delete_api'),
    path('manage/api/sources/test/', views.manage_source_test_api, name='manage_source_test_api'),
    path('manage/api/movie/<int:movie_id>/action/', views.manage_movie_action_api, name='manage_movie_action_api'),
]

