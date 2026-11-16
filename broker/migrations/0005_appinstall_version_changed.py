from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("broker", "0004_appsubscription"),
    ]

    operations = [
        migrations.AddField(
            model_name="appinstall",
            name="version_changed",
            field=models.DateTimeField(blank=True, help_text="When an update last changed the version.", null=True),
        ),
    ]
