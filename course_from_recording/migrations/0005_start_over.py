from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('course_from_recording', '0004_job_folder'),
    ]

    operations = [
        migrations.AddField(
            model_name='recordingjob',
            name='recording_kept',
            field=models.BooleanField(default=False, help_text='Started over keeping the recording: it is still in S3, for the next job to reuse.'),
        ),
        migrations.AlterField(
            model_name='recordingjob',
            name='status',
            field=models.CharField(choices=[('uploading', 'Waiting for the upload'), ('queued', 'Waiting to start'), ('cleaning', 'Cleaning the audio'), ('finding_speakers', 'Finding the speakers'), ('needs_speaker_labels', 'Waiting for you to tag the voices'), ('transcribing', 'Transcribing'), ('transcript_ready', 'Transcript ready'), ('failed', 'Failed'), ('cancelled', 'Started over')], default='uploading', max_length=32),
        ),
    ]
