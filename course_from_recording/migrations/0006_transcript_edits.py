from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('course_from_recording', '0005_start_over'),
    ]

    operations = [
        migrations.AddField(
            model_name='recordingjob',
            name='transcript_edits',
            field=models.JSONField(blank=True, default=dict, help_text='Set by the author: line number -> corrected text.'),
        ),
    ]
