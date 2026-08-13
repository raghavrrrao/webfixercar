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

    # The broken NovaCloud site, shown in the briefing. Reading it is free and
    # deliberately does not start the clock.
    path('api/broken-preview/', views.broken_preview, name='api_broken_preview'),

    # Race API. Every one of these is server-authoritative.
    path('api/race/start/', views.api_race_start, name='api_race_start'),
    path('api/race/state/', views.api_race_state, name='api_race_state'),
    path('api/race/progress/', views.api_race_progress, name='api_race_progress'),
    path('api/race/complete/', views.api_race_complete, name='api_race_complete'),

    # NovaCloud as the collected repairs have left it, for the track-side panel.
    path('api/race/preview/', views.race_preview, name='api_race_preview'),

    # The reward: the finished page, unlocked by crossing the finish line.
    path('api/final-preview/', views.final_preview, name='api_final_preview'),

    # The participant's own recorded entry, once the round has closed.
    path('api/final-design/', views.final_design, name='api_final_design'),
]
