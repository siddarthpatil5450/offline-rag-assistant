"""
ask_db.py — SQLite-based structured data engine.

Replaces ask_data.py's per-query Excel loading with a real relational database.
Data is loaded ONCE into SQLite tables, then queried with actual SQL.

Commands:
    register <table_name> <filepath>   Load an Excel/CSV file into a SQL table
    list                                Show all tables and row counts
    schema <table_name>                 Show a table's columns and types
    query <table_name> "<question>"     Ask a question in plain English (keyword-based -> SQL)
    sql "<raw SQL query>"               Run raw SQL directly (power user mode)
"""

import re
import sqlite3
import argparse
from pathlib import Path

DB_FILE = Path(__file__).parent / "logistics.db"

OLLAMA_URL   = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3.2"
# ── Connection ───────────────────────────────────────────────────────────────
def get_connection():
    return sqlite3.connect(str(DB_FILE))


def sanitize_column(name):
    """SQL column names can't have spaces, dots, or special characters."""
    name = str(name).strip()
    name = re.sub(r"[^0-9a-zA-Z_]", "_", name)
    if name and name[0].isdigit():
        name = "col_" + name
    return name or "unnamed"

def query_ollama_for_sql(question, table, schema_text):
    import requests
    prompt = (
        "You are a SQLite expert. Write ONE SQL query that answers the question.\n"
        "Rules:\n"
        "- Only output the raw SQL query, nothing else. No explanation, no markdown, no code fences.\n"
        "- Only write a SELECT statement. Never write UPDATE, DELETE, INSERT, or DROP.\n"
        f"- The table is named '{table}' with these columns:\n{schema_text}\n\n"
        f"Question: {question}\n\n"
        "SQL query:"
    )
    resp = requests.post(
        OLLAMA_URL,
        json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
        timeout=60
    )
    raw_sql = resp.json().get("response", "").strip()
    raw_sql = raw_sql.replace("```sql", "").replace("```", "").strip()
    return raw_sql



# ── Register: load a file into a SQL table ──────────────────────────────────
def cmd_register(args):
    import pandas as pd

    path = Path(args.filepath)
    if not path.exists():
        print(f"File not found: {args.filepath}")
        return

    ext = path.suffix.lower()
    if ext in (".xlsx", ".xls"):
        df = pd.read_excel(path, engine="openpyxl")
    elif ext == ".csv":
        df = pd.read_csv(path)
    else:
        print(f"Unsupported file type: {ext}")
        return

    # Clean column names so SQL can use them safely
    original_cols = list(df.columns)
    df.columns = [sanitize_column(c) for c in df.columns]

    conn = get_connection()
    df.to_sql(args.table, conn, if_exists="replace", index=False)
    conn.close()

    print(f"Loaded '{path.name}' -> table '{args.table}'")
    print(f"  {len(df)} rows x {len(df.columns)} columns")
    print(f"\n  Columns (original -> SQL-safe name):")
    for orig, safe in zip(original_cols, df.columns):
        marker = "  (renamed)" if orig != safe else ""
        print(f"    {orig}  ->  {safe}{marker}")


# ── List: show all tables ───────────────────────────────────────────────────
def cmd_list(args):
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cur.fetchall()]

    if not tables:
        print("No tables in database yet. Run: python ask_db.py register <table> <file>")
        conn.close()
        return

    print(f"\n{len(tables)} table(s) in {DB_FILE.name}:")
    for t in tables:
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        count = cur.fetchone()[0]
        cur.execute(f"PRAGMA table_info({t})")
        num_cols = len(cur.fetchall())
        print(f"  [{t}]  {count} rows x {num_cols} columns")
    conn.close()


# ── Schema: show a table's structure ────────────────────────────────────────
def cmd_schema(args):
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute(f"PRAGMA table_info({args.table})")
    columns = cur.fetchall()
    conn.close()

    if not columns:
        print(f"Table '{args.table}' not found. Run: python ask_db.py list")
        return

    print(f"\nSchema for '{args.table}':")
    for col in columns:
        # col = (index, name, type, notnull, default, is_primary_key)
        print(f"  {col[1]:<30s} {col[2]}")


