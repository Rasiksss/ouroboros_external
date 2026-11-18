# Validation of the deployment-owned schema

Validated locally against PostgreSQL 16 on an isolated database, using synthetic
credentials only. No corporate database was modified.

- Applied the original GigaAgent revisions 0001–0034 to a clean reference DB
  before removing Alembic from this checkout.
- Applied the combined AIFA SQL changelog to another clean database.
- Compared pg_dump schema output for all 48 GigaAgent application tables:
  definitions, defaults, identity sequence, checks, indexes and foreign keys match.
- The combined schema has 71 tables: 22 AIFA tables, 48 GigaAgent application
  tables and the deployment-owned GigaAgent version marker. No alembic_version.
- All ten AIFA SQLAlchemy metadata collections match the deployed tables/columns.
- A separate runtime role has USAGE and DML/sequence rights, without CREATE or
  ownership. AIFA initializers and GigaAgent version validation succeed under it;
  user/user_settings writes and reads succeed. CREATE and DROP are denied.
- Targeted tests: 10 GigaAgent deployment/schema tests and 38 AIFA schema,
  connection and secret-loading tests pass. uv lock check and frozen export pass;
  Alembic is not in the exported runtime dependencies.

This baseline is intentionally for the first deployment, as confirmed by the
operator. Existing GigaAgent installations need a separate data-preserving upgrade.
DDL must run through the AEF Liquibase job with a migration role before app pods.
Corporate AEF orchestration and its grants must still be checked on the stand.

Liquibase CLI execution was attempted, but its Docker image download stalled.
The SQL was executed directly on PostgreSQL; the actual AEF Liquibase job still
needs validation on the stand. No successful CLI run is claimed here.
