from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("broker", "0003_appinstall_auto_update"),
    ]

    operations = [
        migrations.CreateModel(
            name="AppSubscription",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("app_id", models.CharField(db_index=True, max_length=64)),
                ("kind", models.CharField(max_length=16)),
                ("instance", models.CharField(blank=True, max_length=64)),
                ("payload", models.JSONField(default=list)),
                ("updated", models.DateTimeField(auto_now=True)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(fields=("app_id", "kind", "instance"),
                                            name="broker_appsubscription_unique"),
                ],
            },
        ),
    ]
