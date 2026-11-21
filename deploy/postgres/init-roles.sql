\set ON_ERROR_STOP on
\getenv db POSTGRES_DB
\getenv migrator_password OVM_DB_MIGRATOR_PASSWORD
\getenv app_password OVM_DB_APP_PASSWORD

BEGIN;
SET LOCAL log_statement = 'none';
SET LOCAL log_min_error_statement = 'panic';
SET LOCAL log_min_duration_statement = -1;
SET LOCAL password_encryption = 'scram-sha-256';

CREATE ROLE overmind_migrator LOGIN PASSWORD :'migrator_password';
CREATE ROLE overmind_app LOGIN PASSWORD :'app_password';

REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
REVOKE CONNECT ON DATABASE postgres, template1 FROM PUBLIC;
GRANT CONNECT, CREATE ON DATABASE :"db" TO overmind_migrator;
GRANT CONNECT ON DATABASE :"db" TO overmind_app;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO overmind_migrator;
GRANT USAGE ON SCHEMA public TO overmind_app;
COMMIT;
