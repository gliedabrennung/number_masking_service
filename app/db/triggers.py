"""Database-side invariants that cannot be expressed as plain DDL.

``session_parties`` carries denormalised copies of three ``sessions`` columns so
that the uniqueness invariant can be a partial index — PostgreSQL rejects an
index predicate containing a subquery. The trigger below is what keeps the
copies honest.

Kept in one place so the Alembic migration and the test bootstrap use the same
definition.
"""

SYNC_FUNCTION = """
CREATE OR REPLACE FUNCTION sync_session_parties() RETURNS TRIGGER AS $$
BEGIN
    UPDATE session_parties
       SET is_active    = (NEW.status = 'active'),
           has_ext_code = (NEW.ext_code IS NOT NULL),
           released_at  = NEW.closed_at
     WHERE session_id = NEW.id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

SYNC_TRIGGER = """
CREATE TRIGGER trg_sync_session_parties
AFTER UPDATE OF status, ext_code, closed_at ON sessions
FOR EACH ROW
WHEN (
    OLD.status IS DISTINCT FROM NEW.status
 OR OLD.ext_code IS DISTINCT FROM NEW.ext_code
 OR OLD.closed_at IS DISTINCT FROM NEW.closed_at
)
EXECUTE FUNCTION sync_session_parties();
"""

DROP_TRIGGER = "DROP TRIGGER IF EXISTS trg_sync_session_parties ON sessions"
DROP_FUNCTION = "DROP FUNCTION IF EXISTS sync_session_parties()"
