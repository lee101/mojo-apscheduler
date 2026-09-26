import pathlib
import sys
from datetime import timezone

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "python"))

_LIB = _ROOT / "dist" / "libmojo-apscheduler.so"

if not _LIB.exists():
    pytest.skip(
        "libmojo-apscheduler.so not built; run `bash build/build.sh`",
        allow_module_level=True,
    )

UTC = timezone.utc


@pytest.fixture
def now():
    from datetime import datetime

    return datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC)
