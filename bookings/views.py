import base64
import datetime
import hashlib
import io, os
import json
import logging
import re
import time
import uuid
from decimal import Decimal, ROUND_HALF_UP
from email.mime.image import MIMEImage
from io import BytesIO

from django.contrib.admin.views.decorators import staff_member_required
from django.core.files.storage import default_storage
from django.db import transaction
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
import stripe
import qrcode
import requests
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile, File
from django.core.mail import send_mail, EmailMultiAlternatives
from django.core.validators import FileExtensionValidator
from django.db.models import Subquery, Max, OuterRef, Prefetch
from django.http import JsonResponse, HttpResponseForbidden, StreamingHttpResponse, HttpResponse, FileResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import cache_page
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_POST, require_GET

from .decorators import login_required_allow_anonymous
from .forms import CargoBookingForm, ModifyBookingForm
from .models import Schedule, Booking, Passenger, Payment, Ticket, Cargo, Route, WeatherCondition, AddOn, Vehicle, Port

logger = logging.getLogger(__name__)
stripe.api_key = settings.STRIPE_SECRET_KEY

def safe_float(val):
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def safe_int(val):
    try:
        return int(val) if val is not None else 0
    except (TypeError, ValueError):
        return 0


@require_GET
@csrf_exempt
def check_session(request):
    """
    Check if the user's session is valid.
    """
    if request.session.session_key:
        return JsonResponse({'valid': True})
    return JsonResponse({'valid': False}, status=401)


