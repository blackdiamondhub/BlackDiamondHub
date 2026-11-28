from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hub_settings', '0006_speaker'),
    ]

    operations = [
        migrations.AddField(
            model_name='speaker',
            name='household',
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name='speaker',
            name='aliases',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
