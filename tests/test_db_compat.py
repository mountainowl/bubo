"""Public bubo.db facade compatibility after the state-layer split."""

from bubo import db
from bubo.db_reporting import findings_for
from bubo.db_schema import connect_db
from bubo.statuses import FindingStatus


def test_db_facade_preserves_reader_and_status_exports() -> None:
    assert db.connect_db is connect_db
    assert db.findings_for is findings_for
    assert db.FindingStatus is FindingStatus
