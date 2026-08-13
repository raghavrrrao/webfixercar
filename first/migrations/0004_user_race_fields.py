from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('first', '0003_user_registered_at_finalsubmission_hintreveal'),
    ]

    operations = [
        migrations.AddField(
            model_name='user', name='race_started_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='user', name='race_completed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='user', name='race_time_seconds',
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='user', name='race_obstacles',
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='user', name='race_collisions',
            field=models.PositiveSmallIntegerField(default=0),
        ),
    ]
