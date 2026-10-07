/*
  GrainTime: TEMPORARY discovery login for ONE site database.

  Run this in SSMS as a sysadmin at the discovery site (site preparation; the
  GrainTime app itself is configured entirely in the browser). It creates a
  SQL-authentication login that can read the database's tables and catalog, and
  nothing else. It is only for Phase 0 discovery; once the mapping is
  confirmed, a narrower login (SELECT on the mapped tables only) replaces it,
  and you should run the DROP section at the bottom.

  Before running:
    1. Replace CompuWeighDB with the real database name (two places).
    2. Replace the password with a strong one. Do not reuse it elsewhere.
    3. SQL authentication (mixed mode) must be enabled on the instance:
       Server Properties > Security > "SQL Server and Windows Authentication mode",
       then restart the SQL Server service.

  This script only creates a login and a database user with read
  permissions. It changes no data and no schema objects.
*/

USE [master];
GO
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'graintime_discovery')
    CREATE LOGIN [graintime_discovery]
        WITH PASSWORD = N'CHANGE-ME-to-a-strong-password',
             DEFAULT_DATABASE = [CompuWeighDB],
             CHECK_POLICY = ON, CHECK_EXPIRATION = OFF;
GO

USE [CompuWeighDB];
GO
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'graintime_discovery')
    CREATE USER [graintime_discovery] FOR LOGIN [graintime_discovery];
GO
-- Read-only: SELECT on every table/view in this database. No write permissions.
ALTER ROLE [db_datareader] ADD MEMBER [graintime_discovery];
GO
-- Lets the script see definitions of indexes, triggers and keys.
GRANT VIEW DEFINITION TO [graintime_discovery];
GO

/*
  OPTIONAL (instance level): lets the report show whether this connection is
  encrypted. Discovery works without it. Skip it if you prefer.

  USE [master];
  GRANT VIEW SERVER STATE TO [graintime_discovery];
*/

/* ---------------------------------------------------------------------------
   ALTERNATIVE: use a Windows (domain) account instead of a SQL login.
   In GrainTime choose "Sign in with: Windows account (domain)". Mixed mode is
   then not needed. Replace MERCER\svc_graintime with the real account.

   USE [master];
   CREATE LOGIN [MERCER\svc_graintime] FROM WINDOWS WITH DEFAULT_DATABASE = [CompuWeighDB];
   USE [CompuWeighDB];
   CREATE USER [MERCER\svc_graintime] FOR LOGIN [MERCER\svc_graintime];
   ALTER ROLE [db_datareader] ADD MEMBER [MERCER\svc_graintime];
   GRANT VIEW DEFINITION TO [MERCER\svc_graintime];
--------------------------------------------------------------------------- */

/* ---------------------------------------------------------------------------
   CLEAN UP after discovery (run when the mapping has been confirmed):

   USE [CompuWeighDB];
   DROP USER IF EXISTS [graintime_discovery];
   USE [master];
   IF EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'graintime_discovery')
       DROP LOGIN [graintime_discovery];
--------------------------------------------------------------------------- */
