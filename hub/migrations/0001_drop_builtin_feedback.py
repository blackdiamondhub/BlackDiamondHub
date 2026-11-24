"""Drop the old built-in ``feedback`` app's table and records.

Feedback now comes only from the feedback container app (bdh-app-feedback),
which keeps its messages in its own storage. Safe to run more than once.
"""
from django.apps import apps as global_apps
from django.db import migrations

APP_LABEL = "feedback"
TABLE = "feedback_feedback"


def drop_builtin_feedback(apps, schema_editor):
    # bdh-app-feedback's CI runs its own ``feedback`` app in-process with the
    # hub, and that app's table has the same name: leave it alone.
    if global_apps.is_installed(APP_LABEL):
        return
    connection = schema_editor.connection
    if TABLE in connection.introspection.table_names():
        # DROP TABLE ... CASCADE on Postgres, plain DROP TABLE on SQLite.
        schema_editor.execute(schema_editor.sql_delete_table % {"table": schema_editor.quote_name(TABLE)})
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM django_migrations WHERE app = %s", [APP_LABEL])
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    db = connection.alias
    Permission.objects.using(db).filter(content_type__app_label=APP_LABEL).delete()
    ContentType.objects.using(db).filter(app_label=APP_LABEL).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("admin", "0003_logentry_add_action_flag_choices"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(drop_builtin_feedback, migrations.RunPython.noop),
    ]