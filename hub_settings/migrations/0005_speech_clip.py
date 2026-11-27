import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hub_settings', '0004_ha_instance'),
    ]

    operations = [
        migrations.CreateModel(
            name='SpeechClip',
            fields=[
                ('key', models.CharField(max_length=64, primary_key=True, serialize=False)),
                ('engine', models.CharField(max_length=16)),
                ('voice', models.CharField(blank=True, max_length=80)),
                ('text', models.TextField()),
                ('audio', models.BinaryField()),
                ('content_type', models.CharField(max_length=40)),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('last_used', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
            ],
            options={
                'ordering': ['-last_used'],
            },
        ),
    ]
