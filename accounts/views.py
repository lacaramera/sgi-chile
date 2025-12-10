from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth import login
from django.utils import timezone
from django.utils.http import urlsafe_base64_decode
from django.utils.encoding import force_str
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.forms import SetPasswordForm
from .models import User, Event

def home(request):
    # carousel: imágenes estáticas (si los eventos tienen image, las usamos)
    # coger próximos 5 eventos públicos
    now = timezone.now().date()
    upcoming = Event.objects.filter(is_public=True, date__gte=now).order_by('date')[:6]

    # Si no hay imágenes en Events, puedes configurar imágenes por defecto en static/img/
    carousel_images = []
    for ev in upcoming:
        if ev.image:
            carousel_images.append(ev.image)
    # si no hay imágenes, usa una lista fija
    if not carousel_images:
        carousel_images = ['carousel1.jpg','carousel2.jpg','carousel3.jpg']  # guarda estas en static/img/

    context = {
        'carousel_images': carousel_images,
        'upcoming': upcoming,
    }
    return render(request, 'home.html', context)


def activate_account(request, uidb64, token):
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None

    if user is not None and default_token_generator.check_token(user, token):
        # Si GET -> mostrar formulario para elegir contraseña
        if request.method == 'POST':
            form = SetPasswordForm(user, request.POST)
            if form.is_valid():
                form.save()  # guarda la nueva contraseña
                user.is_active = True
                user.save()
                login(request, user)  # loguea al usuario
                return redirect('dashboard')
        else:
            form = SetPasswordForm(user)
        return render(request, 'accounts/activate.html', {'form': form})
    else:
        return render(request, 'accounts/activation_invalid.html')

@login_required
def dashboard(request):
    user = request.user
    context = {'user': user}
    return render(request, 'dashboard.html', context)

@login_required
def kofu_view(request):
    return render(request, "kofu.html")