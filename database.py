import duckdb
import os

# Paths to CSV files
BASE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

FLAGS_PATH = os.path.join(BASE_PATH, "flags.csv")
REVIEWERS_PATH = os.path.join(BASE_PATH, "reviewers.csv")
ATTACKS_PATH = os.path.join(BASE_PATH, "attacks.csv")

def get_connection():
    """Create DuckDB connection with views over CSV files"""
    con = duckdb.connect()
    con.execute(f"CREATE VIEW flags AS SELECT * FROM read_csv_auto('{FLAGS_PATH}')")
    con.execute(f"CREATE VIEW reviewers AS SELECT * FROM read_csv_auto('{REVIEWERS_PATH}')")
    con.execute(f"CREATE VIEW attacks AS SELECT * FROM read_csv_auto('{ATTACKS_PATH}')")
    return con

def get_schema():
    """Get exact column names for all tables — used in Claude prompt"""
    con = get_connection()
    schema = {}
    for table in ["flags", "reviewers", "attacks"]:
        result = con.execute(f"DESCRIBE {table}").fetchdf()
        schema[table] = result[["column_name", "column_type"]].values.tolist()
    con.close()
    return schema

def run_query(sql):
    """Run a SQL query and return a dataframe"""
    con = get_connection()
    try:
        df = con.execute(sql).fetchdf()
        return df, None
    except Exception as e:
        return None, str(e)
    finally:
        con.close()

if __name__ == "__main__":
    # Test it works
    schema = get_schema()
    print("=== SCHEMA ===")
    for table, cols in schema.items():
        print(f"\n{table}:")
        for col in cols:
            print(f"  {col[0]} ({col[1]})")

    # Test a hardcoded query
    df, error = run_query("SELECT category, COUNT(*) as total FROM flags GROUP BY category ORDER BY total DESC")
    if error:
        print("Error:", error)
    else:
        print("\n=== TEST QUERY RESULT ===")
        print(df)