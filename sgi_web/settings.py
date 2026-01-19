from pathlib import Path
import dj_database_url
import os
from dotenv import load_dotenv
load_dotenv()


BASE_DIR = Path(__file__).resolve().parent.parent

# === ARCHIVOS SUBIDOS POR USUARIOS ===
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-secret-key")
DEBUG = os.getenv("DEBUG", "True").lower() == "true"

def _split_env_list(name: str, default: str = ""):
    value = os.getenv(name, default)
    return [x.strip() for x in value.split(",") if x.strip()]

ALLOWED_HOSTS = _split_env_list("ALLOWED_HOSTS", "127.0.0.1,localhost")

railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN")
if railway_domain:
    railway_domain = railway_domain.strip()
    if railway_domain not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(railway_domain)



CSRF_TRUSTED_ORIGINS = [
    o.strip()
    for o in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",")
    if o.strip()
]

# Si usas https detrás del proxy de Railway
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    

    # terceros
    'rest_framework',

    # apps locales
    
    "accounts.apps.AccountsConfig",

]
LOGOUT_REDIRECT_URL = "/accounts/login/"
LOGIN_URL = "/accounts/login/"

LOGIN_REDIRECT_URL = 'home'


MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'sgi_web.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / "templates"],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'accounts.context_processors.notifications',

            ],
        },
    },
]

WSGI_APPLICATION = 'sgi_web.wsgi.application'

DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    # Railway / Producción (Postgres)
    DATABASES = {
        "default": dj_database_url.parse(
            DATABASE_URL,
            conn_max_age=600,
            ssl_require=True,  # Railway normalmente usa SSL
        )
    }
else:
    # Local (SQLite)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }


AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',},
]

LANGUAGE_CODE = 'es-cl'
TIME_ZONE = 'America/Santiago'
USE_I18N = True
USE_L10N = True
USE_TZ = True

STATIC_URL = '/static/'

BASE_DIR = Path(__file__).resolve().parent.parent

STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Usar el modelo custom que vamos a crear
AUTH_USER_MODEL = 'accounts.User'

# === EMAIL (modo desarrollo: muestra los correos en la consola) ===
# EMAIL - producción (SMTP)
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"

EMAIL_HOST = os.getenv("EMAIL_HOST")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD")
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "True").lower() == "true"

DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "SGI Chile <no-reply@sgi-chile.cl>")

if DEBUG:
    print("SMTP USER:", EMAIL_HOST_USER)
    print("SMTP PASS SET:", bool(EMAIL_HOST_PASSWORD))