@require_GET
def weather_stream(request):
    API_KEY = settings.WEATHER_API_KEY
    FETCH_INTERVAL = 30  # seconds

    def fetch_weather(route, port):
        """Fetch weather from DB or API"""
        now = timezone.now()
        # Check DB first
        weather = WeatherCondition.objects.filter(
            route=route, port=port, expires_at__gt=now
        ).order_by('-updated_at').first()

        if weather and not weather.is_expired():
            return weather

        # Fallback to API
        try:
            resp = requests.get(
                'https://api.weatherapi.com/v1/current.json',
                params={'key': API_KEY, 'q': f"{port.lat},{port.lng}", 'aqi': 'no'},
                timeout=5
            )
            resp.raise_for_status()
            data = resp.json()
            temperature = data['current']['temp_c']
            wind_speed = data['current']['wind_kph']
            condition = data['current']['condition']['text']
            precipitation_probability = data['current'].get('precip_mm', 0) * 100

            weather, _ = WeatherCondition.objects.update_or_create(
                route=route,
                port=port,
                defaults={
                    'temperature': temperature,
                    'wind_speed': wind_speed,
                    'precipitation_probability': precipitation_probability,
                    'condition': condition,
                    'expires_at': now + datetime.timedelta(minutes=30),
                    'updated_at': now
                }
            )
            return weather
        except requests.RequestException as e:
            return None

    def stream():
        last_sent_times = {}
        while True:
            now = timezone.now()
            schedules = Schedule.objects.filter(
                status='scheduled', departure_time__gt=now
            ).select_related('route__departure_port')
            route_ids = schedules.values_list('route_id', flat=True).distinct()
            routes = Route.objects.filter(id__in=route_ids).select_related('departure_port')

            weather_data = []

            for route in routes:
                port = route.departure_port
                weather = fetch_weather(route, port)
                last_sent = last_sent_times.get(route.id)

                if weather and (not last_sent or weather.updated_at > last_sent):
                    data = {
                        'route_id': route.id,
                        'port': port.name,
                        'temperature': safe_float(weather.temperature),
                        'wind_speed': safe_float(weather.wind_speed),
                        'precipitation_probability': safe_float(weather.precipitation_probability),
                        'condition': weather.condition,
                        'updated_at': weather.updated_at.isoformat(),
                        'expires_at': weather.expires_at.isoformat(),
                        'warning': None
                    }
                    if data['wind_speed'] and data['wind_speed'] > 30:
                        data['warning'] = 'Strong winds expected, potential delays.'
                    elif data['precipitation_probability'] and data['precipitation_probability'] > 50:
                        data['warning'] = 'High chance of rain, please prepare accordingly.'

                    weather_data.append(data)
                    last_sent_times[route.id] = now

            if weather_data:
                yield f"data: {json.dumps({'weather': weather_data})}\n\n"

            yield ":\n\n"  # SSE keep-alive
            time.sleep(FETCH_INTERVAL)

    response = StreamingHttpResponse(stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response


@require_GET
@cache_page(60 * 10)  # Cache for 10 minutes
def get_weather_conditions(request):
    schedule_id = request.GET.get('schedule_id')
    since = request.GET.get('since')
    last_updated = None

    # Parse 'since' parameter for conditional updates
    if since:
        try:
            last_updated = timezone.datetime.fromisoformat(since.replace('Z', '+00:00'))
            if not timezone.is_aware(last_updated):
                last_updated = timezone.make_aware(last_updated)
        except ValueError:
            logger.error(f"Invalid 'since' parameter: {since}")
            return JsonResponse({'valid': False, 'error': 'Invalid since parameter'}, status=400)

    # Validate schedule_id
    if not schedule_id or not schedule_id.isdigit():
        logger.error(f"Invalid schedule_id: {schedule_id}")
        return JsonResponse({'valid': False, 'error': 'Invalid schedule ID'}, status=400)

    # Fetch the specific schedule
    try:
        schedule = Schedule.objects.select_related('route__departure_port').get(
            id=schedule_id,
            status='scheduled',
            departure_time__gt=timezone.now()
        )
    except Schedule.DoesNotExist:
        logger.error(f"Schedule not found or invalid: {schedule_id}")
        return JsonResponse({'valid': False, 'error': 'Schedule not found or no longer available'}, status=404)

    route = schedule.route
    port = route.departure_port

    # Generate concise cache key using MD5 hash of route_id
    cache_key = f"weather_route_{hashlib.md5(str(route.id).encode()).hexdigest()}"
    logger.debug(f"Generated cache key: {cache_key}")

    # Check cache
    cached_weather = cache.get(cache_key)
    if cached_weather and (not last_updated or cached_weather['updated_at'] > last_updated.isoformat()):
        logger.info(f"Cache hit for route_id: {route.id}")
        return JsonResponse({'valid': True, 'weather': cached_weather})

    # Check database for existing weather condition
    now = timezone.now()
    weather = WeatherCondition.objects.filter(
        route=route,
        port=port,
        expires_at__gt=now
    )
    if last_updated:
        weather = weather.filter(updated_at__gt=last_updated)
    weather = weather.first()

    weather_data = None
    if weather:
        weather_data = {
            'route_id': route.id,
            'port': port.name,
            'temperature': float(weather.temperature) if weather.temperature is not None else None,
            'wind_speed': float(weather.wind_speed) if weather.wind_speed is not None else None,
            'precipitation_probability': float(weather.precipitation_probability) if weather.precipitation_probability is not None else None,
            'condition': weather.condition,
            'updated_at': weather.updated_at.isoformat(),
            'expires_at': weather.expires_at.isoformat(),
            'warning': None
        }
        # Generate warning based on conditions (customize as needed)
        if weather.wind_speed and weather.wind_speed > 30:
            weather_data['warning'] = 'Strong winds expected, potential delays.'
        elif weather.precipitation_probability and weather.precipitation_probability > 50:
            weather_data['warning'] = 'High chance of rain, please prepare accordingly.'

    else:
        # Fetch from Weather API
        try:
            response = requests.get(
                'https://api.weatherapi.com/v1/current.json',
                params={
                    'key': settings.WEATHER_API_KEY,
                    'q': f"{port.lat},{port.lng}",  # Fixed 'lng' to 'lon' assuming model field
                    'aqi': 'no'
                },
                timeout=5
            )
            response.raise_for_status()
            data = response.json()

            temperature = data['current']['temp_c']
            wind_speed = data['current']['wind_kph']
            condition = data['current']['condition']['text']
            precipitation_probability = data['current'].get('precip_mm', 0) * 100

            # Create or update WeatherCondition
            weather, _ = WeatherCondition.objects.update_or_create(
                route=route,
                port=port,
                defaults={
                    'temperature': temperature,
                    'wind_speed': wind_speed,
                    'precipitation_probability': precipitation_probability,
                    'condition': condition,
                    'expires_at': now + datetime.timedelta(minutes=30),
                    'updated_at': now
                }
            )

            weather_data = {
                'route_id': route.id,
                'port': port.name,
                'temperature': temperature,
                'wind_speed': wind_speed,
                'precipitation_probability': precipitation_probability,
                'condition': condition,
                'updated_at': now.isoformat(),
                'expires_at': (now + datetime.timedelta(minutes=30)).isoformat(),
                'warning': None
            }
            # Generate warning based on conditions
            if wind_speed > 30:
                weather_data['warning'] = 'Strong winds expected, potential delays.'
            elif precipitation_probability > 50:
                weather_data['warning'] = 'High chance of rain, please prepare accordingly.'

        except requests.RequestException as e:
            logger.error(f"WeatherAPI error for {port.name}: {str(e)}")
            weather_data = {
                'route_id': route.id,
                'port': port.name,
                'temperature': None,
                'wind_speed': None,
                'precipitation_probability': None,
                'condition': None,
                'updated_at': None,
                'expires_at': None,
                'warning': None,
                'error': 'Weather data unavailable'
            }

    # Cache the result
    cache.set(cache_key, weather_data, timeout=60 * 10)
    logger.info(f"Weather data cached for route_id: {route.id}")

    return JsonResponse({'valid': True, 'weather': weather_data})


def privacy_policy(request):
    return render(request, 'privacy_policy.html')


def calculate_cargo_price(weight_kg, cargo_type):
    try:
        weight_kg = Decimal(str(weight_kg))
        if weight_kg <= 0:
            raise ValueError("Weight must be positive")

        base_rate = Decimal('5.00')  # base price per kg

        # Multipliers for cargo categories
        type_multiplier = {
            'Light Cargo': Decimal('1.2'),   # parcels, boxes
            'Heavy Cargo': Decimal('2.0'),   # machinery, materials
            'Bulk Cargo': Decimal('1.5'),    # produce, sand, fuel
            'Livestock': Decimal('2.5')      # animals require special handling
        }

        multiplier = type_multiplier.get(cargo_type, Decimal('1.0'))
        return weight_kg * base_rate * multiplier

    except (ValueError, TypeError) as e:
        logger.error(
            f"Invalid cargo weight or type: weight_kg={weight_kg}, cargo_type={cargo_type}, error={str(e)}"
        )
        raise ValueError("Invalid cargo weight or type")


def calculate_addon_price(addon_type, quantity):
    try:
        quantity = int(quantity)
        if quantity < 0:
            raise ValueError("Quantity cannot be negative")
        if addon_type not in dict(AddOn.ADD_ON_TYPE_CHOICES).keys():
            raise ValueError(f"Invalid add-on type: {addon_type}")
        prices = {
            'premium_seating': Decimal('20.00'),
            'priority_boarding': Decimal('10.00'),
            'cabin': Decimal('50.00'),
            'meal_breakfast': Decimal('15.00'),
            'meal_lunch': Decimal('15.00'),
            'meal_dinner': Decimal('15.00'),
            'meal_snack': Decimal('5.00')
        }
        return prices.get(addon_type, Decimal('0.00')) * Decimal(quantity)
    except (ValueError, TypeError) as e:
        logger.error(f"Invalid addon quantity: addon_type={addon_type}, quantity={quantity}, error={str(e)}")
        raise ValueError("Invalid addon quantity")


def calculate_passenger_price(adults, children, infants, schedule):
    base_fare = schedule.route.base_fare or Decimal('35.50')
    return (
        Decimal(adults) * base_fare +
        Decimal(children) * base_fare * Decimal('0.5') +
        Decimal(infants) * base_fare * Decimal('0.1')
    )


def calculate_vehicle_price(vehicle_type, dimensions):
    try:
        # Example pricing logic based on vehicle type and dimensions
        base_price = Decimal('50.00')  # Base price for vehicles
        type_multiplier = {
            'car': Decimal('1.0'),
            'sedan': Decimal('1.0'),
            'truck': Decimal('1.5'),
            'van': Decimal('1.5'),
            'motorcycle': Decimal('0.5')
        }
        multiplier = type_multiplier.get(vehicle_type.lower(), Decimal('1.0'))

        # Optional: Adjust price based on dimensions (e.g., LxWxH in cm)
        if dimensions and re.match(r'^\d+x\d+x\d+$', dimensions):
            length, width, height = map(int, dimensions.split('x'))
            volume = length * width * height / 1_000_000  # Convert to cubic meters
            volume_surcharge = Decimal(volume) * Decimal('10.00')  # $10 per cubic meter
        else:
            volume_surcharge = Decimal('0.00')

        return base_price * multiplier + volume_surcharge
    except (ValueError, TypeError) as e:
        logger.error(f"Invalid vehicle data: vehicle_type={vehicle_type}, dimensions={dimensions}, error={str(e)}")
        raise ValueError("Invalid vehicle type or dimensions")


def calculate_total_price(adults, children, infants, schedule, add_cargo, cargo_type, weight_kg, addons,
                          add_vehicle=False, vehicle_type=None, vehicle_dimensions=None):
    passenger_price = calculate_passenger_price(adults, children, infants, schedule)
    cargo_price = calculate_cargo_price(weight_kg, cargo_type) if add_cargo and cargo_type and weight_kg else Decimal(
        '0.00')
    vehicle_price = calculate_vehicle_price(vehicle_type,
                                            vehicle_dimensions) if add_vehicle and vehicle_type and vehicle_dimensions else Decimal(
        '0.00')
    addon_price = sum(calculate_addon_price(addon['type'], addon['quantity']) for addon in addons)
    return passenger_price + cargo_price + vehicle_price + addon_price


def routes_api(request):
    try:
        # Only pull upcoming/active schedules into memory
        upcoming = Prefetch(
            'bookings',  # <— current related_name
            queryset=Schedule.objects.filter(status='scheduled').order_by('departure_time'),
            to_attr='prefetched_schedules'
        )

        routes = (
            Route.objects
                 .select_related('departure_port', 'destination_port')
                 .prefetch_related(upcoming)
        )

        routes_data = []
        for route in routes:
            # get the first upcoming schedule id (if any)
            first_schedule_id = (
                route.prefetched_schedules[0].id if getattr(route, 'prefetched_schedules', []) else None
            )

            routes_data.append({
                'id': route.id,
                'departure_port': {
                    'name': route.departure_port.name,
                    'lat': route.departure_port.lat,
                    'lng': route.departure_port.lng
                },
                'destination_port': {
                    'name': route.destination_port.name,
                    'lat': route.destination_port.lat,
                    'lng': route.destination_port.lng
                },
                'distance_km': float(route.distance_km) if route.distance_km else None,
                'estimated_duration': int(route.estimated_duration.total_seconds() / 60) if route.estimated_duration else None,
                'base_fare': float(route.base_fare) if route.base_fare else None,
                'schedule_id': first_schedule_id,
                'waypoints': route.waypoints or [
                    [route.departure_port.lat, route.departure_port.lng],
                    [route.destination_port.lat, route.destination_port.lng]
                ]
            })

        return JsonResponse({'routes': routes_data})
    except Exception as e:
        logger.error(f"Routes API error: {e}")
        return JsonResponse({'error': str(e)}, status=500)


@login_required
def profile(request):
    return render(request, "bookings/profile.html")


def terms_of_service(request):
    return render(request, "terms_of_service.html")


@require_GET
def homepage(request):
    now = timezone.now()
    schedules = Schedule.objects.filter(
        status='scheduled',
        departure_time__gt=now
    ).select_related('ferry', 'route__departure_port', 'route__destination_port').order_by('departure_time')

    route_input = request.GET.get('route', '').strip().lower()
    travel_date = request.GET.get('date', '').strip()
    passengers = request.GET.get('passengers', '1')

    logger.debug(f"Search parameters: route={route_input}, travel_date={travel_date}, passengers={passengers}")

    # --- Route filtering ---
    if route_input:
        try:
            origin, destination = route_input.split('-to-')
            schedules = schedules.filter(
                route__departure_port__name__iexact=origin.strip(),
                route__destination_port__name__iexact=destination.strip()
            )
        except ValueError:
            messages.error(request, "Invalid route format. Use 'origin-to-destination' (e.g., nadi-to-suva).")

    # --- Date filtering ---
    if travel_date:
        try:
            travel_date_obj = datetime.datetime.strptime(travel_date, '%Y-%m-%d')
            travel_date_start = timezone.make_aware(travel_date_obj)
            travel_date_end = travel_date_start + datetime.timedelta(days=1)
            schedules = schedules.filter(
                departure_time__range=(travel_date_start, travel_date_end)
            )
        except ValueError:
            messages.error(request, "Invalid date format. Please use YYYY-MM-DD.")

    # --- Routes queryset ---
    routes = Route.objects.select_related('departure_port', 'destination_port').all()

    # --- Next Departure Info ---
    next_departure = schedules.first()
    next_departure_info = None
    if next_departure:
        next_departure_info = {
            'time': next_departure.departure_time.strftime('%a, %b %d, %H:%M'),
            'route': f"{next_departure.route.departure_port.name} to {next_departure.route.destination_port.name}",
            'schedule_id': next_departure.id,
            'estimated_duration': int(
                next_departure.route.estimated_duration.total_seconds() / 60) if next_departure.route.estimated_duration else None
        }
    else:
        next_departure_info = {
            'time': '10:00 AM',
            'route': 'Nadi to Suva',
            'schedule_id': 1,
            'estimated_duration': 240
        }

    # --- Default Weather Data (fallback) ---
    weather_data = {
        'current': {
            'temp': 28,
            'condition': 'Sunny',
            'humidity': 65,
            'wind': 12
        },
        'forecast': [
            {'date': 'Tomorrow', 'temp': 29, 'condition': 'Partly Cloudy'},
            {'date': 'Friday', 'temp': 27, 'condition': 'Sunny'}
        ],
        'ports': {
            'nadi': {'temp': 28, 'condition': 'Sunny', 'wind': 10, 'precip': 0},
            'suva': {'temp': 26, 'condition': 'Partly Cloudy', 'wind': 8, 'precip': 5},
            'denarau': {'temp': 28, 'condition': 'Sunny', 'wind': 10, 'precip': 0},
            'yasawa': {'temp': 27, 'condition': 'Clear', 'wind': 12, 'precip': 2}
        }
    }

    # --- Real Weather Overrides (if available) ---
    try:
        current_conditions = WeatherCondition.objects.filter(
            expires_at__gt=now
        ).order_by('-updated_at').first()

        if current_conditions:
            weather_data['current'] = {
                'temp': float(current_conditions.temperature) if current_conditions.temperature else 28,
                'condition': current_conditions.condition or 'Sunny',
                'humidity': int(current_conditions.humidity) if current_conditions.humidity else 65,
                'wind': float(current_conditions.wind_speed) if current_conditions.wind_speed else 12
            }

        port_weather = WeatherCondition.objects.filter(
            port__name__in=['Nadi', 'Suva', 'Denarau', 'Yasawa'],
            expires_at__gt=now
        ).select_related('port').order_by('port__name', '-updated_at')

        port_data = {}
        for pw in port_weather:
            port_key = pw.port.name.lower()
            port_data[port_key] = {
                'temp': float(pw.temperature) if pw.temperature else 28,
                'condition': pw.condition or 'Sunny',
                'wind': float(pw.wind_speed) if pw.wind_speed else 10,
                'precip': int(pw.precipitation_probability) if pw.precipitation_probability else 0
            }

        weather_data['ports'].update(port_data)

        weather_data['forecast'] = [
            {'date': 'Tomorrow', 'temp': 29, 'condition': 'Partly Cloudy'},
            {'date': 'Friday', 'temp': 27, 'condition': 'Sunny'}
        ]
    except Exception as e:
        logger.error(f"Weather data fetch error: {e}")

    # --- Schedule-specific Weather ---
    schedule_weather_data = []
    schedule_route_ids = schedules.values_list('route_id', flat=True).distinct()

    if schedule_route_ids:
        try:
            latest_conditions_subquery = WeatherCondition.objects.filter(
                route_id=OuterRef('route_id'),
                expires_at__gt=now
            ).values('route_id').annotate(
                latest_updated=Max('updated_at')
            ).values('latest_updated')

            latest_conditions = WeatherCondition.objects.filter(
                route_id__in=schedule_route_ids,
                expires_at__gt=now,
                updated_at__in=Subquery(latest_conditions_subquery)
            ).select_related('route', 'port')

            latest_per_route = {wc.route_id: wc for wc in latest_conditions}

            for schedule in schedules:
                wc = latest_per_route.get(schedule.route_id)
                if wc and not wc.is_expired():
                    schedule_weather_data.append({
                        'route_id': schedule.route_id,
                        'schedule_id': schedule.id,
                        'port': wc.port.name,
                        'condition': wc.condition,
                        'temperature': float(wc.temperature) if wc.temperature is not None else None,
                        'wind_speed': float(wc.wind_speed) if wc.wind_speed is not None else None,
                        'precipitation_probability': float(wc.precipitation_probability) if wc.precipitation_probability is not None else None,
                        'expires_at': wc.expires_at.isoformat() if wc.expires_at else None,
                        'updated_at': wc.updated_at.isoformat() if wc.updated_at else None,
                        'is_expired': False,
                        'error': None
                    })
                else:
                    schedule_weather_data.append({
                        'route_id': schedule.route_id,
                        'schedule_id': schedule.id,
                        'port': schedule.route.departure_port.name,
                        'condition': None,
                        'temperature': None,
                        'wind_speed': None,
                        'precipitation_probability': None,
                        'expires_at': None,
                        'updated_at': None,
                        'is_expired': True,
                        'error': 'No valid weather data available'
                    })
        except Exception as e:
            logger.error(f"Schedule weather data error: {e}")
            for schedule in schedules:
                schedule_weather_data.append({
                    'route_id': schedule.route_id,
                    'schedule_id': schedule.id,
                    'port': schedule.route.departure_port.name,
                    'condition': 'Partly Cloudy',
                    'temperature': 28,
                    'wind_speed': 12,
                    'precipitation_probability': 5,
                    'is_expired': False,
                    'error': None
                })

    # --- Pagination ---
    total_schedules_count = schedules.count()
    displayed_schedules = schedules[:12]
    remaining_schedules = max(0, total_schedules_count - len(displayed_schedules))

    # --- Safe JSON route serialization ---
    routes_data = list(routes.values(
        'id',
        'departure_port__name',
        'destination_port__name',
        'distance_km',
        'estimated_duration',
        'base_fare',
        'service_tier',
        'min_weekly_services',
        'preferred_departure_windows',
        'safety_buffer_minutes',
        'waypoints'
    )[:10])

    if not routes_data:
        routes_data = [
            {
                'departure_port__name': 'Nadi',
                'destination_port__name': 'Suva',
                'base_fare': 50,
            },
            {
                'departure_port__name': 'Denarau',
                'destination_port__name': 'Yasawa Islands',
                'base_fare': 100,
            },
            {
                'departure_port__name': 'Suva',
                'destination_port__name': 'Lautoka',
                'base_fare': 40,
            },
        ]

    # --- Context ---
    context = {
        'bookings': displayed_schedules,
        'total_schedules': total_schedules_count,
        'remaining_schedules': remaining_schedules,
        'routes': routes_data,
        'form_data': {
            'route': route_input,
            'date': travel_date or now.date().strftime('%Y-%m-%d'),
            'passengers': passengers
        },
        'weather_data': weather_data,
        'schedule_weather_data': schedule_weather_data,
        'next_departure': next_departure_info,
        'today': now.date(),
        'tile_error_url': '/static/images/tile-error.png'
    }

    return render(request, 'home.html', context)


@login_required_allow_anonymous
def booking_history(request):
    logger.debug(
        f"Fetching booking history for user={request.user if request.user.is_authenticated else 'Guest'}, "
        f"session_guest_email={request.session.get('guest_email')}, "
        f"session_keys={list(request.session.keys())}"
    )

    if request.method == 'POST':
        guest_email = request.POST.get('guest_email', '').strip()
        if guest_email:
            request.session['guest_email'] = guest_email
            logger.debug(f"Set guest_email in session: {guest_email}")

    if request.user.is_authenticated:
        bookings = Booking.objects.filter(user=request.user).select_related('schedule__ferry', 'schedule__route').order_by('-booking_date')
    else:
        guest_email = request.session.get('guest_email')
        bookings = Booking.objects.filter(guest_email=guest_email).select_related('schedule__ferry', 'schedule__route').order_by('-booking_date') if guest_email else []

    for booking in bookings:
        booking.update_status_if_expired()

    return render(request, 'bookings/history.html', {
        'bookings': bookings,
        'cutoff_time': timezone.now() + datetime.timedelta(hours=6),
        'is_guest': not request.user.is_authenticated
    })


def generate_ticket(request, booking_id):
    booking = get_object_or_404(Booking, id=booking_id, user=request.user)
    if booking.status != 'confirmed':
        messages.error(request, "Tickets can only be generated for confirmed bookings.")
        return redirect('bookings:booking_history')

    for passenger in booking.passengers.all():
        if not Ticket.objects.filter(booking=booking, passenger=passenger).exists():
            ticket = Ticket.objects.create(
                booking=booking,
                passenger=passenger,
                ticket_status='active',
                qr_token=uuid.uuid4().hex
            )
            qr_data = request.build_absolute_uri(reverse('bookings:view_ticket', args=[ticket.qr_token]))
            qr = qrcode.QRCode()
            qr.add_data(qr_data)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buffer = BytesIO()
            img.save(buffer, format='PNG')
            ticket.qr_code.save(f"ticket_{ticket.id}.png", ContentFile(buffer.getvalue()))

    messages.success(request, f"Tickets generated for Booking #{booking.id}.")
    return redirect('bookings:view_tickets', booking_id=booking.id)


@login_required
def view_cargo(request, cargo_id):
    cargo = get_object_or_404(Cargo, id=cargo_id, booking__user=request.user)
    return render(request, 'bookings/view_cargo.html', {'cargo': cargo})


@login_required_allow_anonymous
def view_ticket(request, qr_token):
    try:
        ticket = Ticket.objects.select_related('booking__schedule__ferry', 'booking__schedule__route', 'passenger').get(qr_token=qr_token)
    except Ticket.DoesNotExist:
        messages.error(request, "Invalid or expired ticket link.")
        return redirect('bookings:booking_history')
    if request.user.is_authenticated and ticket.booking.user != request.user:
        return HttpResponseForbidden("You are not authorized to view this ticket.")
    if not request.user.is_authenticated and ticket.booking.guest_email != request.session.get('guest_email'):
        return HttpResponseForbidden("You are not authorized to view this ticket.")
    return render(request, 'bookings/view_ticket.html', {'ticket': ticket})


def get_schedule_updates(request):
    schedules = Schedule.objects.filter(
        departure_time__gte=timezone.now(),
        status='scheduled',
        available_seats__gt=0
    ).select_related('route__departure_port', 'route__destination_port', 'ferry').order_by('departure_time')

    data = [
        {
            'id': s.id,
            'route': f"{s.route.departure_port.name} to {s.route.destination_port.name}",
            'departure_time': s.departure_time.isoformat(),
            'available_seats': s.available_seats,
            'ferry_name': s.ferry.name
        } for s in schedules
    ]

    return JsonResponse({'schedules': data})


@require_GET
def homepage_schedules_api(request):
    """Return the latest schedules for the homepage with lightweight metadata."""

    limit = safe_int(request.GET.get('limit', 12)) or 12
    limit = max(1, min(limit, 30))

    now = timezone.now()
    base_queryset = (
        Schedule.objects
        .filter(departure_time__gte=now, status__in=['scheduled', 'delayed', 'cancelled'])
        .select_related('ferry', 'route__departure_port', 'route__destination_port')
        .order_by('departure_time')
    )

    total_schedules = base_queryset.count()
    schedules = list(base_queryset[:limit])

    next_departure_schedule = next((s for s in schedules if s.status == 'scheduled' and s.available_seats > 0), None)
    if not next_departure_schedule:
        next_departure_schedule = base_queryset.filter(status='scheduled', available_seats__gt=0).first()

    schedule_payload = []
    hash_source = []

    for schedule in schedules:
        route = schedule.route
        departure_local = timezone.localtime(schedule.departure_time)

        try:
            duration_minutes = int(route.estimated_duration.total_seconds() // 60) if route.estimated_duration else None
        except (AttributeError, TypeError):
            duration_minutes = None

        schedule_payload.append({
            'id': schedule.id,
            'route': {
                'id': route.id,
                'departure': route.departure_port.name,
                'destination': route.destination_port.name,
                'base_fare': float(route.base_fare) if route.base_fare is not None else None,
                'estimated_duration_minutes': duration_minutes,
            },
            'ferry_name': schedule.ferry.name if schedule.ferry else '',
            'departure_time': schedule.departure_time.isoformat(),
            'departure_display': departure_local.strftime('%a, %b %d, %I:%M %p'),
            'departure_hour': departure_local.strftime('%H'),
            'available_seats': schedule.available_seats,
            'status': schedule.status,
            'status_display': schedule.get_status_display(),
            'bookable': schedule.status == 'scheduled' and schedule.available_seats > 0,
            'book_url': f"{reverse('bookings:book_ticket')}?schedule_id={schedule.id}",
        })

        hash_source.append({
            'id': schedule.id,
            'status': schedule.status,
            'available_seats': schedule.available_seats,
            'last_updated': schedule.last_updated.isoformat() if schedule.last_updated else None,
        })

    remaining = max(total_schedules - len(schedules), 0)

    next_departure_data = None
    if next_departure_schedule:
        next_departure_local = timezone.localtime(next_departure_schedule.departure_time)
        next_departure_data = {
            'schedule_id': next_departure_schedule.id,
            'route_display': f"{next_departure_schedule.route.departure_port.name} to {next_departure_schedule.route.destination_port.name}",
            'time_display': next_departure_local.strftime('%a, %b %d, %I:%M %p'),
            'departure_time': next_departure_schedule.departure_time.isoformat(),
            'book_url': f"{reverse('bookings:book_ticket')}?schedule_id={next_departure_schedule.id}",
        }

    schedules_hash = ''
    if hash_source:
        try:
            hash_payload = json.dumps(hash_source, sort_keys=True)
            schedules_hash = hashlib.md5(hash_payload.encode('utf-8')).hexdigest()
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.warning(f"Failed to compute schedule hash: {exc}")

    return JsonResponse({
        'schedules': schedule_payload,
        'total_schedules': total_schedules,
        'remaining_schedules': remaining,
        'next_departure': next_departure_data,
        'schedules_hash': schedules_hash,
    })


@csrf_exempt
@require_POST
def get_pricing(request):
    """Fixed to handle individual form fields from JS"""
    try:
        schedule_id = request.POST.get('schedule_id')
        adults = safe_int(request.POST.get('adults', 0))
        children = safe_int(request.POST.get('children', 0))
        infants = safe_int(request.POST.get('infants', 0))

        # Handle individual cargo fields (not array notation)
        add_cargo = request.POST.get('add_cargo') == 'true' or request.POST.get('add_cargo') == 'on'
        cargo_type = request.POST.get('cargo_type', '')  # Individual field
        weight_kg = request.POST.get('cargo_weight_kg', '')
        cargo_license_plate = request.POST.get('cargo_license_plate', '')

        # Handle individual vehicle fields
        add_vehicle = request.POST.get('add_vehicle') == 'true' or request.POST.get('add_vehicle') == 'on'
        vehicle_type = request.POST.get('vehicle_type', '')
        vehicle_dimensions = request.POST.get('vehicle_dimensions', '')

        # Handle addons as individual quantity fields
        addons = []
        for addon_type in dict(AddOn.ADD_ON_TYPE_CHOICES).keys():
            quantity = safe_int(request.POST.get(f'{addon_type}_quantity', 0))
            if quantity > 0:
                addons.append({'type': addon_type, 'quantity': quantity})

        if not schedule_id:
            return JsonResponse({'error': 'Schedule ID required'}, status=400)

        schedule = get_object_or_404(Schedule, id=schedule_id, status='scheduled')

        weight = safe_float(weight_kg) or 0
        total_price = calculate_total_price(
            adults, children, infants, schedule, add_cargo, cargo_type, weight, addons,
            add_vehicle, vehicle_type, vehicle_dimensions
        )

        base_fare = schedule.route.base_fare or Decimal('35.50')
        breakdown = {
            'adults': str(Decimal(adults) * base_fare),
            'children': str(Decimal(children) * base_fare * Decimal('0.5')),
            'infants': str(Decimal(infants) * base_fare * Decimal('0.1')),
            'cargo': str(
                calculate_cargo_price(Decimal(weight), cargo_type) if add_cargo and weight > 0 else Decimal('0.00')),
            'vehicle': str(
                calculate_vehicle_price(vehicle_type, vehicle_dimensions) if add_vehicle else Decimal('0.00')),
            'addons': sum(calculate_addon_price(addon['type'], addon['quantity']) for addon in addons),
            'total': str(total_price)
        }

        return JsonResponse({
            'total_price': str(total_price),
            'breakdown': breakdown,
            'pricing': breakdown  # Consistent structure for JS
        })

    except Exception as e:
        logger.exception(f"Pricing error: {e}")
        return JsonResponse({'error': str(e)}, status=400)



def validate_passenger_data(request, p_type, index, adults, errors):
    first_name = request.POST.get(f'{p_type}_first_name_{index}', '').strip()
    last_name = request.POST.get(f'{p_type}_last_name_{index}', '').strip()
    age = request.POST.get(f'{p_type}_age_{index}', '').strip()
    dob = request.POST.get(f'{p_type}_dob_{index}', '').strip()
    linked_adult_index = request.POST.get(f'{p_type}_linked_adult_{index}', '').strip()
    document = request.FILES.get(f'{p_type}_id_document_{index}')

    # Mandatory fields
    if not first_name:
        errors.append({'field': f'{p_type}_first_name_{index}', 'message': f'{p_type.capitalize()} {index + 1}: First name is required.', 'step': 2})
    if not last_name:
        errors.append({'field': f'{p_type}_last_name_{index}', 'message': f'{p_type.capitalize()} {index + 1}: Last name is required.', 'step': 2})

    # Document validation for adults and children
    if p_type in ['adult', 'child']:
        if not document:
            errors.append({'field': f'{p_type}_id_document_{index}', 'message': f'{p_type.capitalize()} {index + 1}: ID document is required.', 'step': 2})
        else:
            try:
                FileExtensionValidator(allowed_extensions=['pdf', 'jpg', 'jpeg', 'png'])(document)
                if document.size > 2.5 * 1024 * 1024:  # 2.5MB
                    errors.append({'field': f'{p_type}_id_document_{index}', 'message': f'{p_type.capitalize()} {index + 1}: Document size must be less than 2.5MB.', 'step': 2})
            except ValidationError as e:
                errors.append({'field': f'{p_type}_id_document_{index}', 'message': f'{p_type.capitalize()} {index + 1}: Invalid file type. Please upload a PDF, JPG, or PNG.', 'step': 2})

    # Infant-specific validation
    if p_type == 'infant' and not dob:
        errors.append({'field': f'{p_type}_dob_{index}', 'message': f'Infant {index + 1}: Date of birth is required.', 'step': 2})

    # Linked adult validation for children and infants
    if p_type in ['child', 'infant']:
        if not linked_adult_index:
            errors.append({'field': f'{p_type}_linked_adult_{index}', 'message': f'{p_type.capitalize()} {index + 1}: Must be linked to an adult.', 'step': 2})
        else:
            try:
                linked_adult_index = int(linked_adult_index)
                if linked_adult_index < 0 or linked_adult_index >= adults:
                    errors.append({'field': f'{p_type}_linked_adult_{index}', 'message': f'{p_type.capitalize()} {index + 1}: Invalid linked adult.', 'step': 2})
            except (ValueError, TypeError):
                errors.append({'field': f'{p_type}_linked_adult_{index}', 'message': f'{p_type.capitalize()} {index + 1}: Invalid linked adult.', 'step': 2})

    # Age and DOB validation
    if p_type == 'infant' and dob:
        try:
            dob_date = datetime.datetime.strptime(dob, '%Y-%m-%d').date()
            age_days = (datetime.date.today() - dob_date).days
            if age_days > 730:  # 2 years
                errors.append({'field': f'{p_type}_dob_{index}', 'message': f'Infant {index + 1}: Must be under 2 years old.', 'step': 2})
        except ValueError:
            errors.append({'field': f'{p_type}_dob_{index}', 'message': f'Infant {index + 1}: Invalid date of birth.', 'step': 2})

    if p_type in ['adult', 'child'] and age:
        try:
            age = int(age)
            if p_type == 'child' and not (2 <= age <= 17):
                errors.append({'field': f'{p_type}_age_{index}', 'message': f'Child {index + 1}: Age must be 2-17.', 'step': 2})
            elif p_type == 'adult' and age < 18:
                errors.append({'field': f'{p_type}_age_{index}', 'message': f'Adult {index + 1}: Age must be 18 or older.', 'step': 2})
        except (ValueError, TypeError):
            errors.append({'field': f'{p_type}_age_{index}', 'message': f'{p_type.capitalize()} {index + 1}: Invalid age.', 'step': 2})

    return {
        'first_name': first_name,
        'last_name': last_name,
        'age': age,
        'dob': dob,
        'linked_adult_index': linked_adult_index,
        'document': document
    }


@require_POST
@csrf_protect
def validate_step(request):
    step = request.POST.get('step')
    errors = []

    if step == '1':
        schedule_id = request.POST.get('schedule_id', '').strip()
        guest_email = request.POST.get('guest_email', '').strip()
        is_authenticated = request.user.is_authenticated
        total_passengers = safe_int(request.POST.get('adults', '0')) + safe_int(request.POST.get('children', '0')) + safe_int(request.POST.get('infants', '0'))

        cache_key = f'schedule_exists_{schedule_id}'
        schedule_exists = cache.get(cache_key)
        if schedule_exists is None:
            try:
                schedule = Schedule.objects.get(id=schedule_id, status='scheduled', departure_time__gt=timezone.now())
                schedule_exists = True
                if total_passengers > schedule.available_seats:
                    errors.append({'field': 'schedule_id', 'message': f'Not enough seats available ({schedule.available_seats} remaining).'})
                cache.set(cache_key, schedule_exists, timeout=3600)
            except Schedule.DoesNotExist:
                schedule_exists = False
                errors.append({'field': 'schedule_id', 'message': 'Please select a valid ferry schedule.'})
                cache.set(cache_key, schedule_exists, timeout=3600)

        if not schedule_id or not schedule_exists:
            errors.append({'field': 'schedule_id', 'message': 'Please select a valid ferry schedule.'})

        if not is_authenticated:
            if not guest_email:
                errors.append({'field': 'guest_email', 'message': 'Guest email is required.'})
            elif not re.match(r'^[^\s@]+@[^\s@]+\.[^\s@]+$', guest_email):
                errors.append({'field': 'guest_email', 'message': 'Please enter a valid email address.'})

    elif step == '2':
        adults = safe_int(request.POST.get('adults', '0'))
        children = safe_int(request.POST.get('children', '0'))
        infants = safe_int(request.POST.get('infants', '0'))

        total_passengers = adults + children + infants
        if total_passengers == 0:
            errors.append({'field': 'general', 'message': 'At least one passenger is required.'})
        if (children > 0 or infants > 0) and adults == 0:
            errors.append({'field': 'general', 'message': 'Children and infants must be accompanied by an adult.'})

        for field, value in [('adults', adults), ('children', children), ('infants', infants)]:
            if value < 0:
                errors.append({'field': field, 'message': f'{field.capitalize()} count cannot be negative.'})

        for p_type in ['adult', 'child', 'infant']:
            count = adults if p_type == 'adult' else children if p_type == 'child' else infants
            for i in range(count):
                validate_passenger_data(request, p_type, i, adults, errors)

    elif step == '3':
        add_vehicle = request.POST.get('add_vehicle') == 'true' or request.POST.get('add_vehicle') == 'on'
        add_cargo = request.POST.get('add_cargo') == 'true' or request.POST.get('add_cargo') == 'on'

        # Validate vehicle fields
        if add_vehicle:
            vehicle_type = request.POST.get('vehicle_type', '').strip()
            vehicle_dimensions = request.POST.get('vehicle_dimensions', '').strip()
            if not vehicle_type:
                errors.append({'field': 'vehicle_type', 'message': 'Vehicle type is required.'})
            if not vehicle_dimensions or not re.match(r'^\d+x\d+x\d+$', vehicle_dimensions):
                errors.append({'field': 'vehicle_dimensions', 'message': 'Vehicle dimensions must be in format LxWxH (e.g., 400x180x150).'})

        # Validate cargo fields
        if add_cargo:
            cargo_type = request.POST.get('cargo_type', '').strip()
            cargo_weight = request.POST.get('cargo_weight_kg', '').strip()
            cargo_dimensions = request.POST.get('cargo_dimensions_cm', '').strip()
            if not cargo_type:
                errors.append({'field': 'cargo_type', 'message': 'Cargo type is required.'})
            try:
                weight = float(cargo_weight)
                if weight <= 0:
                    errors.append({'field': 'cargo_weight_kg', 'message': 'Cargo weight must be a positive number.'})
            except ValueError:
                errors.append({'field': 'cargo_weight_kg', 'message': 'Cargo weight must be a valid number.'})
            if not cargo_dimensions or not re.match(r'^\d+x\d+x\d+$', cargo_dimensions):
                errors.append({'field': 'cargo_dimensions_cm', 'message': 'Cargo dimensions must be in format LxWxH (e.g., 400x180x150).'})

    elif step == '4':
        if not request.POST.get('privacy_consent'):
            errors.append({'field': 'privacy_consent', 'message': 'You must agree to the privacy policy.'})

    if errors:
        return JsonResponse({'valid': False, 'errors': errors, 'step': step}, status=400)
    return JsonResponse({'valid': True, 'step': step})


@csrf_exempt  # Add this decorator
@require_POST
def validate_file(request):
    """Fixed with proper AJAX check"""
    if request.method != 'POST':
        return JsonResponse({'valid': False, 'error': 'POST required'}, status=405)

    # Verify AJAX request
    if not request.META.get('HTTP_X_REQUESTED_WITH') == 'XMLHttpRequest':
        return JsonResponse({'valid': False, 'error': 'AJAX required'}, status=403)

    file = request.FILES.get('file')
    if not file:
        return JsonResponse({'valid': False, 'error': 'No file provided'}, status=400)

    try:
        FileExtensionValidator(allowed_extensions=['pdf', 'jpg', 'jpeg', 'png'])(file)
        if file.size > 2621440:  # 2.5MB
            return JsonResponse({'valid': False, 'error': 'File too large (2.5MB max)'}, status=413)

        # Basic verification (replace with OCR/service later)
        verification_status = 'verified'

        return JsonResponse({
            'valid': True,
            'file_name': file.name,
            'verification_status': verification_status
        })

    except ValidationError as e:
        return JsonResponse({'valid': False, 'error': str(e)}, status=400)
    except Exception as e:
        logger.error(f"File validation error: {e}")
        return JsonResponse({'valid': False, 'error': 'Validation failed'}, status=500)


@require_POST
@csrf_exempt  # Add for AJAX
def check_schedule_availability(request):
    if not request.META.get('HTTP_X_REQUESTED_WITH') == 'XMLHttpRequest':
        return JsonResponse({'valid': False, 'error': 'AJAX required'}, status=403)

    try:
        schedule_id = request.POST.get('schedule_id')
        adults = safe_int(request.POST.get('adults', 0))
        children = safe_int(request.POST.get('children', 0))
        infants = safe_int(request.POST.get('infants', 0))
        total_passengers = adults + children + infants

        if not schedule_id or total_passengers <= 0:
            return JsonResponse({'valid': False, 'error': 'Invalid parameters'}, status=400)

        schedule = get_object_or_404(
            Schedule,
            id=schedule_id,
            status='scheduled',
            departure_time__gt=timezone.now()
        )

        if schedule.available_seats < total_passengers:
            return JsonResponse({
                'valid': False,
                'error': f'Only {schedule.available_seats} seats available'
            }, status=400)

        return JsonResponse({
            'valid': True,
            'schedule': {
                'id': schedule.id,
                'route': {
                    'departure_port': {'name': schedule.route.departure_port.name},
                    'destination_port': {'name': schedule.route.destination_port.name},
                    'base_fare': str(schedule.route.base_fare or Decimal('35.50'))
                },
                'departure_time': schedule.departure_time.isoformat(),
                'available_seats': schedule.available_seats
            }
        })

    except Exception as e:
        logger.error(f"Availability check failed: {e}")
        return JsonResponse({'valid': False, 'error': 'Server error'}, status=500)


@require_GET
def api_bookings(request):
    """
    API: /bookings/api/bookings/?route=...&date=...
    Returns filtered schedules with full route name.
    """
    route_param = request.GET.get('route', '').strip()
    date_str = request.GET.get('date')

    logger.debug(f"[api_bookings] route_param={route_param}, date_str={date_str}")

    # Base queryset
    qs = Schedule.objects.select_related(
        'ferry', 'route', 'route__departure_port', 'route__destination_port'
    )

    # Filter by date
    if date_str:
        try:
            target_date = timezone.datetime.strptime(date_str, '%Y-%m-%d').date()
            qs = qs.filter(departure_time__date=target_date)
        except ValueError:
            logger.warning(f"Invalid date format: {date_str}")
            return JsonResponse({"schedules": []})
    else:
        # Default: today
        qs = qs.filter(departure_time__date=timezone.now().date())

    # Filter by route
    if route_param:
        # Normalize: handle URL encoding
        route_clean = route_param.replace('+', ' ').strip()

        # Try exact match first
        qs = qs.filter(
            Q(route__departure_port__name__iexact=route_clean.split(' to ')[0].strip()) |
            Q(route__name__iexact=route_clean) |
            Q(route__slug__iexact=route_clean.replace(' ', '-').lower())
        )

        # Fallback: partial match
        if not qs.exists():
            departure = route_clean.split(' to ')[0].strip()
            qs = Schedule.objects.select_related(
                'ferry', 'route', 'route__departure_port', 'route__destination_port'
            ).filter(
                route__departure_port__name__icontains=departure,
                departure_time__date=target_date if date_str else timezone.now().date()
            )

    # Final filter: only scheduled + has seats
    qs = qs.filter(status='scheduled', available_seats__gt=0).order_by('departure_time')

    schedules = []
    for s in qs:
        route_name = f"{s.route.departure_port.name} to {s.route.destination_port.name}"
        schedules.append({
            "id": s.id,
            "ferry_name": s.ferry.name,
            "departure_time": s.departure_time.isoformat(),
            "available_seats": s.available_seats,
            "status": s.status,
            "route": route_name
        })

    logger.debug(f"[api_bookings] Found {len(schedules)} schedules")
    return JsonResponse({"schedules": schedules})


@require_POST
@csrf_protect
def create_checkout_session(request):
    """Create Stripe checkout session for ferry booking, including passengers, cargo, vehicles, and addons"""
    errors = []

    try:
        # --- Extract booking data ---
        schedule_id = request.POST.get('schedule_id')
        adults = safe_int(request.POST.get('adults', 0))
        children = safe_int(request.POST.get('children', 0))
        infants = safe_int(request.POST.get('infants', 0))
        guest_email = request.POST.get('guest_email', '').strip()

        add_cargo = request.POST.get('add_cargo') in ['true', 'on']
        cargo_type = request.POST.get('cargo_type', '')
        weight_kg = safe_float(request.POST.get('cargo_weight_kg', 0))
        cargo_license_plate = request.POST.get('cargo_license_plate', '')

        add_vehicle = request.POST.get('add_vehicle') in ['true', 'on']
        vehicle_type = request.POST.get('vehicle_type', '')
        vehicle_dimensions = request.POST.get('vehicle_dimensions', '')
        vehicle_license_plate = request.POST.get('vehicle_license_plate', '')

        # --- Addons ---
        addons = []
        for addon_type in dict(AddOn.ADD_ON_TYPE_CHOICES).keys():
            quantity = safe_int(request.POST.get(f'{addon_type}_quantity', 0))
            if quantity > 0:
                addons.append({'type': addon_type, 'quantity': quantity})

        total_passengers = adults + children + infants
        if not schedule_id or total_passengers == 0:
            return JsonResponse({'success': False, 'errors': [{'field': 'general', 'message': 'Invalid booking data'}]}, status=400)

        schedule = get_object_or_404(Schedule, id=schedule_id, status='scheduled')
        if schedule.available_seats < total_passengers:
            return JsonResponse({'success': False, 'errors': [{'field': 'schedule_id', 'message': f'Only {schedule.available_seats} seats available'}]}, status=400)

        # --- Validate email ---
        customer_email = request.user.email if request.user.is_authenticated else guest_email
        if not customer_email or not re.match(r'^[^\s@]+@[^\s@]+\.[^\s@]+$', customer_email):
            return JsonResponse({'success': False, 'errors': [{'field': 'email', 'message': 'Valid email required'}]}, status=400)

        # --- Calculate total price ---
        total_price = calculate_total_price(
            adults, children, infants, schedule, add_cargo, cargo_type, weight_kg, addons,
            add_vehicle, vehicle_type, vehicle_dimensions
        )

        # --- Create booking ---
        booking = Booking.objects.create(
            user=request.user if request.user.is_authenticated else None,
            schedule=schedule,
            guest_email=guest_email if not request.user.is_authenticated else None,
            passenger_adults=adults,
            passenger_children=children,
            passenger_infants=infants,
            total_price=total_price,
            status='pending'
        )

        # --- Create passengers ---
        passenger_lists = {'adult': adults, 'child': children, 'infant': infants}
        adult_passengers = []

        for p_type, count in passenger_lists.items():
            for i in range(count):
                first_name = request.POST.get(f'{p_type}_first_name_{i}', '').strip()
                last_name = request.POST.get(f'{p_type}_last_name_{i}', '').strip()

                if not first_name or not last_name:
                    raise ValueError(f"{p_type.capitalize()} {i + 1} missing name")

                passenger_data = {
                    'booking': booking,
                    'first_name': first_name,
                    'last_name': last_name,
                    'passenger_type': p_type
                }

                if p_type != 'infant':
                    age = request.POST.get(f'{p_type}_age_{i}')
                    if age:
                        passenger_data['age'] = int(age)

                if p_type == 'infant':
                    dob = request.POST.get(f'{p_type}_dob_{i}')
                    if dob:
                        passenger_data['date_of_birth'] = datetime.datetime.strptime(dob, '%Y-%m-%d').date()

                passenger = Passenger.objects.create(**passenger_data)

                # Link child/infant to adult
                if p_type in ['child', 'infant']:
                    linked_idx = request.POST.get(f'{p_type}_linked_adult_{i}')
                    if linked_idx and adult_passengers:
                        try:
                            passenger.linked_adult = adult_passengers[int(linked_idx)]
                            passenger.save()
                        except (IndexError, ValueError):
                            pass

                if p_type == 'adult':
                    adult_passengers.append(passenger)

        # --- Create cargo/vehicle/addons ---
        if add_cargo and weight_kg > 0:
            Cargo.objects.create(
                booking=booking,
                cargo_type=cargo_type,
                weight_kg=Decimal(weight_kg),
                license_plate=cargo_license_plate,
                price=calculate_cargo_price(Decimal(weight_kg), cargo_type)
            )

        if add_vehicle:
            Vehicle.objects.create(
                booking=booking,
                vehicle_type=vehicle_type,
                dimensions=vehicle_dimensions,
                license_plate=vehicle_license_plate,
                price=calculate_vehicle_price(vehicle_type, vehicle_dimensions)
            )

        for addon in addons:
            AddOn.objects.create(
                booking=booking,
                add_on_type=addon['type'],
                quantity=addon['quantity'],
                price=calculate_addon_price(addon['type'], addon['quantity'])
            )

        # --- Reserve seats ---
        schedule.available_seats -= total_passengers
        schedule.save()

        # --- Create Stripe session ---
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'fjd',
                    'product_data': {'name': f'Ferry Booking #{booking.id}'},
                    'unit_amount': int(total_price * 100),
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=request.build_absolute_uri('/bookings/success/?session_id={CHECKOUT_SESSION_ID}'),
            cancel_url=request.build_absolute_uri('/bookings/cancel/'),
            metadata={'booking_id': str(booking.id), 'guest_email': guest_email or ''},
            customer_email=customer_email,
        )

        booking.stripe_session_id = session.id
        booking.save()

        request.session['booking_id'] = booking.id
        request.session['stripe_session_id'] = session.id

        return JsonResponse({'sessionId': session.id})

    except Exception as e:
        logger.exception(f"Checkout error: {e}")
        # Cleanup on error
        if 'booking' in locals():
            booking.delete()
            if 'schedule' in locals():
                schedule.available_seats += total_passengers
                schedule.save()
        return JsonResponse({'success': False, 'errors': [{'field': 'general', 'message': str(e)}]}, status=400)


def login_required_allow_anonymous(view_func):
    def wrapper(request, *args, **kwargs):
        return view_func(request, *args, **kwargs)
    return wrapper


@login_required_allow_anonymous
def book_ticket(request):
    schedule_id = request.GET.get('schedule_id', '').strip()
    to_port = request.GET.get('to_port', '').strip().lower()
    step = safe_int(request.GET.get('step', 1))

    # Query bookings for GET requests
    available_schedules = Schedule.objects.filter(
        status='scheduled',
        departure_time__gt=timezone.now()
    ).select_related('ferry', 'route__departure_port', 'route__destination_port')

    if schedule_id:
        try:
            available_schedules = available_schedules.filter(id=schedule_id)
            if not available_schedules.exists():
                logger.warning(f"No schedule found for schedule_id={schedule_id}")
                messages.error(request, "Selected schedule is not available.")
        except ValueError:
            logger.error(f"Invalid schedule_id={schedule_id}")
            messages.error(request, "Invalid schedule ID.")
            available_schedules = Schedule.objects.none()

    if to_port:
        available_schedules = available_schedules.filter(
            route__destination_port__name__iexact=to_port
        )
        if not available_schedules.exists():
            logger.warning(f"No bookings found for to_port={to_port}")
            messages.error(request, f"No bookings available for destination: {to_port.capitalize()}.")

    # Define add-ons
    add_ons = [
        {'id': 'premium_seating', 'label': 'Premium Seating', 'price': 20.00, 'max_quantity': 20},
        {'id': 'priority_boarding', 'label': 'Priority Boarding', 'price': 10.00, 'max_quantity': 20},
        {'id': 'cabin', 'label': 'Cabin', 'price': 50.00, 'max_quantity': 5},
        {'id': 'meal_breakfast', 'label': 'Breakfast', 'price': 15.00, 'max_quantity': 50},
        {'id': 'meal_lunch', 'label': 'Lunch', 'price': 15.00, 'max_quantity': 50},
        {'id': 'meal_dinner', 'label': 'Dinner', 'price': 15.00, 'max_quantity': 50},
        {'id': 'meal_snack', 'label': 'Snack', 'price': 5.00, 'max_quantity': 100},
    ]

    if request.method == 'GET':
        # Initialize form data for rendering
        form_data = {
            'step': step,
            'schedule_id': schedule_id or '',
            'adults': 1,
            'children': 0,
            'infants': 0,
            'guest_email': request.session.get('guest_email', ''),
            'add_vehicle': False,
            'add_cargo': False,
            'vehicle_type': '',
            'vehicle_dimensions': '',
            'vehicle_license_plate': '',
            'cargo_type': '',
            'cargo_weight_kg': '',
            'cargo_dimensions_cm': '',
            'cargo_license_plate': '',
            'privacy_consent': False,
            'to_port': to_port or '',
            **{f'{addon["id"]}_quantity': 0 for addon in add_ons}
        }

        # Load saved passenger data from session
        saved_passenger_data = request.session.get('passenger_data', {})
        for p_type in ['adult', 'child', 'infant']:
            count_key = 'children' if p_type == 'child' else f'{p_type}s'
            count = form_data.get(count_key, 0)
            for i in range(count):
                form_data.update({
                    f'{p_type}_first_name_{i}': saved_passenger_data.get(f'{p_type}_first_name_{i}', ''),
                    f'{p_type}_last_name_{i}': saved_passenger_data.get(f'{p_type}_last_name_{i}', ''),
                    f'{p_type}_age_{i}': saved_passenger_data.get(f'{p_type}_age_{i}', ''),
                    f'{p_type}_dob_{i}': saved_passenger_data.get(f'{p_type}_dob_{i}', ''),
                    f'{p_type}_linked_adult_{i}': saved_passenger_data.get(f'{p_type}_linked_adult_{i}', '')
                })

        # Generate summary for step 4 if schedule selected
        summary = None
        if step == 4 and schedule_id:
            try:
                schedule = Schedule.objects.get(
                    id=schedule_id,
                    status='scheduled',
                    departure_time__gt=timezone.now()
                )
                adults = safe_int(form_data['adults'])
                children = safe_int(form_data['children'])
                infants = safe_int(form_data['infants'])

                # Use individual fields for consistency
                add_vehicle = form_data['add_vehicle']
                add_cargo = form_data['add_cargo']
                vehicle_type = form_data['vehicle_type']
                vehicle_dimensions = form_data['vehicle_dimensions']
                cargo_type = form_data['cargo_type']
                cargo_weight_kg = safe_float(form_data['cargo_weight_kg'])

                # Calculate addons from form data
                addons = []
                for addon in add_ons:
                    quantity = safe_int(form_data.get(f'{addon["id"]}_quantity', 0))
                    if quantity > 0:
                        addons.append({'type': addon['id'], 'quantity': quantity})

                total_price = calculate_total_price(
                    adults, children, infants, schedule, add_cargo, cargo_type,
                    cargo_weight_kg, addons, add_vehicle, vehicle_type, vehicle_dimensions
                )

                base_fare = schedule.route.base_fare or Decimal('35.50')
                summary = {
                    'schedule': {
                        'route': f"{schedule.route.departure_port.name} to {schedule.route.destination_port.name}",
                        'departure_time': schedule.departure_time.strftime("%a, %b %d, %H:%M"),
                        'estimated_duration': int(
                            schedule.route.estimated_duration.total_seconds() / 60) if schedule.route.estimated_duration else "N/A"
                    },
                    'pricing': {
                        'adults': str(Decimal(adults) * base_fare),
                        'children': str(Decimal(children) * base_fare * Decimal('0.5')),
                        'infants': str(Decimal(infants) * base_fare * Decimal('0.1')),
                        'vehicle': str(
                            calculate_vehicle_price(vehicle_type, vehicle_dimensions)) if add_vehicle else "0.00",
                        'cargo': str(
                            calculate_cargo_price(Decimal(cargo_weight_kg or 0), cargo_type)) if add_cargo else "0.00",
                        'addons': {
                                    addon['type']: {
                                        'label': next(
                                            (a['label'] for a in add_ons if a['id'] == addon['type']),
                                            addon['type'].replace('_', ' ').title()
                                        ),
                                        'quantity': addon['quantity'],
                                        'amount': str(calculate_addon_price(addon['type'], addon['quantity']))
                                    }
                                    for addon in addons
                                },
                        'total': str(total_price)
                    },
                    'total_price': str(total_price)
                }
            except Schedule.DoesNotExist:
                messages.error(request, "Selected schedule is not available.")
                summary = None

        return render(request, 'bookings/book.html', {
            'bookings': available_schedules,
            'user': request.user,
            'form_data': form_data,
            'debug': settings.DEBUG,
            'stripe_publishable_key': settings.STRIPE_PUBLISHABLE_KEY,
            'summary': summary,
            'add_ons': add_ons
        })

    # POST handling - now simplified to validation and redirect to checkout
    if request.method == 'POST':
        step = request.POST.get('step')

        # Basic validation
        schedule_id = request.POST.get('schedule_id', '').strip()
        adults = safe_int(request.POST.get('adults', 0))
        children = safe_int(request.POST.get('children', 0))
        infants = safe_int(request.POST.get('infants', 0))
        total_passengers = adults + children + infants

        errors = []

        # Step 1: Schedule validation
        if step in ['2', '3', '4'] and not schedule_id:
            errors.append({'field': 'schedule_id', 'message': 'Schedule selection required', 'step': 1})

        if step in ['2', '3', '4'] and total_passengers == 0:
            errors.append({'field': 'passengers', 'message': 'At least one passenger required', 'step': 2})

        if step in ['2', '3', '4']:
            try:
                schedule = Schedule.objects.get(
                    id=schedule_id,
                    status='scheduled',
                    departure_time__gt=timezone.now()
                )
                if schedule.available_seats < total_passengers:
                    errors.append({
                        'field': 'schedule_id',
                        'message': f'Only {schedule.available_seats} seats available',
                        'step': 1
                    })
            except Schedule.DoesNotExist:
                errors.append({'field': 'schedule_id', 'message': 'Invalid schedule', 'step': 1})

        # Step 4: Final validation and redirect to checkout
        if step == '4' and not errors:
            privacy_consent = request.POST.get('privacy_consent') == 'on'
            if not privacy_consent:
                errors.append({'field': 'privacy_consent', 'message': 'Privacy consent required', 'step': 4})

            if not errors:
                # Store form data in session for checkout
                request.session['booking_form_data'] = dict(request.POST)
                request.session['booking_step'] = '4'

                # Redirect to dedicated checkout endpoint
                return redirect('bookings:create_checkout_session')

        if errors:
            # Return errors for AJAX handling
            return JsonResponse({'success': False, 'errors': errors})

        # For non-step4 POSTs, save progress and return success
        request.session['booking_form_data'] = dict(request.POST)
        request.session['booking_step'] = step
        return JsonResponse({'success': True, 'message': 'Progress saved'})

    return JsonResponse({'error': 'Invalid request method'}, status=405)


def ticket_detail(request, ticket_id):
    ticket = get_object_or_404(Ticket, id=ticket_id)
    return render(request, "ticket.html", {"ticket": ticket})


@login_required_allow_anonymous
def view_tickets(request, booking_id):
    booking = get_object_or_404(Booking, id=booking_id)

    if request.user.is_authenticated and booking.user != request.user:
        logger.error(f"Authorization failed: User {request.user} not authorized for booking {booking_id}")
        return HttpResponseForbidden("You are not authorized to view this booking.")
    if not request.user.is_authenticated and booking.guest_email != request.session.get('guest_email'):
        logger.error(f"Authorization failed: Guest email mismatch for booking {booking_id}")
        return HttpResponseForbidden("You are not authorized to view this booking.")

    tickets = Ticket.objects.filter(booking=booking).select_related('passenger')
    cargo = Cargo.objects.filter(booking=booking).first()
    addons = AddOn.objects.filter(booking=booking)

    amount_to_charge = booking.total_price
    if booking.status == 'pending' and 'price_difference' in request.session:
        amount_to_charge = Decimal(str(request.session.get('price_difference', booking.total_price)))

    base_fare = booking.schedule.route.base_fare or Decimal('35.50')
    passenger_price = calculate_passenger_price(
        booking.passenger_adults, booking.passenger_children, booking.passenger_infants, booking.schedule
    )

    return render(request, 'bookings/ticket.html', {
        'booking': booking,
        'tickets': tickets,
        'cargo': cargo,
        'addons': addons,
        'amount_to_charge': amount_to_charge,
        'price_adults': booking.passenger_adults * base_fare,
        'price_children': booking.passenger_children * base_fare * Decimal('0.5'),
        'price_infants': booking.passenger_infants * base_fare * Decimal('0.1'),
        'cargo_price': cargo.price if cargo else Decimal('0.00'),
        'addon_prices': {addon.add_on_type: addon.price for addon in addons},
        'estimated_duration': int(booking.schedule.route.estimated_duration.total_seconds() / 60) if booking.schedule.route.estimated_duration else None,
        'stripe_publishable_key': settings.STRIPE_PUBLISHABLE_KEY
    })



def _load_image(path):
    """Safely load image or return None."""
    if path and os.path.exists(path):
        try:
            return ImageReader(path)
        except Exception:
            pass
    return None


def booking_pdf(request, booking_id):
    booking = get_object_or_404(Booking, id=booking_id)
    tickets = list(booking.tickets.all().order_by('passenger__first_name'))

    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    # ----------------------- Palette -----------------------
    BRAND_PRIMARY = colors.HexColor("#0EA5E9")
    BRAND_DARK   = colors.HexColor("#1E40AF")
    TEXT_PRIMARY = colors.HexColor("#111827")
    TEXT_MUTED   = colors.HexColor("#6B7280")
    BORDER       = colors.HexColor("#E5E7EB")
    BG_CARD      = colors.HexColor("#FFFFFF")

    # ----------------------- Logo -----------------------
    logo_path = os.path.join(settings.BASE_DIR, "static", "logo.png")
    logo_img = _load_image(logo_path)

    # ----------------------- Helpers -----------------------
    def font(name, size):
        p.setFont(name, size)

    def fmt_dt(dt):
        return dt.strftime("%a, %d %b %Y %H:%M") if dt else "—"

    def draw_row(x, y, label, value, gap=5*mm, colon=True):
        p.setFillColor(TEXT_MUTED)
        font("Helvetica", 9)
        txt = f"{label}{':' if colon else ''}"
        p.drawString(x, y, txt)
        w = p.stringWidth(txt, "Helvetica", 9)
        p.setFillColor(TEXT_PRIMARY)
        font("Helvetica-Bold", 10)
        p.drawString(x + w + gap, y, value or "—")

    # ----------------------- Subtle Watermark (Text) -----------------------
    def draw_watermark():
        p.saveState()
        p.setFillColor(BRAND_PRIMARY)
        try:
            p.setFillAlpha(0.05)
        except Exception:
            pass
        p.translate(width * 0.7, height * 0.3)
        p.rotate(30)
        font("Helvetica-Bold", 80)
        p.drawCentredString(0, 0, "FIJI FERRY")
        p.restoreState()

    # ----------------------- Header -----------------------
    def draw_header():
        band_h = 28*mm
        p.setFillColor(BRAND_PRIMARY)
        p.rect(0, height-band_h, width, band_h, stroke=0, fill=1)
        p.setFillColor(BRAND_DARK)
        p.rect(0, height-band_h, width, band_h/2, stroke=0, fill=1)

        if logo_img:
            logo_w, logo_h = 48*mm, 16*mm
            p.drawImage(logo_img,
                        15*mm,
                        height - band_h/2 - logo_h/2,
                        width=logo_w, height=logo_h,
                        preserveAspectRatio=True)

        title_x = 70*mm if logo_img else 15*mm
        p.setFillColor(colors.white)
        font("Helvetica-Bold", 19)
        p.drawString(title_x, height - 16*mm, "Fiji Ferry")
        font("Helvetica", 11)
        p.drawString(title_x, height - 22*mm, f"Booking #{booking.id}")

    # ----------------------- Footer -----------------------
    def draw_footer():
        p.setStrokeColor(BORDER)
        p.setLineWidth(0.5)
        p.line(15*mm, 20*mm, width-15*mm, 20*mm)

        p.setFillColor(TEXT_MUTED)
        font("Helvetica", 8)
        p.drawString(15*mm, 15*mm,
                     "Present this boarding pass with a valid photo ID.")
        p.drawString(15*mm, 11*mm,
                     "support@fijiferry.example • +679 738 8496")

    # ----------------------- Ticket Card -----------------------
    def draw_ticket_card(t, start_y):
        margin = 15*mm
        card_w = width - 2*margin
        card_h = 130*mm
        card_y = start_y - card_h

        # Card
        p.setFillColor(BG_CARD)
        p.setStrokeColor(BORDER)
        p.setLineWidth(0.8)
        p.roundRect(margin, card_y, card_w, card_h, 8*mm, stroke=1, fill=1)

        # Brand stripe
        p.setFillColor(BRAND_PRIMARY)
        p.rect(margin, card_y + card_h - 6*mm, card_w, 6*mm, stroke=0, fill=1)

        # Header
        p.setFillColor(BRAND_DARK)
        font("Helvetica-Bold", 15)
        p.drawString(margin + 12*mm, card_y + card_h - 15*mm, f"Ticket #{t.id}")

        p.setFillColor(TEXT_MUTED)
        font("Helvetica-Oblique", 9)
        issued = getattr(t, 'created_at', None) or getattr(t, 'issued_at', None)
        p.drawString(margin + 12*mm, card_y + card_h - 22*mm,
                     f"Issued {fmt_dt(issued)}")

        # Columns
        col_gap = 12*mm
        inner = 12*mm
        col_w = (card_w - inner*2 - col_gap) / 2
        left_x = margin + inner
        right_x = left_x + col_w + col_gap
        row_h = 8*mm
        y = card_y + card_h - 32*mm

        # Passenger
        p.setFillColor(BRAND_DARK)
        font("Helvetica-Bold", 11)
        p.drawString(left_x, y, "Passenger")
        y -= 6*mm

        passenger = getattr(t, 'passenger', None)
        name = "—"
        ptype = ""
        if passenger:
            name = f"{passenger.first_name or ''} {passenger.last_name or ''}".strip()
            ptype = getattr(passenger, 'get_passenger_type_display', lambda: "—")()

        draw_row(left_x, y, "Name", name); y -= row_h
        draw_row(left_x, y, "Type", ptype); y -= row_h
        draw_row(left_x, y, "Booking", f"#{booking.id}"); y -= row_h
        draw_row(left_x, y, "Status", t.ticket_status.title())

        # Schedule
        p.setFillColor(BRAND_DARK)
        font("Helvetica-Bold", 11)
        p.drawString(right_x, card_y + card_h - 38*mm, "Schedule")
        y2 = card_y + card_h - 44*mm

        sched = getattr(booking, 'schedule', None)
        ferry_name = getattr(getattr(sched, 'ferry', None), 'name', "—")
        route_str = "—"
        if sched and getattr(sched, 'route', None):
            dep = getattr(sched.route.departure_port, 'name', '—')
            dest = getattr(sched.route.destination_port, 'name', '—')
            route_str = f"{dep} → {dest}"

        draw_row(right_x, y2, "Ferry", ferry_name); y2 -= row_h
        draw_row(right_x, y2, "Route", route_str); y2 -= row_h
        draw_row(right_x, y2, "Departure", fmt_dt(getattr(sched, 'departure_time', None))); y2 -= row_h
        draw_row(right_x, y2, "Arrival", fmt_dt(getattr(sched, 'arrival_time', None)))

        # QR Code
        qr = getattr(t, 'qr_code', None)
        if qr and hasattr(qr, 'path') and qr.path and os.path.exists(qr.path):
            qr_size = 48*mm
            qr_x = right_x + col_w - qr_size - 2*mm
            qr_y = card_y + 12*mm

            p.setStrokeColor(BORDER)
            p.setLineWidth(1)
            p.roundRect(qr_x - 4*mm, qr_y - 4*mm,
                        qr_size + 8*mm, qr_size + 8*mm,
                        4*mm, stroke=1, fill=0)

            try:
                p.drawImage(_load_image(qr.path),
                            qr_x, qr_y,
                            width=qr_size, height=qr_size,
                            preserveAspectRatio=True)
            except Exception:
                p.setFillColor(colors.red)
                font("Helvetica", 8)
                p.drawString(qr_x, qr_y, "QR Error")

            p.setFillColor(TEXT_MUTED)
            font("Helvetica", 9)
            p.drawCentredString(qr_x + qr_size/2, qr_y - 8*mm, "Scan at check-in")

        # Note
        p.setFillColor(TEXT_MUTED)
        font("Helvetica", 8)
        p.drawString(margin + 12*mm, card_y + 8*mm,
                     "Valid only for listed passenger and sailing.")

        return card_y - 15*mm

    # ----------------------- Build PDF -----------------------
    y_cursor = height - 40*mm

    for idx, ticket in enumerate(tickets):
        if idx > 0:
            p.showPage()
            y_cursor = height - 40*mm

        draw_header()
        draw_watermark()  # Safe text watermark
        y_cursor = draw_ticket_card(ticket, y_cursor)
        draw_footer()

    p.save()
    buffer.seek(0)
    return FileResponse(buffer,
                        as_attachment=True,
                        filename=f"FijiFerry_Booking_{booking.id}_Tickets.pdf")


@login_required
def process_payment(request, booking_id):
    booking = get_object_or_404(Booking, id=booking_id)

    if booking.user and booking.user != request.user:
        logger.error(f"Authorization failed: User {request.user} not authorized for booking {booking_id}")
        return HttpResponseForbidden("You are not authorized to process this payment.")

    if booking.status == 'cancelled':
        messages.error(request, "This booking is no longer valid.")
        return redirect('bookings:booking_history')

    base_fare = booking.schedule.route.base_fare or Decimal('35.50')
    price_adults = Decimal(booking.passenger_adults) * base_fare
    price_children = Decimal(booking.passenger_children) * base_fare * Decimal('0.5')
    price_infants = Decimal(booking.passenger_infants) * base_fare * Decimal('0.1')
    cargo_price = sum(cargo.price for cargo in Cargo.objects.filter(booking=booking))
    addon_price = sum(addon.price for addon in AddOn.objects.filter(booking=booking))
    total_price = price_adults + price_children + price_infants + cargo_price + addon_price

    price_difference = request.session.get('price_difference')
    if price_difference is not None:
        try:
            price_difference = Decimal(str(price_difference))
        except Exception:
            price_difference = None

    amount_to_charge = price_difference if (price_difference and price_difference > 0) else total_price

    if amount_to_charge <= 0:
        logger.error(f"Invalid amount_to_charge for booking {booking_id}: {amount_to_charge}")
        return JsonResponse({'error': 'Payment amount must be greater than zero.'}, status=400)

    if request.method == 'POST':
        try:
            amount_cents = int(amount_to_charge * 100)
            if amount_cents <= 0:
                return JsonResponse({'error': 'Payment amount must be positive.'}, status=400)

            customer_email = booking.user.email if booking.user else booking.guest_email
            if not customer_email:
                return JsonResponse({'error': 'A valid email is required for payment.'}, status=400)

            success_url = request.build_absolute_uri('/bookings/success/?session_id={CHECKOUT_SESSION_ID}')
            cancel_url = request.build_absolute_uri('/bookings/cancel/')

            logger.info(f"Creating Stripe session for booking {booking_id}: amount={amount_cents}, email={customer_email}")

            session = stripe.checkout.Session.create(
                payment_method_types=['card'],
                line_items=[{
                    'price_data': {
                        'currency': 'fjd',
                        'product_data': {'name': f'Ferry Booking #{booking.id}'},
                        'unit_amount': amount_cents,
                    },
                    'quantity': 1,
                }],
                mode='payment',
                success_url=success_url,
                cancel_url=cancel_url,
                metadata={'booking_id': str(booking.id), 'guest_email': booking.guest_email or ''},
                customer_email=customer_email,
            )

            booking.stripe_session_id = session.id
            booking.save()

            Payment.objects.create(
                booking=booking,
                payment_method='stripe',
                amount=amount_to_charge,
                session_id=session.id,
                payment_status='pending'
            )

            request.session['booking_id'] = booking.id
            request.session['stripe_session_id'] = session.id
            if booking.guest_email and not request.user.is_authenticated:
                request.session['guest_email'] = booking.guest_email
            request.session.pop('price_difference', None)

            return JsonResponse({'sessionId': session.id})

        except stripe.error.StripeError as e:
            logger.error(f"Stripe error for booking {booking_id}: {str(e)}")
            return JsonResponse({'error': f"Payment processing error: {str(e)}"}, status=400)
        except Exception as e:
            logger.error(f"Unexpected error for booking {booking_id}: {str(e)}")
            return JsonResponse({'error': 'An unexpected error occurred. Please contact support.'}, status=500)

    return render(request, 'bookings/payment.html', {
        'booking': booking,
        'amount_to_charge': amount_to_charge,
        'price_adults': price_adults,
        'price_children': price_children,
        'price_infants': price_infants,
        'cargo_price': cargo_price,
        'addon_price': addon_price,
        'stripe_publishable_key': settings.STRIPE_PUBLISHABLE_KEY
    })



# Helper: safe display name (works with custom User)
def _display_name(user):
    """
    Best-effort user display name without relying on get_full_name()
    (works for custom User models).
    """
    if not user:
        return None
    first = getattr(user, "first_name", "") or ""
    last  = getattr(user, "last_name", "") or ""
    name = f"{first} {last}".strip()
    if name:
        return name
    return getattr(user, "email", None) or getattr(user, "username", None)


def payment_success(request):
    booking_id = request.session.get('booking_id')
    session_id = request.GET.get('session_id') or request.session.get('stripe_session_id')

    logger.debug(f"Payment success: booking_id={booking_id}, session_id={session_id}")

    # Try to recover a missing/placeholder session_id from the Booking record
    if not session_id or session_id == '{CHECKOUT_SESSION_ID}':
        logger.warning("Invalid or missing session_id in payment_success")
        session_id = None
        if booking_id:
            try:
                booking = Booking.objects.get(id=booking_id)
                session_id = booking.stripe_session_id
                logger.debug(f"Retrieved session_id {session_id} from booking {booking_id}")
            except Booking.DoesNotExist:
                logger.error(f"Booking {booking_id} not found")
                messages.error(request, "Booking not found. Please contact support.")
                return redirect('bookings:booking_history')

    # If we have a session_id but no booking_id, look it up via Stripe metadata
    if not booking_id and session_id:
        try:
            session = stripe.checkout.Session.retrieve(session_id)
            booking_id = session.metadata.get('booking_id')
            guest_email = session.metadata.get('guest_email')
            if guest_email and not request.session.get('guest_email'):
                request.session['guest_email'] = guest_email
                logger.debug(f"Restored guest_email from metadata: {guest_email}")
        except stripe.error.StripeError as e:
            logger.error(f"Stripe error retrieving session {session_id}: {str(e)}")
            messages.error(request, "Error verifying payment session. Please contact support.")
            return redirect('bookings:booking_history')
        except Exception as e:
            logger.error(f"Unexpected error retrieving session {session_id}: {str(e)}")
            messages.error(request, "An unexpected error occurred retrieving payment session. Please contact support.")
            return redirect('bookings:booking_history')

    if not booking_id:
        logger.error("Missing booking_id in session and metadata")
        messages.error(request, "Payment status could not be verified due to missing booking information. Please contact support.")
        return redirect('bookings:booking_history')

    try:
        booking = Booking.objects.get(id=booking_id)
    except Booking.DoesNotExist:
        logger.error(f"Booking {booking_id} not found")
        messages.error(request, "Booking not found. Please contact support.")
        return redirect('bookings:booking_history')

    # Authorization
    if request.user.is_authenticated and booking.user != request.user:
        logger.error(f"Authorization failed: User {request.user} not authorized for booking {booking_id}")
        return HttpResponseForbidden("You are not authorized to view this booking.")
    if not request.user.is_authenticated and booking.guest_email != request.session.get('guest_email'):
        logger.error(f"Authorization failed: Guest email mismatch for booking {booking_id}")
        return HttpResponseForbidden("You are not authorized to view this booking.")

    if booking.evaluated_status == 'cancelled':
        logger.error(f"Booking {booking_id} is cancelled or expired")
        messages.error(request, "This booking is no longer valid.")
        return redirect('bookings:booking_history')

    # Currency formatting
    def fmt_fjd(value):
        try:
            d = Decimal(value)
        except Exception:
            d = Decimal("0.00")
        d = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"FJD {d:,.2f}"

    try:
        # DEV shortcut
        if session_id == 'debug-mode' and settings.DEBUG:
            payment, _ = Payment.objects.get_or_create(
                booking=booking,
                session_id='debug-session',
                defaults={
                    'payment_method': 'stripe',
                    'amount': booking.total_price,
                    'payment_status': 'completed',
                    'transaction_id': 'debug-transaction',
                    'payment_intent_id': 'debug-transaction'
                }
            )
            booking.status = 'confirmed'
            booking.payment_intent_id = 'debug-transaction'
            booking.save()
            logger.info(f"Debug mode payment processed for booking {booking.id}")
        else:
            if not session_id:
                logger.error(f"No valid session_id found for booking {booking_id}")
                messages.error(request, "Invalid payment session. Please try again or contact support.")
                return redirect('bookings:booking_history')

            # Retrieve and verify the session
            session = stripe.checkout.Session.retrieve(session_id, expand=['payment_intent'])
            if not session.payment_intent:
                logger.error(f"No payment_intent found for session {session_id}, booking {booking_id}")
                messages.error(request, "Payment could not be verified. Please contact support.")
                return redirect('bookings:booking_history')

            if session.metadata.get('booking_id') != str(booking_id):
                logger.error(f"Session {session_id} metadata mismatch for booking {booking_id}")
                messages.error(request, "Invalid payment session. Please contact support.")
                return redirect('bookings:booking_history')

            # Persist payment record
            payment, created = Payment.objects.get_or_create(
                booking=booking,
                session_id=session.id,
                defaults={
                    'payment_method': 'stripe',
                    'amount': Decimal(session.amount_total) / 100,
                    'payment_status': 'pending'
                }
            )
            payment.payment_intent_id = session.payment_intent.id
            payment.transaction_id = session.payment_intent.id
            payment.amount = Decimal(session.payment_intent.amount) / 100

            if session.payment_intent.status == 'succeeded':
                payment.payment_status = 'completed'
                booking.status = 'confirmed'
                booking.payment_intent_id = session.payment_intent.id
                booking.stripe_session_id = session.id
                booking.save()
                logger.info(f"Payment confirmed for booking {booking.id}")
            else:
                logger.warning(f"Payment not completed for booking {booking.id}: status={session.payment_intent.status}")
                messages.error(request, f"Payment is not completed yet. Status: {session.payment_intent.status}")
                return redirect('bookings:booking_history')

            payment.save()

        # --- Ticket generation with QR codes ---
        tickets = []
        if Ticket.objects.filter(booking=booking).count() == booking.passengers.count():
            logger.info(f"Tickets already generated for booking {booking.id}")
            tickets = list(Ticket.objects.filter(booking=booking))
        else:
            if not booking.passengers.exists():
                logger.error(f"No passengers found for booking {booking.id}")
                messages.error(request, "No passengers associated with booking. Please contact support.")
                return redirect('bookings:booking_history')

            logger.debug(f"Starting ticket generation for booking {booking.id}")
            for passenger in booking.passengers.all():
                if not Ticket.objects.filter(booking=booking, passenger=passenger).exists():
                    try:
                        ticket = Ticket(
                            booking=booking,
                            passenger=passenger,
                            ticket_status='active',
                            qr_token=uuid.uuid4().hex
                        )
                        ticket.full_clean()
                        ticket.save()

                        # Generate QR code
                        qr_url = request.build_absolute_uri(reverse('bookings:view_ticket', args=[ticket.qr_token]))
                        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=4)
                        qr.add_data(qr_url)
                        qr.make(fit=True)
                        img = qr.make_image(fill_color="black", back_color="white")
                        buffer = BytesIO()
                        img.save(buffer, format='PNG')
                        qr_image = buffer.getvalue()
                        buffer.close()

                        # Save QR to model
                        ticket.qr_code.save(f"qr_{ticket.id}.png", ContentFile(qr_image), save=True)
                        tickets.append(ticket)
                        logger.debug(f"Generated ticket {ticket.id} for passenger {passenger.id}")
                    except Exception as e:
                        logger.error(f"Error generating ticket for passenger {passenger.id}: {str(e)}")
                        messages.error(request, "Error generating tickets. Please contact support.")
                        return redirect('bookings:booking_history')

        # --- Prepare email with embedded QR codes ---
        try:
            guest_name = _display_name(booking.user) or "Valued Guest"

            # Trip details
            estimated_duration = booking.schedule.route.estimated_duration
            arrival_str = "N/A"
            duration_str = "N/A"
            if estimated_duration:
                estimated_arrival = booking.schedule.departure_time + estimated_duration
                arrival_str = estimated_arrival.strftime("%A, %B %d, %Y at %H:%M")
                total_minutes = int(estimated_duration.total_seconds() / 60)
                hours, minutes = divmod(total_minutes, 60)
                duration_str = f"{hours}h {minutes}m" if minutes else f"{hours}h"

            dep_port = booking.schedule.route.departure_port.name
            dest_port = booking.schedule.route.destination_port.name
            vessel = booking.schedule.ferry.name
            depart = booking.schedule.departure_time.strftime("%A, %B %d, %Y at %H:%M")
            total_str = fmt_fjd(booking.total_price)

            # Passenger details
            passenger_details = [
                f"{p.first_name} {p.last_name} ({p.get_passenger_type_display()})"
                for p in booking.passengers.all()
            ]

            # Optional sections (vehicles, cargo, add-ons)
            def _section_html(title, rows):
                if not rows:
                    return ""
                return f'''
                    <tr><td colspan="2" style="padding:0 0 8px 0;">
                        <h3 style="margin:24px 0 8px;font-size:15px;font-weight:700;color:#111827;
                                  border-left:4px solid #3b82f6;padding-left:8px;">{title}</h3>
                    </td></tr>
                    <tr><td colspan="2" style="padding:0;">
                        <table style="width:100%;border-collapse:separate;border-spacing:0 6px;">
                            {''.join(rows)}
                        </table>
                    </td></tr>
                '''

            # Vehicles
            vehicle_rows = []
            for v in booking.vehicles.all():
                vehicle_rows.extend([
                    f'<tr><td style="width:40%;color:#6b7280;background:#f9fafb;padding:10px 12px;border:1px solid #eef2f7;">Type</td>'
                    f'<td style="background:#f9fafb;padding:10px 12px;border:1px solid #eef2f7;">{v.get_vehicle_type_display()}</td></tr>',
                    f'<tr><td style="color:#6b7280;background:#f9fafb;padding:10px 12px;border:1px solid #eef2f7;">Dimensions</td>'
                    f'<td style="background:#f9fafb;padding:10px 12px;border:1px solid #eef2f7;">{v.dimensions}</td></tr>',
                    f'<tr><td style="color:#6b7280;background:#f9fafb;padding:10px 12px;border:1px solid #eef2f7;">License Plate</td>'
                    f'<td style="background:#f9fafb;padding:10px 12px;border:1px solid #eef2f7;">{v.license_plate or "N/A"}</td></tr>',
                    f'<tr><td style="color:#6b7280;background:#f9fafb;padding:10px 12px;border:1px solid #eef2f7;">Price</td>'
                    f'<td style="background:#f9fafb;padding:10px 12px;border:1px solid #eef2f7;">{fmt_fjd(v.price)}</td></tr>',
                    '<tr><td colspan="2" style="background:transparent;border:none;padding:4px;"></td></tr>',
                ])
            vehicles_html = _section_html("Vehicles", vehicle_rows)

            # Cargo
            cargo_rows = []
            for c in booking.cargo.all():
                cargo_rows.extend([
                    f'<tr><td style="width:40%;color:#6b7280;background:#f9fafb;padding:10px 12px;border:1px solid #eef2f7;">Type</td>'
                    f'<td style="background:#f9fafb;padding:10px 12px;border:1px solid #eef2f7;">{c.get_cargo_type_display()}</td></tr>',
                    f'<tr><td style="color:#6b7280;background:#f9fafb;padding:10px 12px;border:1px solid #eef2f7;">Weight</td>'
                    f'<td style="background:#f9fafb;padding:10px 12px;border:1px solid #eef2f7;">{c.weight_kg} kg</td></tr>',
                    f'<tr><td style="color:#6b7280;background:#f9fafb;padding:10px 12px;border:1px solid #eef2f7;">Dimensions</td>'
                    f'<td style="background:#f9fafb;padding:10px 12px;border:1px solid #eef2f7;">{c.dimensions_cm or "N/A"}</td></tr>',
                    f'<tr><td style="color:#6b7280	background:#f9fafb;padding:10px 12px;border:1px solid #eef2f7;">License Plate</td>'
                    f'<td style="background:#f9fafb;padding:10px 12px;border:1px solid #eef2f7;">{c.license_plate or "N/A"}</td></tr>',
                    f'<tr><td style="color:#6b7280	background:#f9fafb;padding:10px 12px;border:1px solid #eef2f7;">Price</td>'
                    f'<td style="background:#f9fafb;padding:10px 12px;border:1px solid #eef2f7;">{fmt_fjd(c.price)}</td></tr>',
                    '<tr><td colspan="2" style="background:transparent;border:none;padding:4px;"></td></tr>',
                ])
            cargo_html = _section_html("Cargo", cargo_rows)

            # Add-ons
            addon_rows = []
            for a in booking.add_ons.all():
                qty = getattr(a, "quantity", 1) or 1
                addon_rows.append(
                    f'<tr><td style="width:40%;color:#6b7280;background:#f9fafb;padding:10px 12px;border:1px solid #eef2f7;">'
                    f'{a.get_add_on_type_display()}</td>'
                    f'<td style="background:#f9fafb;padding:10px 12px;border:1px solid #eef2f7;">x{qty} — {fmt_fjd(a.price)}</td></tr>'
                )
            addons_html = _section_html("Add-ons", addon_rows)

            # --- Generate QR code images for email (base64) ---
            qr_images = {}
            for ticket in tickets:
                if ticket.qr_code:
                    with default_storage.open(ticket.qr_code.name, 'rb') as img_file:
                        qr_images[ticket.id] = base64.b64encode(img_file.read()).decode('utf-8')

            # --- Plain-text fallback ---
            email_text = f"""Bula {guest_name},

Vinaka vakalevu! Your booking is confirmed.

Booking ID: {booking.id}
Route: {dep_port} → {dest_port}
Vessel: {vessel}
Departure: {depart}
Est. Arrival: {arrival_str}
Duration: {duration_str}

Passengers:
""" + "\n".join(f"- {p}" for p in passenger_details) + "\n\n"

            if booking.vehicles.exists():
                email_text += "Vehicles:\n" + "\n".join(
                    f"- {v.get_vehicle_type_display()} | {v.dimensions} | {v.license_plate or 'N/A'} | {fmt_fjd(v.price)}"
                    for v in booking.vehicles.all()
                ) + "\n\n"
            if booking.cargo.exists():
                email_text += "Cargo:\n" + "\n".join(
                    f"- {c.get_cargo_type_display()} | {c.weight_kg} kg | {c.dimensions_cm or 'N/A'} | {fmt_fjd(c.price)}"
                    for c in booking.cargo.all()
                ) + "\n\n"
            if booking.add_ons.exists():
                email_text += "Add-ons:\n" + "\n".join(
                    f"- {a.get_add_on_type_display()} (x{getattr(a, 'quantity', 1)}) | {fmt_fjd(a.price)}"
                    for a in booking.add_ons.all()
                ) + "\n\n"

            email_text += f"""Total Paid: {total_str}
View Tickets: {request.build_absolute_uri(reverse('bookings:view_tickets', args=[booking.id]))}

Please arrive 30–60 minutes early. Bring photo ID.

Support: support@yourferryservice.com | +679-738-8496

Vinaka vakalevu,
Fiji Ferry Service Team
"""

            # --- HTML Email with embedded QR codes ---
            wave_svg = "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIyMDAiIGhlaWdodD0iMTAwIiB2aWV3Qm94PSIwIDAgMjAwIDEwMCIgZmlsbD0ibm9uZSI+PHBhdGggZD0iTTAgNTBDMTYgNTAgMjQgNjUgMzUgNzBDNDYgODAgNTUgODUgNjUgODVDNzUgODUgODQgODAgOTUgNzBDMTA2IDY1IDExNiA1NSAxMzAgNTBDMTQ0IDQ1IDE1NiA0MCAxNzAgNDBDMTA0IDQwIDEwMCA0NSAqMTAwIDUwQzEwMCA1NSA5NiA2MCA5MCA2NUM4MyA3MCA3NSA3NSA2NSA3NUM1NSA3NSA0NSA3MCAzNSA2NUMyNSA2MCAxNiA1NSAwIDUwWiIgZmlsbD0iIzBlYTVlOSIgZmlsbC1vcGFjaXR5PSIwLjA1Ii8+PC9zdmc+"

            # QR ticket rows
            qr_rows = []
            for ticket in tickets:
                passenger = ticket.passenger
                name = f"{passenger.first_name} {passenger.last_name}"
                qr_cid = f"qr_{ticket.id}"
                qr_data = qr_images.get(ticket.id)
                qr_img = f'<img src="cid:{qr_cid}" alt="QR Code for {name}" style="width:150px;height:150px;margin:10px auto;display:block;border:1px solid #ddd;border-radius:8px;">' if qr_data else '<p style="color:#ef4444;">QR code unavailable</p>'
                qr_rows.append(f'''
                    <tr>
                        <td style="padding:16px 0;text-align:center;">
                            <p style="margin-bottom:8px;font-weight:600;color:#111827;">{name} ({passenger.get_passenger_type_display()})</p>
                            {qr_img}
                            <p style="margin:8px 0 0;font-size:12px;color:#6b7280;">Scan at check-in</p>
                        </td>
                    </tr>
                ''')

            email_html = f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Booking Confirmation #{booking.id}</title>
