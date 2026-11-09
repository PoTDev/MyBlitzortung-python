# SPDX-License-Identifier: GPL-3.0-or-later
import pytest

from mybo.db import engine_for, schema


@pytest.fixture
def db():
    engine = engine_for("sqlite://")
    schema.create_all(engine)
    yield engine
    engine.dispose()
