"""Load-order and scoping rules for the DuckDB view builder.

``database/build.py`` is the one module with no other test coverage, and its two
rules are both invisible until they break something: the numeric prefixes on the
SQL files encode dependency order, and ``sql/queries/`` holds bare SELECTs that
must never be executed as DDL.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from finmind_pipeline.database import build


def test_queries_dir_is_excluded():
    names = {p.name for p in build.sql_files()}
    assert "examples.sql" not in names


def test_files_are_ordered_by_numeric_prefix():
    names = [p.name for p in build.sql_files()]
    assert names == sorted(names)
    # The prefixes are the dependency order: stock_info defines v_securities_all,
    # which 23_ joins, and 27_ wraps the views 23_/24_ define.
    assert names.index("21_v_stock_info.sql") < names.index("23_v_daily_prices.sql")
    assert names.index("23_v_daily_prices.sql") < names.index("24_v_adj_daily_prices.sql")
    assert names.index("24_v_adj_daily_prices.sql") < names.index("27_macros.sql")


def test_ordering_is_by_filename_not_path(monkeypatch, tmp_path):
    """A new subdirectory must not reorder the load.

    Sorting whole paths would put ``marts/30_`` before ``views/20_`` because the
    directory name is compared first, silently breaking dependency order.
    """
    (tmp_path / "views").mkdir()
    (tmp_path / "marts").mkdir()
    (tmp_path / "queries").mkdir()
    (tmp_path / "views" / "20_a.sql").write_text("-- a", encoding="utf-8")
    (tmp_path / "marts" / "30_b.sql").write_text("-- b", encoding="utf-8")
    (tmp_path / "queries" / "examples.sql").write_text("SELECT 1;", encoding="utf-8")

    monkeypatch.setattr(build, "SQL_DIR", tmp_path)
    assert [p.name for p in build.sql_files()] == ["20_a.sql", "30_b.sql"]


def test_nested_queries_dir_is_excluded(monkeypatch, tmp_path):
    nested = tmp_path / "queries" / "adhoc"
    nested.mkdir(parents=True)
    (nested / "40_scratch.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "20_a.sql").write_text("-- a", encoding="utf-8")

    monkeypatch.setattr(build, "SQL_DIR", tmp_path)
    assert [p.name for p in build.sql_files()] == ["20_a.sql"]


def test_empty_dir_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(build, "SQL_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        build.sql_files()


def test_render_uses_forward_slashes():
    """DuckDB treats a backslash as an escape inside a string literal."""
    out = build.render("read_parquet('{{DB_ROOT}}/x.parquet')", Path(r"D:\a\b"))
    assert out == "read_parquet('D:/a/b/x.parquet')"
    assert build.DB_ROOT_TOKEN not in out


def test_every_sql_file_consumes_the_token():
    """A file that hardcodes a path would silently bind to the wrong db_root."""
    for path in build.sql_files():
        text = path.read_text(encoding="utf-8")
        if "read_parquet" in text or "read_json_auto" in text:
            assert build.DB_ROOT_TOKEN in text, f"{path.name} reads files without {{DB_ROOT}}"
