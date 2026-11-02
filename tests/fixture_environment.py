import os
import tempfile
from pathlib import Path

# Both test modules must select the fixture before importing any app modules.
TEMP = tempfile.TemporaryDirectory()
os.environ['DATABASE_URL'] = 'sqlite:///' + str(Path(TEMP.name) / 'fixture.db')
os.environ['SECRET_KEY'] = 'fixture-only-session-secret'
os.environ['APP_READ_ONLY'] = 'true'
