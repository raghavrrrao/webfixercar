"""Participant identity moves from the PC number to the participant.

A PC number identifies a machine in the lab, and a machine is used by one
participant after another all day. Making it unique meant the second person to
sit down at PC-14 was told "PC number already registered"; making it the login
field meant they would have signed in as the first person if they had.

So `username` becomes the unique login identity and `pc_no` becomes ordinary
metadata. Nobody is deleted and no race is reset: the de-duplication step only
renames participants who happened to share a display name, which is what the
new unique constraint needs before it can be applied.
"""

from django.db import migrations, models


def deduplicate_names(apps, schema_editor):
    """Give every existing participant a distinct name, keeping their record.

    Older rounds let several people register the same display name because it
    was never the identity. Suffix the later ones with the PC they raced on —
    and then a counter if that is still ambiguous — so the constraint applies
    without losing a single run.
    """
    User = apps.get_model('first', 'User')
    seen = set()
    for user in User.objects.order_by('registered_at', 'pk').iterator():
        name = (user.username or '').strip() or f'participant-{user.pk}'
        if name not in seen:
            seen.add(name)
            if name != user.username:
                user.username = name
                user.save(update_fields=['username'])
            continue

        candidate = f'{name} ({user.pc_no})' if user.pc_no else name
        suffix = 2
        while candidate in seen:
            candidate = f'{name} ({user.pc_no}) {suffix}' if user.pc_no else f'{name} {suffix}'
            suffix += 1
        seen.add(candidate)
        user.username = candidate[:100]
        user.save(update_fields=['username'])


def noop(apps, schema_editor):
    """Reversing only drops the constraint; the renames stay, harmlessly."""


class Migration(migrations.Migration):

    dependencies = [
        ('first', '0005_race_repair_progress'),
    ]

    operations = [
        # The PC becomes metadata: many participants share one machine.
        migrations.AlterField(
            model_name='user',
            name='pc_no',
            field=models.CharField(max_length=50, verbose_name='PC number'),
        ),
        # Names have to be distinct before they can be the identity.
        migrations.RunPython(deduplicate_names, noop),
        migrations.AlterField(
            model_name='user',
            name='username',
            field=models.CharField(max_length=100, unique=True,
                                   verbose_name='participant name'),
        ),
    ]
