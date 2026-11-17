from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('display', '0006_display_load'),
    ]

    operations = [
        migrations.AddField(
            model_name='displayload',
            name='prev_stats',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name='DisplaySample',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('ip', models.GenericIPAddressField(db_index=True)),
                ('at', models.DateTimeField(db_index=True)),
                ('path', models.CharField(blank=True, max_length=200)),
                ('data', models.JSONField(default=dict)),
            ],
        ),
    ]
