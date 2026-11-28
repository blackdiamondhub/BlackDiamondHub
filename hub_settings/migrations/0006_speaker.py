from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hub_settings', '0005_speech_clip'),
    ]

    operations = [
        migrations.AddField(
            model_name='speechclip',
            name='kind',
            field=models.CharField(choices=[('speech', 'Speech'), ('announce', 'Chime and speech'),
                                            ('custom', 'App clip')], default='speech', max_length=16),
        ),
        migrations.CreateModel(
            name='Speaker',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('native_id', models.CharField(max_length=200, unique=True)),
                ('kind', models.CharField(choices=[('sonos', 'Sonos'), ('alexa', 'Alexa'), ('ha', 'Home Assistant')],
                                          max_length=16)),
                ('driver', models.CharField(max_length=16)),
                ('name', models.CharField(max_length=200)),
                ('legacy_id', models.CharField(blank=True, db_index=True, max_length=200, null=True)),
                ('caps', models.JSONField(default=dict)),
                ('online', models.BooleanField(default=True)),
                ('last_seen', models.DateTimeField(blank=True, null=True)),
                ('address', models.CharField(blank=True, max_length=64)),
                ('ha_instance', models.CharField(blank=True, max_length=64)),
                ('ha_entity', models.CharField(blank=True, max_length=200)),
            ],
            options={
                'ordering': ['name'],
            },
        ),
    ]
