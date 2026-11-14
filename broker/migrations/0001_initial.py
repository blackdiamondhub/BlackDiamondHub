from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="AppInstall",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("app_id", models.SlugField(max_length=64, unique=True)),
                ("version", models.CharField(blank=True, max_length=64)),
                ("generation", models.PositiveIntegerField(default=1)),
                ("port", models.PositiveIntegerField(help_text="Public port the hub proxies to this app.", unique=True)),
                ("upstream", models.CharField(blank=True, help_text='Where the container listens, e.g. "http://127.0.0.1:9201".', max_length=200)),
                ("network", models.CharField(blank=True, max_length=32)),
                ("lifecycle", models.CharField(blank=True, max_length=32)),
                ("memory_mb", models.PositiveIntegerField(blank=True, null=True)),
                ("requested_permissions", models.JSONField(blank=True, default=list)),
                ("granted_permissions", models.JSONField(blank=True, default=list)),
                ("enabled", models.BooleanField(default=True)),
                ("manifest", models.JSONField(blank=True, null=True)),
                ("manifest_fetched_at", models.DateTimeField(blank=True, null=True)),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["port"],
            },
        ),
        migrations.CreateModel(
            name="AuditEntry",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("app_id", models.CharField(db_index=True, max_length=64)),
                ("op", models.CharField(max_length=64)),
                ("allowed", models.BooleanField()),
                ("detail", models.CharField(blank=True, max_length=500)),
                ("created", models.DateTimeField(auto_now_add=True, db_index=True)),
            ],
            options={
                "verbose_name_plural": "audit entries",
                "ordering": ["-id"],
            },
        ),
    ]
