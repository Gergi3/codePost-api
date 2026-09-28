# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0150_pendingagentaction_approval"),
    ]

    operations = [
        migrations.AlterField(
            model_name="environment",
            name="language",
            field=models.CharField(
                choices=[
                    ("python-3.12", "python-3.12"),
                    ("python-3.11", "python-3.11"),
                    ("python-3.10", "python-3.10"),
                    ("python-3.7", "python-3.7"),
                    ("python-2.7", "python-2.7"),
                    ("java", "java"),
                    ("java-17", "java-17"),
                    ("java-11", "java-11"),
                    ("c/c++", "c/c++"),
                    ("node-20", "node-20"),
                    ("node-18", "node-18"),
                    ("r-4", "r-4"),
                    ("ruby", "ruby"),
                    ("php", "php"),
                    ("other", "other"),
                ],
                default="python-3.7",
                max_length=25,
            ),
        ),
    ]
