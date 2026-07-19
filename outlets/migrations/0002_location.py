from django.db import migrations, models


def seed_locations(apps, schema_editor):
    Location = apps.get_model("outlets", "Location")
    Location.objects.get_or_create(slug="fr", defaults={"name": "Food Republic"})
    Location.objects.get_or_create(slug="pentagon", defaults={"name": "Pentagon"})


def unseed_locations(apps, schema_editor):
    Location = apps.get_model("outlets", "Location")
    Location.objects.filter(slug__in=["fr", "pentagon"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("outlets", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Location",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=100)),
                (
                    "slug",
                    models.SlugField(blank=True, max_length=120, unique=True),
                ),
            ],
        ),
        migrations.RunPython(seed_locations, unseed_locations),
    ]
