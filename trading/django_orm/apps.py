from django.apps import AppConfig


class DjangoOrmConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "django_orm"
    label = "django_orm"
    verbose_name = "Crypto ORM"