# ── Raw SQL: power-user escape hatch ────────────────────────────────────────
def cmd_sql(args):
    import pandas as pd
    query      = " ".join(args.query)
    is_select  = query.strip().upper().startswith("SELECT")
    conn       = get_connection()
    try:
        if is_select:
            df = pd.read_sql_query(query, conn)
            print(f"\n{len(df)} row(s):\n")
            print(df.to_string(index=False))
        else:
            # UPDATE / INSERT / DELETE don't return rows — use a plain
            # cursor instead of pandas, and commit so the change is saved.
            cur = conn.cursor()
            cur.execute(query)
            conn.commit()
            print(f"\nOK — {cur.rowcount} row(s) affected.")
    except Exception as e:
        print(f"SQL Error: {e}")
    conn.close()


# ── Query: plain-English question -> SQL ────────────────────────────────────
def get_columns(conn, table):
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cur.fetchall()]


def get_numeric_columns(conn, table):
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    # SQLite types: INTEGER, REAL, TEXT, BLOB
    return [row[1] for row in cur.fetchall() if row[2] in ("INTEGER", "REAL")]


def find_matching_columns(columns, words):
    matches = []
    for col in columns:
        if any(w in col.lower() for w in words):
            matches.append(col)
    return matches


def cmd_query(args):
    conn    = get_connection()
    columns = get_columns(conn, args.table)
    if not columns:
        print(f"Table '{args.table}' not found. Run: python ask_db.py list")
        conn.close()
        return

    question = " ".join(args.question).lower()
    words    = question.split()
    print(f"\nTable   : {args.table}")
    print(f"Question: {question}\n")

    cur = conn.cursor()

    # --- DATE FILTER ---
    months = {"january":"01","february":"02","march":"03","april":"04",
              "may":"05","june":"06","july":"07","august":"08",
              "september":"09","october":"10","november":"11","december":"12"}
    month_num = next((num for m, num in months.items() if m in question), None)
    year      = next((w for w in words if w.isdigit() and len(w) == 4), None)

    if month_num or year:
        date_cols = [c for c in columns if any(k in c.lower() for k in ["date", "eta", "etd", "ata"])]
        target_col = next((c for c in date_cols if "delivery" in c.lower()), date_cols[0] if date_cols else None)
        if target_col:
            like_pattern = f"%{year or ''}%{month_num or ''}%" if year and month_num else f"%{year or month_num}%"
            sql = f"SELECT COUNT(*) FROM {args.table} WHERE {target_col} LIKE ?"
            cur.execute(sql, (like_pattern,))
            count = cur.fetchone()[0]
            print(f"  SQL: {sql}  (param: '{like_pattern}')")
            print(f"  Result: {count} row(s) match {target_col} filter")
            conn.close()
            return

    # --- COUNT ---
    if any(w in question for w in ["how many", "count"]):
        sql = f"SELECT COUNT(*) FROM {args.table}"
        cur.execute(sql)
        print(f"  SQL: {sql}")
        print(f"  Result: {cur.fetchone()[0]} rows")
        conn.close()
        return

    # --- SUM ---
    if any(w in question for w in ["total", "sum", "how much"]):
        num_cols = get_numeric_columns(conn, args.table)
        matched  = find_matching_columns(num_cols, words)
        for col in matched:
            sql = f"SELECT SUM({col}) FROM {args.table}"
            cur.execute(sql)
            print(f"  SQL: {sql}")
            print(f"  Total '{col}': {cur.fetchone()[0]:,.2f}")
        if not matched:
            print("  No matching numeric column. Available numeric columns:")
            for c in num_cols[:10]:
                print(f"    - {c}")
        conn.close()
        return

    # --- AVERAGE ---
    if any(w in question for w in ["average", "avg", "mean"]):
        num_cols = get_numeric_columns(conn, args.table)
        matched  = find_matching_columns(num_cols, words)
        for col in matched:
            sql = f"SELECT AVG({col}) FROM {args.table}"
            cur.execute(sql)
            print(f"  SQL: {sql}")
            print(f"  Average '{col}': {cur.fetchone()[0]:,.2f}")
        conn.close()
        return

    # --- MAX ---
    if any(w in question for w in ["max", "highest", "most", "largest"]):
        num_cols = get_numeric_columns(conn, args.table)
        matched  = find_matching_columns(num_cols, words)
        for col in matched:
            sql = f"SELECT * FROM {args.table} ORDER BY {col} DESC LIMIT 1"
            cur.execute(sql)
            row = cur.fetchone()
            print(f"  SQL: {sql}")
            print(f"  Max '{col}' row: {dict(zip(columns, row))}")
        conn.close()
        return

    # --- MIN ---
    if any(w in question for w in ["min", "lowest", "least", "smallest"]):
        num_cols = get_numeric_columns(conn, args.table)
        matched  = find_matching_columns(num_cols, words)
        for col in matched:
            sql = f"SELECT * FROM {args.table} ORDER BY {col} ASC LIMIT 1"
            cur.execute(sql)
            row = cur.fetchone()
            print(f"  SQL: {sql}")
            print(f"  Min '{col}' row: {dict(zip(columns, row))}")
        conn.close()
        return

    # --- LOOKUP (container/document number etc.) ---
    for word in words:
        if len(word) > 6 and word.replace(".", "").isalnum():
            for col in columns:
                sql = f"SELECT * FROM {args.table} WHERE UPPER({col}) LIKE ?"
                cur.execute(sql, (f"%{word.upper()}%",))
                rows = cur.fetchall()
                if rows:
                    print(f"  SQL: {sql}  (param: '%{word.upper()}%')")
                    print(f"  Found {len(rows)} row(s) in column '{col}':")
                    for r in rows[:5]:
                        print(f"    {dict(zip(columns, r))}")
                    conn.close()
                    return

    # --- SHOW / LIST rows ---
    if any(w in question for w in ["show", "list", "display", "first"]):
        n = next((int(w) for w in words if w.isdigit()), 10)
        sql = f"SELECT * FROM {args.table} LIMIT {n}"
        cur.execute(sql)
        rows = cur.fetchall()
        print(f"  SQL: {sql}")
        for r in rows:
            print(f"    {dict(zip(columns, r))}")
        conn.close()
        return

    # --- SHOW COLUMNS ---
    if any(w in question for w in ["columns", "fields"]):
        print(f"  {len(columns)} columns:")
        for c in columns:
            print(f"    - {c}")
        conn.close()
        return

    # --- DEFAULT ---
    print("  Couldn't match your question to an operation.")
    print("  Try: counts, totals, averages, max/min, a lookup value, or 'show rows'.")
    print(f"\n  Available columns:")
    for c in columns[:20]:
        print(f"    - {c}")
    conn.close()


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Structured data engine — SQLite backed")
    sub    = parser.add_subparsers(dest="cmd")

    p_reg = sub.add_parser("register", help="Load an Excel/CSV file into a SQL table")
    p_reg.add_argument("table", help="Table name to create, e.g. 'shipments'")
    p_reg.add_argument("filepath", help="Path to the Excel/CSV file")

    sub.add_parser("list", help="Show all tables")

    p_schema = sub.add_parser("schema", help="Show a table's columns and types")
    p_schema.add_argument("table")

    p_q = sub.add_parser("query", help="Ask a question about a table")
    p_q.add_argument("table")
    p_q.add_argument("question", nargs="+")

    p_sql = sub.add_parser("sql", help="Run raw SQL")
    p_sql.add_argument("query", nargs="+")

    args     = parser.parse_args()
    dispatch = {
        "register": cmd_register,
        "list":     cmd_list,
        "schema":   cmd_schema,
        "query":    cmd_query,
        "sql":      cmd_sql,
    }
    if args.cmd in dispatch:
        dispatch[args.cmd](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
