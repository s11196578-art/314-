from django.urls import path
from bookings import views


app_name = 'bookings'

urlpatterns = [
    path('', views.homepage, name='home'),  # Root URL for homepage
    path('homepage/', views.homepage, name='homepage'),  # Backward compatibility
    path('history/', views.booking_history, name='booking_history'),
    path('ticket/<int:booking_id>/', views.view_tickets, name='view_tickets'),
    path('generate_ticket/<int:booking_id>/', views.generate_ticket, name='generate_ticket'),
    path('view_cargo/<int:cargo_id>/', views.view_cargo, name='view_cargo'),
    path('view_ticket/<str:qr_token>/', views.view_ticket, name='view_ticket'),
    path('book/', views.book_ticket, name='book_ticket'),
    path('process_payment/<int:booking_id>/', views.process_payment, name='process_payment'),
    path('success/', views.payment_success, name='success'),
    path('cancel/', views.payment_cancel, name='cancel'),
    path('cancel/<int:booking_id>/', views.cancel_booking, name='cancel_legacy'),
    path('modify/<int:booking_id>/', views.modify_booking, name='modify_booking'),
    path('cancel_booking/<int:booking_id>/', views.cancel_booking, name='cancel_booking'),
    path('api/bookings/', views.get_schedule_updates, name='get_schedule_updates'),
    path('api/bookings/', views.api_bookings, name='api_bookings'),
    path('api/pricing/', views.get_pricing, name='api_pricing'),  # Updated: Consistent API path
    path('api/stripe_webhook/', views.stripe_webhook, name='stripe_webhook'),  # Updated: Moved to api/
    path('api/weather/stream/', views.weather_stream, name='weather_stream'),
    path('api/weather/forecast/', views.weather_forecast_view, name='weather_forecast'),  # Added
    path('api/stripe/insights/', views.stripe_insights_view, name='stripe_insights'),  # Added
    path('api/validate_file/', views.validate_file, name='validate_file'),  # Updated: Moved to api/
    path('api/validate_step/', views.validate_step, name='validate_step'),  # Updated: Moved to api/
    path('privacy_policy/', views.privacy_policy, name='privacy_policy'),
    path('api/routes/', views.routes_api, name='routes_api'),
    path('api/homepage-schedules/', views.homepage_schedules_api, name='homepage_schedules_api'),
    path('api/weather/conditions/', views.get_weather_conditions, name='get_weather_conditions'),  # Updated: More specific path
    path('api/create_checkout_session/', views.create_checkout_session, name='api_create_checkout_session'), # Updated: Moved to api/
    path('api/check_session/', views.check_session, name='check_session'),  # Updated: Moved to api/
    path('booking/<int:booking_id>/pdf/', views.booking_pdf, name='booking_pdf'),
    path('profile/', views.profile, name='profile'),
    path('terms_of_service/', views.terms_of_service, name='terms_of_service'),  # Updated: Consistent naming
    path('get_pricing/', views.get_pricing, name='get-pricing'),
    path('check-schedule-availability/', views.check_schedule_availability, name='check_schedule_availability'),
]