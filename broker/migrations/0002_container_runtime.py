from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("broker", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="appinstall",
            name="runtime",
            field=models.CharField(
                choices=[("external", "External"), ("container", "Container (bdh-supervisor)")],
                default="external", max_length=16),
        ),
        migrations.AddField(
            model_name="appinstall",
            name="approved_network",
            field=models.CharField(blank=True, max_length=16),
        ),
        migrations.AddField(
            model_name="appinstall",
            name="allow_lan",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="appinstall",
            name="approved_companions",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
