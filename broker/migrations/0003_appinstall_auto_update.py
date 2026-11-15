from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("broker", "0002_container_runtime"),
    ]

    operations = [
        migrations.AddField(
            model_name="appinstall",
            name="auto_update",
            field=models.BooleanField(default=True, help_text="Install new catalog versions overnight."),
        ),
    ]
