"""Tests de la fonction _validate_sql() qui protège l'endpoint /v1/ai/ask.

C'est un test critique pour la sécurité : si la regex de validation laisse
passer un INSERT/DROP/etc., l'IA peut détruire la DB.
"""

import pytest

from src.api.v1.ai import _ALLOWED_TABLES, _validate_sql

# ----------------------------------------------------------------------
# SQL valide
# ----------------------------------------------------------------------


class TestValidSql:
    def test_simple_select(self):
        sql = "SELECT * FROM accounts"
        assert _validate_sql(sql) == "SELECT * FROM accounts"

    def test_select_with_where(self):
        sql = "SELECT * FROM transactions WHERE transaction_date >= '2026-01-01'"
        out = _validate_sql(sql)
        assert "SELECT" in out

    def test_select_with_join(self):
        sql = "SELECT t.* FROM transactions t JOIN accounts a ON a.id = t.account_id"
        assert _validate_sql(sql)

    def test_with_cte(self):
        sql = (
            "WITH recent AS (SELECT * FROM transactions WHERE transaction_date > '2026-01-01') "
            "SELECT * FROM recent"
        )
        assert _validate_sql(sql)

    def test_lowercase(self):
        sql = "select * from accounts"
        assert _validate_sql(sql) == "select * from accounts"

    def test_strips_trailing_semicolon(self):
        sql = "SELECT * FROM accounts;"
        out = _validate_sql(sql)
        assert not out.endswith(";")

    def test_strips_whitespace(self):
        sql = "   SELECT * FROM accounts   "
        out = _validate_sql(sql)
        assert out.startswith("SELECT")
        assert out.endswith("accounts")


# ----------------------------------------------------------------------
# SQL invalide — non-SELECT
# ----------------------------------------------------------------------


class TestInvalidNonSelect:
    def test_empty(self):
        with pytest.raises(ValueError, match="vide"):
            _validate_sql("")
        with pytest.raises(ValueError, match="vide"):
            _validate_sql("   ")

    def test_starts_with_other_keyword(self):
        # SET n'est pas dans les mots-clés interdits, mais ne commence pas par SELECT/WITH
        with pytest.raises(ValueError, match="SELECT"):
            _validate_sql("SET search_path = public")

    def test_pure_function_call(self):
        # pg_sleep est maintenant dans la liste des mots-clés interdits (DoS).
        # Avant ce fix, ça tombait sur le check SELECT ; maintenant, c'est rejeté plus tôt.
        with pytest.raises(ValueError, match="interdit"):
            _validate_sql("pg_sleep(1)")


# ----------------------------------------------------------------------
# SQL invalide — mots-clés interdits
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden",
    [
        "INSERT INTO accounts VALUES (...)",
        "SELECT * FROM accounts; DELETE FROM accounts",
        "SELECT * FROM accounts; DROP TABLE accounts",
        "SELECT * FROM accounts; UPDATE accounts SET nickname='x'",
        "SELECT * FROM accounts; TRUNCATE accounts",
        "SELECT * FROM accounts; ALTER TABLE accounts ADD COLUMN x TEXT",
        "SELECT * FROM accounts; CREATE TABLE evil (x INT)",
        "SELECT * FROM accounts; GRANT ALL ON accounts TO public",
        "SELECT * FROM accounts; REVOKE ALL FROM hub",
        "SELECT * FROM accounts; COPY accounts TO STDOUT",
        "SELECT * FROM accounts; VACUUM accounts",
    ],
)
def test_forbidden_keywords_rejected(forbidden):
    with pytest.raises(ValueError, match="interdit"):
        _validate_sql(forbidden)


# ----------------------------------------------------------------------
# SQL invalide — tables non whitelistées
# ----------------------------------------------------------------------


class TestTableWhitelist:
    def test_unknown_table_rejected(self):
        with pytest.raises(ValueError, match="non autoris"):
            _validate_sql("SELECT * FROM secret_users")

    def test_unknown_join_table_rejected(self):
        with pytest.raises(ValueError, match="non autoris"):
            _validate_sql("SELECT * FROM accounts a JOIN admin_secrets s ON a.id = s.user_id")

    def test_all_whitelisted_tables_accepted(self):
        # Chaque table whitelistée doit pouvoir être utilisée
        for tbl in _ALLOWED_TABLES:
            sql = f"SELECT * FROM {tbl}"
            assert _validate_sql(sql)


# ----------------------------------------------------------------------
# SQL injection patterns
# ----------------------------------------------------------------------


class TestSqlInjectionPatterns:
    def test_classic_drop_table(self):
        with pytest.raises(ValueError):
            _validate_sql("'; DROP TABLE accounts; --")

    def test_union_with_unauthorized_table(self):
        with pytest.raises(ValueError, match="non autoris"):
            _validate_sql("SELECT * FROM accounts UNION SELECT * FROM pg_user")
