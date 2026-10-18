# throwaway
"""一次性連通性檢查：ChromaDB / Elasticsearch / Neo4j / Redis / PostgreSQL / Open WebUI。

依評估表「探索性 Spike」登記範圍執行，僅驗證六項服務能否連線，
不涉及任何 RAG 功能邏輯，執行後不需保留為正式程式。
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()

results: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> None:
    try:
        detail = fn()
        results.append((name, True, detail))
    except Exception as exc:
        results.append((name, False, f"{type(exc).__name__}: {exc}"))


def check_postgres() -> str:
    import psycopg2

    conn = psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ.get("POSTGRES_DB", "postgres"),
        connect_timeout=5,
    )
    with conn.cursor() as cur:
        cur.execute("SELECT version();")
        version = cur.fetchone()[0]
    conn.close()
    return version


def check_redis() -> str:
    import redis

    client = redis.Redis(host="localhost", port=6379, socket_connect_timeout=5)
    return f"PING -> {client.ping()}"


def check_chromadb() -> str:
    import chromadb

    client = chromadb.PersistentClient(path="./data/chromadb_smoketest")
    collection = client.get_or_create_collection("smoketest")
    collection.upsert(ids=["1"], documents=["長照 smoke test"], metadatas=[{"source": "smoketest"}])
    result = collection.query(query_texts=["長照"], n_results=1)
    return f"query result ids={result['ids']}"


def check_elasticsearch() -> str:
    import httpx

    url = os.environ.get("ELASTIC_URL", "https://localhost:9200")
    auth = (os.environ.get("ELASTIC_USER", "elastic"), os.environ["ELASTIC_PASSWORD"])
    response = httpx.get(f"{url}/_cluster/health", timeout=5.0, verify=False, auth=auth)
    response.raise_for_status()
    return response.json()["status"]


def check_neo4j() -> str:
    from neo4j import GraphDatabase

    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ["NEO4J_PASSWORD"]
    driver = GraphDatabase.driver(uri, auth=(user, password))
    with driver.session() as session:
        record = session.run("RETURN 1 AS ok").single()
    driver.close()
    return f"RETURN 1 -> {record['ok']}"


def check_open_webui() -> str:
    import httpx

    response = httpx.get("http://localhost:8080/health", timeout=5.0)
    return f"status_code={response.status_code}"


def main() -> int:
    check("PostgreSQL", check_postgres)
    check("Redis", check_redis)
    check("ChromaDB", check_chromadb)
    check("Elasticsearch", check_elasticsearch)
    check("Neo4j", check_neo4j)
    check("Open WebUI", check_open_webui)

    failed = False
    for name, ok, detail in results:
        mark = "OK" if ok else "FAIL"
        print(f"[{mark}] {name}: {detail}")
        failed = failed or not ok

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
