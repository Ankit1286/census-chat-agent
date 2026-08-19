-- Run in a Snowsight worksheet (interactive UI - handles MFA normally) as
-- ACCOUNTADMIN, or a role with CREATE ROLE/USER/WAREHOUSE privileges.
--
-- Creates a least-privilege service identity for the chat app, using
-- key-pair authentication rather than a password: Snowflake's mandatory MFA
-- policy on new accounts blocks plain password logins from non-interactive
-- clients (our discovery script, the deployed Streamlit app), but key-pair
-- auth is exempt from that policy and is Snowflake's recommended approach
-- for service/programmatic connections regardless.

CREATE ROLE IF NOT EXISTS CENSUS_APP_ROLE;

CREATE WAREHOUSE IF NOT EXISTS CENSUS_APP_WH
  WAREHOUSE_SIZE = XSMALL
  AUTO_SUSPEND = 60
  AUTO_RESUME = TRUE
  INITIALLY_SUSPENDED = TRUE;

GRANT USAGE ON WAREHOUSE CENSUS_APP_WH TO ROLE CENSUS_APP_ROLE;

-- Marketplace shares are read-only by nature, so IMPORTED PRIVILEGES is the
-- correct grant (not per-object SELECT grants).
GRANT IMPORTED PRIVILEGES ON DATABASE US_OPEN_CENSUS_DATA_NEIGHBORHOOD_INSIGHTS_FREE_DATASET
  TO ROLE CENSUS_APP_ROLE;

CREATE USER IF NOT EXISTS CENSUS_APP_USER
  RSA_PUBLIC_KEY = 'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAsA20Kqrb/QagFftLK8aag43KGyhT2371hpOGEXfUYluLuULD8rEZNftBuzomLOmv+YbIzXmEAsPqwkdf8THHK1NVGD0zNFnYLcwJqX4ISFWpewmx3a6g53qxCL2kSM9A/z5tOox0kpDp4MkjAIaiTrTxp5atLcAaBfNofSwpiX3F88F8JnDXv4y/qXDvCOH0/8327ebxGmB4l6BOLUVoh8+7uYWGB0Wbae+TICXQQtT+zfNSlsDO+pIR/dMHyFQPJNpHZl9qEKCrv8NFwF7AOc0lT86BqXFGNdjdVk24YD2Jp7O8R+lMM6zC0u6ZG2gF9JBZlMbrDjUeochQalaSvwIDAQAB'
  DEFAULT_ROLE = CENSUS_APP_ROLE
  DEFAULT_WAREHOUSE = CENSUS_APP_WH
  MUST_CHANGE_PASSWORD = FALSE;

GRANT ROLE CENSUS_APP_ROLE TO USER CENSUS_APP_USER;

-- Sanity check: run as CENSUS_APP_USER / CENSUS_APP_ROLE and confirm you can
-- SELECT from the Census tables but cannot CREATE/INSERT/DROP anything.