<style>
  body {{margin:0;padding:0;background:#f6f8fb;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:#1f2937;background-image:url('{wave_svg}');background-repeat:repeat-x;background-position:bottom;}}
  .container {{max-width:680px;margin:32px auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 10px 30px rgba(2,6,23,.06);border:1px solid #eef2f7;}}
  .header {{background:linear-gradient(135deg,#0ea5e9,#6366f1);color:#fff;padding:24px;display:flex;align-items:center;gap:12px;}}
  .content {{padding:28px;}}
  .hello {{margin:0 0 12px;font-size:17px;font-weight:600;}}
  .lead {{margin:0 0 24px;color:#475569;line-height:1.5;}}
  .section {{margin-bottom:28px;}}
  .section-title {{font-size:15px;font-weight:700;margin:0 0 8px;color:#111827;border-left:4px solid #3b82f6;padding-left:8px;}}
  .grid {{display:grid;grid-template-columns:160px 1fr;gap:6px 16px;background:#f9fafb;border:1px solid #eef2f7;border-radius:12px;padding:16px;}}
  .label {{color:#6b7280;}}
  .value {{color:#111827;font-weight:600;}}
  .badge {{display:inline-block;padding:5px 10px;border-radius:999px;font-size:12px;font-weight:700;background:#ecfeff;color:#155e75;border:1px solid #a5f3fc;}}
  .total-box {{display:flex;justify-content:space-between;align-items:center;background:#f3f4f6;border:1px solid #e5e7eb;border-radius:12px;padding:14px 16px;margin-top:12px;}}
  .total-amount {{font-size:20px;font-weight:800;color:#111827;}}
  .cta {{display:block;text-align:center;margin:28px 0 0;background:#2563eb;color:#fff;text-decoration:none;padding:13px 16px;border-radius:10px;font-weight:700;}}
  .footer {{margin-top:32px;padding-top:20px;border-top:1px solid #e5e7eb;color:#6b7280;font-size:12px;text-align:center;}}
  .qr-table {{width:100%;border-collapse:separate;border-spacing:0 12px;}}
  a {{color:#2563eb;text-decoration:none;}}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <svg viewBox="0 0 24 24" fill="none" width="22" height="22">
      <path d="M3 18c3 0 3-2 6-2s3 2 6 2 3-2 6-2" stroke="white" stroke-width="1.5" stroke-linecap="round"/>
      <path d="M10 14l3-7 3 7" stroke="white" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>
    <h1 style="margin:0;font-size:18px;font-weight:700;">Booking Confirmation #{booking.id}</h1>
  </div>

  <div class="content">
    <p class="hello">Bula {guest_name},</p>
    <p class="lead">Vinaka vakalevu! Your payment is confirmed and your journey is booked.</p>

    <div class="section">
      <h3 class="section-title">Trip Details</h3>
      <div class="grid">
        <div class="label">Route</div><div class="value">{dep_port} → {dest_port}</div>
        <div class="label">Vessel</div><div class="value">{vessel}</div>
        <div class="label">Departure</div><div class="value">{depart}</div>
        <div class="label">Est. Arrival</div><div class="value">{arrival_str}</div>
        <div class="label">Duration</div><div class="value">{duration_str}</div>
        <div class="label">Status</div><div class="value"><span class="badge">Paid</span></div>
      </div>
    </div>

    <div class="section">
      <h3 class="section-title">Your Tickets</h3>
      <table class="qr-table">
        {''.join(qr_rows)}
      </table>
    </div>

    <div class="section">
      <h3 class="section-title">Passengers</h3>
      <table style="width:100%;border-collapse:separate;border-spacing:0 6px;">
        {''.join(f'<tr><td style="width:40%;color:#6b7280;background:#f9fafb;padding:10px 12px;border:1px solid #eef2f7;">Passenger</td>'
                 f'<td style="background:#f9fafb;padding:10px 12px;border:1px solid #eef2f7;">{pd}</td></tr>' for pd in passenger_details)}
      </table>
    </div>

    {vehicles_html}
    {cargo_html}
    {addons_html}

    <div class="section">
      <h3 class="section-title">Payment</h3>
      <div class="total-box">
        <div style="font-weight:600;">Total Paid</div>
        <div class="total-amount">{total_str}</div>
      </div>
    </div>

    <a class="cta" href="{request.build_absolute_uri(reverse('bookings:view_tickets', args=[booking.id]))}">
      View All Tickets Online
    </a>

    <div class="footer">
      Please arrive 30–60 minutes early. Present QR code at check-in.<br>
      Need help? <a href="mailto:support@yourferryservice.com">support@yourferryservice.com</a> • +679-738-8496
      <br><br>Vinaka vakSnack, and safe travels,<br><strong>Fiji Ferry Service Team</strong>
    </div>
  </div>
</div>
</body>
</html>
"""

            # --- Send email with inline QR images ---
            recipient = booking.user.email if booking.user and getattr(booking.user, "email", None) else (
                request.session.get('guest_email') or booking.guest_email
            )

            if recipient:
                msg = EmailMultiAlternatives(
                    subject=f"Your Ferry Booking Confirmed – ID {booking.id}",
                    body=email_text,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    to=[recipient]
                )
                # Attach HTML body
                msg.attach_alternative(email_html, "text/html")

                # Ensure HTML + inline images are grouped as multipart/related
                msg.mixed_subtype = 'related'

                # Attach QR codes as inline images with Content-ID
                for ticket in tickets:
                    b64 = qr_images.get(ticket.id)
                    if b64:
                        img_part = MIMEImage(base64.b64decode(b64), _subtype="png")
                        img_part.add_header('Content-ID', f'<qr_{ticket.id}>')  # matches src="cid:qr_<id>"
                        img_part.add_header('Content-Disposition', 'inline', filename=f'qr_{ticket.id}.png')
                        msg.attach(img_part)

                msg.send()
                logger.info(f"Confirmation email with QR codes sent to {recipient}")
            else:
                logger.warning("No recipient email found for booking confirmation")

        except Exception as e:
            logger.error(f"Error sending email for booking {booking.id}: {str(e)}")
            messages.warning(request, "Booking confirmed, but email failed. Check your inbox later.")

        # Cleanup and redirect
        messages.success(request, f'Booking #{booking.id} confirmed! Tickets generated and emailed.')
        request.session.pop('booking_id', None)
        request.session.pop('stripe_session_id', None)
        request.session.pop('guest_email', None)

        return redirect('bookings:view_tickets', booking_id=booking.id)

    except stripe.error.StripeError as e:
        logger.error(f"Stripe error for booking {booking_id}: {str(e)}")
        messages.error(request, f"Payment verification failed: {str(e)}. Please contact support.")
        return redirect('bookings:booking_history')
    except Exception as e:
        logger.error(f"Unexpected error for booking {booking_id}: {str(e)}")
        messages.error(request, "An unexpected error occurred. Please contact support.")
        return redirect('bookings:booking_history')



@login_required_allow_anonymous
def payment_cancel(request):
    booking_id = request.session.get('booking_id')
    logger.debug(f"Payment cancelled: booking_id={booking_id}")

    if booking_id:
        try:
            booking = Booking.objects.get(id=booking_id)
            if request.user.is_authenticated and booking.user != request.user:
                logger.error(f"Authorization failed: User {request.user} not authorized for booking {booking_id}")
                return HttpResponseForbidden("You are not authorized to view this booking.")
            if not request.user.is_authenticated and booking.guest_email != request.session.get('guest_email'):
                logger.error(f"Authorization failed: Guest email mismatch for booking {booking_id}")
                return HttpResponseForbidden("You are not authorized to view this booking.")

            booking.status = 'cancelled'
            booking.schedule.available_seats += (
                booking.passenger_adults + booking.passenger_children + booking.passenger_infants
            )
            booking.schedule.save()
            booking.save()

            messages.info(request, f'Booking #{booking.id} has been cancelled.')
            request.session.pop('booking_id', None)
            request.session.pop('stripe_session_id', None)
            request.session.pop('price_difference', None)

        except Booking.DoesNotExist:
            logger.error(f"Booking {booking_id} not found")
            messages.error(request, "Booking not found. Please contact support.")
    else:
        logger.warning("No booking_id found in session for cancellation")
        messages.error(request, "No booking found to cancel.")

    return redirect('bookings:booking_history')


@require_POST
def cancel_booking(request, booking_id):
    booking = get_object_or_404(Booking, id=booking_id)

    # Authorization
    if request.user.is_authenticated:
        if booking.user != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)
    else:
        if booking.guest_email != request.session.get('guest_email'):
            return JsonResponse({'error': 'Unauthorized'}, status=403)

    if booking.status == 'cancelled':
        return JsonResponse({'error': 'Booking already cancelled'}, status=400)

    if booking.schedule.departure_time <= timezone.now() + datetime.timedelta(hours=6):
        return JsonResponse({'error': 'Cannot cancel within 6 hours of departure'}, status=400)

    try:
        with transaction.atomic():
            # Get payment to retrieve exact amount paid
            payment = Payment.objects.filter(booking=booking, payment_status='completed').first()
            if not payment or not payment.payment_intent_id:
                return JsonResponse({'error': 'No payment found'}, status=400)

            # Use EXACT amount from Stripe (in cents)
            session = stripe.checkout.Session.retrieve(
                payment.session_id or booking.stripe_session_id,
                expand=['payment_intent']
            )
            if not session.payment_intent:
                return JsonResponse({'error': 'Payment intent not found'}, status=400)

            amount_paid_cents = session.payment_intent.amount_received  # Exact amount in cents

            # Create refund using EXACT amount
            refund = stripe.Refund.create(
                payment_intent=session.payment_intent.id,
                amount=amount_paid_cents  # ← Critical: use exact amount
            )

            # Update booking & payment
            booking.status = 'cancelled'
            booking.save()

            payment.payment_status = 'refunded'
            payment.refund_id = refund.id
            payment.save()

            logger.info(f"Booking {booking.id} cancelled and refunded {amount_paid_cents} cents")

        return JsonResponse({'message': 'Booking cancelled and refunded successfully'})

    except stripe.error.StripeError as e:
        logger.error(f"Refund error for booking {booking.id}: {e}")
        return JsonResponse({'error': str(e.user_message or 'Refund failed')}, status=400)
    except Exception as e:
        logger.error(f"Unexpected error cancelling booking {booking.id}: {e}")
        return JsonResponse({'error': 'An unexpected error occurred'}, status=500)


@require_POST
@csrf_exempt
def stripe_webhook(request):
    payload = request.body
    sig_header = request.META.get('HTTP_STRIPE_SIGNATURE')
    endpoint_secret = settings.STRIPE_WEBHOOK_SECRET

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except ValueError:
        logger.error("Invalid webhook payload")
        return JsonResponse({'status': 'invalid payload'}, status=400)
    except stripe.error.SignatureVerificationError:
        logger.error("Invalid webhook signature")
        return JsonResponse({'status': 'invalid signature'}, status=400)

    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        booking_id = session.get('metadata', {}).get('booking_id')
        session_id = session.get('id')
        payment_intent_id = session.get('payment_intent')
        guest_email = session.get('metadata', {}).get('guest_email')

        if not booking_id:
            logger.error(f"No booking_id in session metadata: session_id={session_id}")
            return JsonResponse({'status': 'missing booking_id'}, status=400)

        try:
            booking = Booking.objects.get(id=booking_id)
        except Booking.DoesNotExist:
            logger.error(f"Booking {booking_id} not found for session {session_id}")
            return JsonResponse({'status': 'booking not found'}, status=404)

        try:
            payment, created = Payment.objects.get_or_create(
                booking=booking,
                session_id=session_id,
                defaults={
                    'payment_method': 'stripe',
                    'amount': Decimal(session.get('amount_total', 0)) / 100,
                    'payment_status': 'pending'
                }
            )
            payment.payment_intent_id = payment_intent_id
            payment.transaction_id = payment_intent_id
            payment.amount = Decimal(session.get('amount_total', 0)) / 100
            payment.payment_status = 'completed'
            payment.save()

            booking.status = 'confirmed'
            booking.payment_intent_id = payment_intent_id
            booking.stripe_session_id = session_id
            booking.save()

            # Create tickets if missing
            if Ticket.objects.filter(booking=booking).count() < booking.passengers.count():
                logger.debug(f"Starting ticket generation for booking {booking.id}, passenger count: {booking.passengers.count()}")
                for passenger in booking.passengers.all():
                    if not Ticket.objects.filter(booking=booking, passenger=passenger).exists():
                        try:
                            ticket = Ticket(
                                booking=booking,
                                passenger=passenger,
                                ticket_status='active',
                                qr_token=uuid.uuid4().hex
                            )
                            ticket.full_clean()
                            ticket.save()
                            qr_data = request.build_absolute_uri(reverse('bookings:view_ticket', args=[ticket.qr_token]))
                            qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L)
                            qr.add_data(qr_data)
                            qr.make(fit=True)
                            img = qr.make_image(fill_color="black", back_color="white")
                            buffer = BytesIO()
                            img.save(buffer, format='PNG')
                            try:
                                ticket.qr_code.save(f"ticket_{ticket.id}.png", ContentFile(buffer.getvalue()), save=True)
                                logger.debug(f"Generated ticket {ticket.id} for passenger {passenger.id}")
                            except Exception as e:
                                logger.error(f"Error saving QR code for ticket {ticket.id}: {str(e)}")
                                ticket.delete()
                                return JsonResponse({'status': 'error', 'message': 'Error saving ticket QR code'}, status=500)
                        except ValidationError as ve:
                            logger.error(f"Validation error for ticket, passenger {passenger.id}: {str(ve)}")
                            return JsonResponse({'status': 'error', 'message': 'Invalid ticket data'}, status=500)
                        except Exception as e:
                            logger.error(f"Error generating ticket for passenger {passenger.id}: {str(e)}")
                            return JsonResponse({'status': 'error', 'message': 'Error generating tickets'}, status=500)

            # Build confirmation email
            from datetime import timedelta
            from django.template.loader import render_to_string

            # Derive guest name
            guest_name = booking.user.get_full_name() if booking.user and booking.user.get_full_name() else "Valued Guest"

            # Calculate arrival and duration strings
            estimated_duration = booking.schedule.route.estimated_duration
            arrival_str = "N/A"
            duration_str = "N/A"
            if estimated_duration:
                estimated_arrival = booking.schedule.departure_time + estimated_duration
                arrival_str = estimated_arrival.strftime("%A, %B %d, %Y at %H:%M")
                total_minutes = int(estimated_duration.total_seconds() / 60)
                hours = total_minutes // 60
                minutes = total_minutes % 60
                duration_str = f"{hours} hours {minutes} minutes" if minutes else f"{hours} hours"

            # Passenger details list
            passenger_details = []
            for p in booking.passengers.all():
                passenger_details.append(f"{p.first_name} {p.last_name} ({p.get_passenger_type_display()})")

            # Plain text fallback
            email_body = (
                f"Dear {guest_name},\n\n"
                f"Thank you for choosing our ferry service. We are pleased to confirm your booking.\n\n"
                f"**Booking Details**\n"
                f"Booking ID: {booking.id}\n"
                f"Route: {booking.schedule.route.departure_port.name} to {booking.schedule.route.destination_port.name}\n"
                f"Vessel: {booking.schedule.ferry.name}\n"
                f"Departure: {booking.schedule.departure_time.strftime('%A, %B %d, %Y at %H:%M')}\n"
                f"Estimated Arrival: {arrival_str}\n"
                f"Estimated Duration: {duration_str}\n\n"
                f"**Passengers**\n" + "\n".join([f"- {pd}" for pd in passenger_details]) + f"\nTotal: {len(passenger_details)}\n\n"
            )

            if booking.vehicles.exists():
                email_body += "**Vehicles**\n"
                for vehicle in booking.vehicles.all():
                    email_body += (
                        f"- Type: {vehicle.get_vehicle_type_display()}\n"
                        f"  Dimensions: {vehicle.dimensions}\n"
                        f"  License Plate: {vehicle.license_plate or 'N/A'}\n"
                        f"  Price: FJD {vehicle.price}\n\n"
                    )

            if booking.cargo.exists():
                email_body += "**Cargo**\n"
                for cargo in booking.cargo.all():
                    email_body += (
                        f"- Type: {cargo.get_cargo_type_display()}\n"
                        f"  Weight: {cargo.weight_kg} kg\n"
                        f"  Dimensions: {cargo.dimensions_cm or 'N/A'}\n"
                        f"  License Plate: {cargo.license_plate or 'N/A'}\n"
                        f"  Price: FJD {cargo.price}\n\n"
                    )

            if booking.add_ons.exists():
                email_body += "**Add-ons**\n"
                for addon in booking.add_ons.all():
                    email_body += f"- {addon.get_add_on_type_display()} (x{addon.quantity}): FJD {addon.price}\n\n"

            email_body += (
                f"**Payment Summary**\n"
                f"Total Price: FJD {booking.total_price}\n"
                f"Payment Method: Stripe\n"
                f"Status: Completed\n\n"
                f"**Important Instructions**\n"
                f"Please arrive at least 30-60 minutes before departure for check-in and boarding.\n"
                f"Bring a valid ID for all passengers and vehicle documents if applicable.\n"
                f"Wear comfortable clothing and consider bringing water and snacks.\n"
                f"View your tickets: {request.build_absolute_uri(reverse('bookings:view_tickets', args=[booking.id]))}\n\n"
                f"For any inquiries, contact us at support@yourferryservice.com or +679-123-4567.\n"
                f"Review our cancellation policy: {request.build_absolute_uri(reverse('bookings:cancellation_policy'))}\n\n"
                f"Best regards,\n"
                f"Your Ferry Service Team"
            )

            # HTML version (assume 'emails/booking_confirmation.html' template exists)
            context = {
                'guest_name': guest_name,
                'booking': booking,
                'arrival_str': arrival_str,
                'duration_str': duration_str,
                'passengers': passenger_details,
                'vehicles': booking.vehicles.all(),
                'cargos': booking.cargo.all(),
                'add_ons': booking.add_ons.all(),
                'view_tickets_url': request.build_absolute_uri(reverse('bookings:view_tickets', args=[booking.id])),
                'policy_url': request.build_absolute_uri(reverse('bookings:cancellation_policy')),
                'contact_email': 'support@yourferryservice.com',
                'contact_phone': '+679-123-4567',
            }
            html_body = render_to_string('emails/booking_confirmation.html', context)

            send_mail(
                f"Your Ferry Booking Confirmation - ID {booking.id}",
                email_body,
                settings.DEFAULT_FROM_EMAIL,
                [booking.user.email if booking.user else guest_email],
                html_message=html_body,
                fail_silently=True
            )

            logger.info(f"Webhook processed: Payment confirmed for booking {booking.id}")
            return JsonResponse({'status': 'success'})

        except Payment.DoesNotExist:
            logger.error(f"Payment not found for booking {booking_id}, session {session_id}")
            return JsonResponse({'status': 'payment not found'}, status=404)

        except Exception as e:
            logger.error(f"Webhook error for booking {booking_id}: {str(e)}")
            return JsonResponse({'status': 'error', 'message': str(e)}, status=500)

    return JsonResponse({'status': 'event not handled'})


@login_required
def modify_booking(request, booking_id):
    booking = get_object_or_404(Booking, id=booking_id, user=request.user)
    now = timezone.now()

    if booking.status != 'confirmed' or booking.schedule.departure_time <= now + datetime.timedelta(hours=6):
        messages.error(request, "This booking cannot be modified.")
        return redirect('bookings:booking_history')

    form = ModifyBookingForm(request.POST or None, instance=booking)
    if request.method == 'POST' and form.is_valid():
        new_schedule = form.cleaned_data['schedule']
        new_adults = form.cleaned_data['passenger_adults']
        new_children = form.cleaned_data['passenger_children']
        new_infants = form.cleaned_data['passenger_infants']

        total_passengers = new_adults + new_children + new_infants
        if total_passengers == 0:
            messages.error(request, "At least one passenger is required.")
            return render(request, 'bookings/modify.html', {'form': form, 'booking': booking})

        if new_schedule.available_seats < total_passengers:
            messages.error(request, "Not enough seats available in the selected schedule.")
            return render(request, 'bookings/modify.html', {'form': form, 'booking': booking})

        old_total_price = booking.total_price
        new_total_price = calculate_total_price(
            new_adults, new_children, new_infants, new_schedule,
            booking.cargo.exists(), booking.cargo.first().cargo_type if booking.cargo.exists() else None,
            booking.cargo.first().weight_kg if booking.cargo.exists() else 0,
            [{'type': addon.add_on_type, 'quantity': addon.quantity} for addon in booking.addons.all()]
        )

        booking.schedule.available_seats += (
                    booking.passenger_adults + booking.passenger_children + booking.passenger_infants)
        booking.schedule.save()

        booking.schedule = new_schedule
        booking.passenger_adults = new_adults
        booking.passenger_children = new_children
        booking.passenger_infants = new_infants
        booking.total_price = new_total_price
        booking.save()

        new_schedule.available_seats -= total_passengers
        new_schedule.save()

        price_difference = new_total_price - old_total_price
        if price_difference > 0:
            request.session['price_difference'] = str(price_difference)
            request.session['booking_id'] = booking.id
            messages.info(request, f"Booking modified. Additional payment of FJD {price_difference} required.")
            return redirect('bookings:process_payment', booking_id=booking.id)
        elif price_difference < 0:
            try:
                refund = stripe.Refund.create(
                    payment_intent=booking.payment_intent_id,
                    amount=int(abs(price_difference) * 100)
                )
                Payment.objects.create(
                    booking=booking,
                    payment_method='stripe',
                    amount=price_difference,
                    payment_status='refunded',
                    transaction_id=refund.id
                )
                logger.info(f"Refund processed for booking {booking.id}: amount={price_difference}")
            except stripe.error.StripeError as e:
                logger.error(f"Refund error for booking {booking.id}: {str(e)}")
                messages.error(request, f"Refund processing failed: {str(e)}. Please contact support.")
                return redirect('bookings:booking_history')

        messages.success(request, "Booking modified successfully.")
        return redirect('bookings:view_tickets', booking_id=booking.id)

    return render(request, 'bookings/modify.html', {
        'form': form,
        'booking': booking,
        'cutoff_time': now + datetime.timedelta(hours=6)
    })


@login_required
def cancel_booking(request, booking_id):
    # Get the booking first so we can give precise feedback instead of a 404
    booking = get_object_or_404(Booking, id=booking_id)

    # Ownership/authorization check
    if booking.user_id != request.user.id and not request.user.is_staff:
        # Show a clear message to the user and avoid 404
        messages.error(request, "You can only cancel your own bookings.")
        logger.warning(
            "User %s attempted to cancel booking %s not owned by them.",
            request.user.id, booking_id
        )
        # For web flow, redirect with a message; for strict API, you could return HttpResponseForbidden
        return redirect('bookings:booking_history')

    now = timezone.now()
    cutoff = now + datetime.timedelta(hours=6)

    # Business rule messaging
    if booking.status != 'confirmed':
        messages.error(request, "Only confirmed bookings can be cancelled.")
        return redirect('bookings:booking_history')

    if booking.schedule.departure_time <= cutoff:
        # Be explicit why: departure too soon or already departed
        if booking.schedule.departure_time <= now:
            messages.error(request, "This trip has already departed and cannot be cancelled.")
        else:
            leave_in = booking.schedule.departure_time - now
            minutes_left = max(0, int(leave_in.total_seconds() // 60))
            messages.error(
                request,
                f"This booking cannot be cancelled within 6 hours of departure "
                f"(only {minutes_left} minute(s) remaining)."
            )
        return redirect('bookings:booking_history')

    if request.method == 'POST':
        try:
            # Process refund if we have a Stripe PaymentIntent
            if booking.payment_intent_id:
                refund = stripe.Refund.create(
                    payment_intent=booking.payment_intent_id,
                    amount=int(booking.total_price * 100)  # amount in cents
                )
                Payment.objects.create(
                    booking=booking,
                    payment_method='stripe',
                    amount=-booking.total_price,
                    payment_status='refunded',
                    transaction_id=refund.id
                )

            # Update booking and related objects
            booking.status = 'cancelled'
            booking.schedule.available_seats += (
                booking.passenger_adults + booking.passenger_children + booking.passenger_infants
            )
            booking.schedule.save()
            booking.save()

            # Mark tickets cancelled
            for ticket in booking.tickets.all():
                ticket.ticket_status = 'cancelled'
                ticket.save()

            messages.success(request, f"Booking #{booking.id} has been cancelled and refunded.")
            return redirect('bookings:booking_history')

        except stripe.error.StripeError as e:
            logger.error(f"Refund error for booking {booking.id}: {str(e)}")
            messages.error(request, f"Refund processing failed: {str(e)}. Please contact support.")
            return redirect('bookings:booking_history')
        except Exception as e:
            logger.error(f"Unexpected error cancelling booking {booking.id}: {str(e)}")
            messages.error(request, "An unexpected error occurred. Please contact support.")
            return redirect('bookings:booking_history')

    # GET -> show confirmation page
    return render(request, 'bookings/cancel.html', {
        'booking': booking,
        'cutoff_time': cutoff
    })


@login_required
def download_ticket(request, ticket_id):
    ticket = get_object_or_404(Ticket, id=ticket_id, booking__user=request.user)
    if ticket.ticket_status != 'active':
        messages.error(request, "This ticket is not valid for download.")
        return redirect('bookings:booking_history')

    buffer = BytesIO()
    ticket.qr_code.seek(0)
    buffer.write(ticket.qr_code.read())
    buffer.seek(0)

    response = HttpResponse(buffer, content_type='image/png')
    response['Content-Disposition'] = f'attachment; filename=ticket_{ticket.id}.png'
    return response


@require_GET
@staff_member_required
def weather_forecast_view(request):
    """Fetch weather forecasts for all ports using OpenWeatherMap API."""
    api_key = settings.OPENWEATHERMAP_API_KEY
    ports = Port.objects.values('lat', 'lng', 'name')
    forecasts = []
    cache_key = 'weather_forecasts_all_ports'
    cached_forecasts = cache.get(cache_key)

    if cached_forecasts:
        logger.info("Returning cached weather forecasts")
        return JsonResponse({'forecasts': cached_forecasts})

    try:
        for port in ports:
            url = f"https://api.openweathermap.org/data/2.5/forecast?lat={port['lat']}&lon={port['lng']}&appid={api_key}&units=metric"
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            data = response.json()
            if data.get('list'):
                forecasts.append({
                    'port': port['name'],
                    'forecast': [
                        {
                            'datetime': item['dt_txt'],
                            'temperature': float(item['main']['temp']),
                            'condition': item['weather'][0]['description'],
                            'wind_speed': float(item['wind']['speed']) * 3.6,  # Convert m/s to km/h
                            'precipitation_probability': float(item.get('pop', 0)) * 100
                        } for item in data['list'][:8]  # Next 24 hours (3-hour intervals)
                    ]
                })
        cache.set(cache_key, forecasts, timeout=1800)  # Cache for 30 minutes
        logger.info(f"Weather forecasts fetched for {len(ports)} ports")
        return JsonResponse({'forecasts': forecasts})
    except requests.RequestException as e:
        logger.error(f"OpenWeatherMap API error: {str(e)}")
        return JsonResponse({'error': 'Failed to fetch weather forecasts'}, status=500)


@require_GET
@staff_member_required
def stripe_insights_view(request):
    """Fetch recent Stripe transactions and disputes."""
    stripe.api_key = settings.STRIPE_SECRET_KEY
    cache_key = 'stripe_insights'
    cached_insights = cache.get(cache_key)

    if cached_insights:
        logger.info("Returning cached Stripe insights")
        return JsonResponse(cached_insights)

    try:
        charges = stripe.Charge.list(limit=5)
        disputes = stripe.Dispute.list(limit=3)
        insights = {
            'recent_charges': [
                {
                    'id': c.id,
                    'amount': float(c.amount / 100),
                    'status': c.status,
                    'created': datetime.datetime.fromtimestamp(c.created).isoformat(),
                    'description': c.description or f"Booking #{c.metadata.get('booking_id', 'N/A')}"
                } for c in charges.data
            ],
            'disputes': [
                {
                    'id': d.id,
                    'amount': float(d.amount / 100),
                    'status': d.status,
                    'reason': d.reason,
                    'created': datetime.datetime.fromtimestamp(d.created).isoformat()
                } for d in disputes.data
            ]
        }
        cache.set(cache_key, insights, timeout=300)  # Cache for 5 minutes
        logger.info("Stripe insights fetched successfully")
        return JsonResponse(insights)
    except stripe.error.StripeError as e:
        logger.error(f"Stripe API error: {str(e)}")
        return JsonResponse({'error': 'Failed to fetch Stripe insights'}, status=500)

