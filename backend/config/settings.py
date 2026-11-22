"""Django settings. Everything environment-specific comes from environment variables."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BASE_DIR.parent


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in ("1", "true", "yes", "on")


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = [h.strip() for h in env("DJANGO_ALLOWED_HOSTS", "*").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [o.strip() for o in env("DJANGO_CSRF_TRUSTED_ORIGINS", "http://localhost:8080,http://127.0.0.1:8080,http://localhost:5173,http://127.0.0.1:5173").split(",") if o.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "payroll",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"

DATA_DIR = Path(env("PAYROLL_DATA_DIR", REPO_DIR / "var"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

if env("POSTGRES_HOST"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "HOST": env("POSTGRES_HOST"),
            "PORT": env("POSTGRES_PORT", "5432"),
            "NAME": env("POSTGRES_DB", "payroll"),
            "USER": env("POSTGRES_USER", "payroll"),
            "PASSWORD": env("POSTGRES_PASSWORD", "payroll"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": env("SQLITE_PATH", str(DATA_DIR / "payroll.sqlite3")),
            # WAL lets the API and the workflow worker use the file at the same time.
            "OPTIONS": {"init_command": "PRAGMA journal_mode=WAL;", "timeout": 30},
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_PASSWORD_VALIDATORS = []
LANGUAGE_CODE = "en-in"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = False
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = DATA_DIR / "static"
FRONTEND_DIST = REPO_DIR / "frontend" / "dist"
WHITENOISE_ROOT = FRONTEND_DIST if FRONTEND_DIST.exists() else None
WHITENOISE_INDEX_FILE = True
MEDIA_ROOT = DATA_DIR / "media"
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
DATA_UPLOAD_MAX_MEMORY_SIZE = 20 * 1024 * 1024

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "EXCEPTION_HANDLER": "payroll.api.errors.exception_handler",
    "UNAUTHENTICATED_USER": None,
}
SESSION_COOKIE_AGE = 60 * 60 * 12
# Browsers share cookies across ports, so Django's default names would clash with any other Django
# app on the same host (Horilla on :8000 signs you out of this app and vice versa).
SESSION_COOKIE_NAME = "payroll_sessionid"
CSRF_COOKIE_NAME = "payroll_csrftoken"

# --- workflow -----------------------------------------------------------------
# "temporal": months run as Temporal workflows (durable, survives restarts of every service).
# "local":    the same steps run in-process; state lives only in the database (still restart-safe).
WORKFLOW_BACKEND = env("WORKFLOW_BACKEND", "temporal")
TEMPORAL_ADDRESS = env("TEMPORAL_ADDRESS", "localhost:7233")
TEMPORAL_NAMESPACE = env("TEMPORAL_NAMESPACE", "default")
TEMPORAL_TASK_QUEUE = env("TEMPORAL_TASK_QUEUE", "payroll-months")
TEMPORAL_UI_URL = env("TEMPORAL_UI_URL", "http://localhost:8233")

# --- AI -----------------------------------------------------------------------
# AI_PROVIDER: "anthropic" (Claude API), "claude-code" (Claude Code CLI, headless), "off" (rule-based
# suggestions only) or "auto" (API key -> anthropic; else `claude` on PATH -> claude-code; else rule-based).
AI_PROVIDER = env("AI_PROVIDER", "auto")
AI_MODEL = env("AI_MODEL", "claude-opus-5-5")
AI_EFFORT = env("AI_EFFORT", "low")
AI_TIMEOUT_SECONDS = float(env("AI_TIMEOUT_SECONDS", "180"))
# Claude Code headless mode (AI_PROVIDER=claude-code, or picked by "auto" when no API key is set and
# the `claude` CLI is installed and signed in). Not meant for production: it uses the CLI's own login.
# The CLI is for development only: it runs on one person's login. It is never used when DEBUG is off unless
# AI_ALLOW_CLI=1; production uses the Claude API (AI_PROVIDER=anthropic with ANTHROPIC_API_KEY) or no AI.
AI_ALLOW_CLI = env_bool("AI_ALLOW_CLI", False)
CLAUDE_CODE_BIN = env("CLAUDE_CODE_BIN", "claude")
CLAUDE_CODE_MODEL = env("CLAUDE_CODE_MODEL", "")  # empty = the CLI's default model

# --- browser agent ------------------------------------------------------------
AGENT_HEADLESS = env_bool("AGENT_HEADLESS", True)
AGENT_MAX_PAGES_PER_DATASET = int(env("AGENT_MAX_PAGES_PER_DATASET", "50"))
AGENT_NAV_TIMEOUT_MS = int(env("AGENT_NAV_TIMEOUT_MS", "30000"))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "loggers": {
        "payroll": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
        "temporalio": {"handlers": ["console"], "level": "WARNING"},
    },
}
