import django.db.models.deletion
from django.db import migrations, models


def backfill_location(apps, schema_editor):
    Restaurant = apps.get_model("outlets", "Restaurant")
    Location = apps.get_model("outlets", "Location")
    fr = Location.objects.get(slug="fr")
    Restaurant.objects.filter(location__isnull=True).update(location=fr)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("outlets", "0002_location"),
    ]

    operations = [
        migrations.AddField(
            model_name="restaurant",
            name="contact_number",
            field=models.CharField(blank=True, max_length=20, default=""),
        ),
        migrations.AddField(
            model_name="restaurant",
            name="location",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="restaurants",
                to="outlets.location",
            ),
        ),
        migrations.RunPython(backfill_location, noop),
        migrations.AlterField(
            model_name="restaurant",
            name="location",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="restaurants",
                to="outlets.location",
            ),
        ),
    ]
