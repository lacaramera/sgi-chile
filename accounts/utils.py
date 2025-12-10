from django.urls import reverse
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes
from django.contrib.auth.tokens import default_token_generator
from django.template.loader import render_to_string
from django.conf import settings
from django.core.mail import send_mail

def send_activation_email(user, request):
    """
    Envía un correo de activación con un token seguro que apunta a la vista 'activate'.
    En desarrollo el backend de email puede ser console, así que se imprimirá en la terminal.
    """
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    path = reverse('activate', kwargs={'uidb64': uid, 'token': token})
    activation_link = request.build_absolute_uri(path)
    subject = 'Activa tu cuenta — Soka Gakkai Chile'
    message = render_to_string('accounts/activation_email.txt', {
        'user': user,
        'activation_link': activation_link,
    })
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'no-reply@sokagakkai.cl')
    # send_mail devuelve el número de emails enviados
    send_mail(subject, message, from_email, [user.email])
