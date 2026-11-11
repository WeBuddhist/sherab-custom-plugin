from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('course_from_recording', '0006_transcript_edits'),
    ]

    operations = [
        migrations.AddField(
            model_name='recordingjob',
            name='course_draft',
            field=models.JSONField(blank=True, default=dict, help_text='The course outline and lessons, as the author last saved them.'),
        ),
        migrations.AddField(
            model_name='recordingjob',
            name='course_approved_at',
            field=models.DateTimeField(blank=True, null=True, help_text='When the author confirmed the teacher has reviewed the outline.'),
        ),
        migrations.AddField(
            model_name='recordingjob',
            name='course_sections',
            field=models.JSONField(blank=True, default=list, help_text='The sections written into the course (usage keys).'),
        ),
        migrations.AlterField(
            model_name='recordingjob',
            name='status',
            field=models.CharField(choices=[('uploading', 'Waiting for the upload'), ('queued', 'Waiting to start'), ('cleaning', 'Cleaning the audio'), ('finding_speakers', 'Finding the speakers'), ('needs_speaker_labels', 'Waiting for you to tag the voices'), ('transcribing', 'Transcribing'), ('transcript_ready', 'Transcript ready'), ('suggesting', 'Suggesting a course'), ('course_draft_ready', 'Waiting for you to shape the course'), ('cutting_audio', 'Cutting the lesson audio'), ('writing_course', 'Adding the course to Studio'), ('course_ready', 'Draft course ready'), ('failed', 'Failed'), ('cancelled', 'Started over')], default='uploading', max_length=32),
        ),
    ]
