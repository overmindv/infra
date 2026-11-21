\set ON_ERROR_STOP on

BEGIN;
SELECT format(
    'GRANT %s ON TABLE %I.%I TO overmind_app',
    CASE
        WHEN c.relname = 'user_security_audit' THEN 'SELECT, INSERT'
        WHEN c.relkind IN ('v', 'm') THEN 'SELECT'
        ELSE 'SELECT, INSERT, UPDATE, DELETE'
    END,
    n.nspname, c.relname
)
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relowner = current_user::regrole
  AND c.relkind IN ('r', 'p', 'v', 'm')
  AND c.relname <> 'goose_db_version'
\gexec

SELECT format('GRANT USAGE, SELECT ON SEQUENCE %I.%I TO overmind_app', n.nspname, c.relname)
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relowner = current_user::regrole
  AND c.relkind = 'S'
  AND NOT EXISTS (
      SELECT 1 FROM pg_depend AS d
      WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
        AND d.refclassid = 'pg_class'::regclass
        AND d.refobjid = to_regclass('public.goose_db_version')
        AND d.deptype IN ('a', 'i')
  )
\gexec
COMMIT;
