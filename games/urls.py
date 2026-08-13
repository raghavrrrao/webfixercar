from django.contrib import admin
from django.urls import path

from first import views

urlpatterns = [
    path('admin/', admin.site.urls),

    # Game home page
    path('', views.intro, name='intro'),

    # Auth routes
    path('signup/', views.user_signup, name='signup'),
    path('login/', views.user_login, name='login'),
    path('logout/', views.user_logout, name='logout'),

    # Game routes
    path('start/', views.start, name='start'),
    path('home/', views.home, name='home'),
    path('race-result/', views.race_result, name='race_result'),

    # Arena API
    path('save-css/', views.save_css, name='save_css'),
    path('get-css/', views.get_css, name='get_css'),
    path('api/state/', views.api_state, name='api_state'),
    path('api/broken-preview/', views.broken_preview, name='api_broken_preview'),
    path('api/race/start/', views.api_race_start, name='api_race_start'),
    path('api/race/state/', views.api_race_state, name='api_race_state'),
    path('api/race/complete/', views.api_race_complete, name='api_race_complete'),
    path('api/check/', views.api_check, name='api_check'),
    path('api/reset/', views.api_reset, name='api_reset'),

    # Hints are charged and recorded server-side.
    path('api/hint/', views.api_hint, name='api_hint'),

    # Visual reference only: the official finished page, never graded.
    path('api/final-preview/', views.final_preview, name='api_final_preview'),

    # The player's own submitted design, once the round has closed.
    path('api/final-design/', views.final_design, name='api_final_design'),
]
