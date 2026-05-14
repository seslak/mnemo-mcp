"""
Mnemo v0.12.0 consolidation benchmark.

Inserts 50 000 memories (mix of exact duplicates, near-duplicates, and unique),
then measures:
  1. backfill_signatures (all rows start without signatures)   -- target < 60 s
  2. consolidate dry_run (candidate-based, O(n*k) path)        -- target < 10 s

Usage:
    python benchmark_consolidation.py [--rows N]

Exits with code 1 if any target is exceeded.
"""
from __future__ import annotations

import argparse
import os
import random
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server

# Vocabulary for generating realistic-looking but diverse entries.
# 200 common English nouns/verbs split into domain buckets so same-bucket
# entries share vocabulary but cross-bucket entries do not — this mimics real
# memory stores where clusters are local, not global.
_DOMAINS = [
    ["auth", "login", "session", "token", "oauth", "middleware", "credential",
     "permission", "role", "verify", "authenticate", "refresh", "revoke", "scope"],
    ["database", "query", "index", "migration", "schema", "transaction", "cursor",
     "connection", "pool", "replica", "shard", "backup", "restore", "vacuum"],
    ["deploy", "container", "docker", "kubernetes", "helm", "manifest", "rollout",
     "canary", "bluegreen", "namespace", "ingress", "service", "pod", "replica"],
    ["test", "fixture", "mock", "stub", "assertion", "coverage", "regression",
     "integration", "unit", "benchmark", "flaky", "suite", "runner", "report"],
    ["cache", "redis", "ttl", "eviction", "invalidate", "warm", "cold", "hit",
     "miss", "serialise", "deserialise", "prefix", "namespace", "cluster"],
    ["api", "endpoint", "route", "handler", "request", "response", "payload",
     "header", "status", "retry", "timeout", "rate", "limit", "throttle"],
    ["logging", "metric", "trace", "span", "alert", "dashboard", "grafana",
     "prometheus", "loki", "kibana", "event", "stream", "sink", "buffer"],
    ["frontend", "component", "render", "hydrate", "bundle", "webpack", "vite",
     "build", "asset", "chunk", "treeshake", "minify", "sourcemap", "lint"],
]

_UNIQUE_ADJECTIVES = [
    "fast", "slow", "reliable", "experimental", "legacy", "new", "deprecated",
    "optional", "required", "default", "custom", "global", "local", "async",
    "sync", "blocking", "parallel", "sequential", "lazy", "eager",
]


def _gen_text(i: int, rng: random.Random) -> str:
    """Generate a realistic-looking unique memory text."""
    bucket = i % len(_DOMAINS)
    domain_words = _DOMAINS[bucket]
    # Pick 6-10 random words from this domain, plus 1-2 adjectives
    n_domain = rng.randint(6, 10)
    n_adj = rng.randint(1, 2)
    words = rng.sample(domain_words, min(n_domain, len(domain_words)))
    words += rng.sample(_UNIQUE_ADJECTIVES, n_adj)
    rng.shuffle(words)
    return " ".join(words)


def _gen_near_dup(base_text: str, rng: random.Random) -> str:
    """Produce a near-duplicate of base_text by substituting one word."""
    words = base_text.split()
    if len(words) < 4:
        return base_text + " updated"
    idx = rng.randint(0, len(words) - 1)
    substitute = rng.choice(_UNIQUE_ADJECTIVES)
    words[idx] = substitute
    return " ".join(words)


def _insert_rows_without_signatures(sqlite_file: Path, n: int) -> None:
    """Insert rows with no signature columns (simulates pre-backfill state)."""
    rng = random.Random(42)  # deterministic
    conn = sqlite3.connect(str(sqlite_file))
    try:
        BATCH = 2_000
        # Collect near-dup base texts every 200 entries (5% near-dup rate)
        near_dup_bases: dict[int, str] = {}
        exact_dup_texts: dict[int, str] = {}

        for start in range(0, n, BATCH):
            end = min(start + BATCH, n)
            rows = []
            for i in range(start, end):
                base_group = i // 200  # new base text every 200 entries
                if i % 200 == 0:
                    # Base text for this group
                    text = _gen_text(i, rng)
                    near_dup_bases[base_group] = text
                    exact_dup_texts[base_group] = text
                elif i % 200 == 1:
                    # Near-duplicate of the base
                    text = _gen_near_dup(near_dup_bases[base_group], rng)
                elif i % 200 == 2:
                    # Exact duplicate of the base
                    text = exact_dup_texts[base_group]
                else:
                    # Unique entry
                    text = _gen_text(i, rng)
                rows.append((
                    f"bench-{i}", "note", text, "bench", "[]",
                    f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}T00:00:00Z",
                ))
            conn.executemany(
                "INSERT OR IGNORE INTO memories (id, kind, text, source, tags_json, created_at) "
                "VALUES (?,?,?,?,?,?)",
                rows,
            )
            conn.commit()
            print(f"  inserted {end:,}/{n:,} rows...", end="\r")
    finally:
        conn.close()
    print()


def run_benchmark(n: int = 50_000) -> bool:
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        sqlite_file = Path(tmp) / "bench.sqlite"
        os.environ["MNEMO_STORE"] = "sqlite"
        os.environ["MNEMO_SQLITE_FILE"] = str(sqlite_file)
        os.environ["MNEMO_LOG_QUERIES"] = "0"
        os.environ["MNEMO_LOG_EVENTS"] = "0"
        server._SQLITE_BOOTSTRAPPED.clear()

        print(f"\n=== Mnemo v0.12.0 Consolidation Benchmark ({n:,} rows) ===\n")

        # Ensure schema exists
        server.load_store()

        print(f"Inserting {n:,} rows without signatures (~5% near-dup, ~0.5% exact-dup)...")
        t0 = time.monotonic()
        _insert_rows_without_signatures(sqlite_file, n)
        insert_elapsed = time.monotonic() - t0

        conn2 = sqlite3.connect(str(sqlite_file))
        try:
            count = conn2.execute("SELECT COUNT(*) FROM memories WHERE deleted=0").fetchone()[0]
        finally:
            conn2.close()
        print(f"  Insert: {insert_elapsed:.1f}s  Active rows: {count:,}")

        # --- Benchmark 1: backfill_signatures ---
        print(f"\n[1] backfill_signatures (target < 60s)...")
        t1 = time.monotonic()
        result = server.memory_maintenance({"action": "backfill_signatures", "dry_run": False})
        backfill_elapsed = time.monotonic() - t1
        if result.get("isError"):
            print(f"  ERROR: {result}")
            ok = False
        else:
            sc = result["structuredContent"]
            status = "PASS" if backfill_elapsed < 60.0 else f"FAIL (limit 60s)"
            print(f"  Updated: {sc.get('updated_count', '?'):,} rows")
            print(f"  Time:    {backfill_elapsed:.2f}s  {status}")
            if backfill_elapsed >= 60.0:
                ok = False

        # --- Benchmark 2: consolidate dry_run ---
        print(f"\n[2] consolidate dry_run (candidate-based, target < 10s)...")
        t2 = time.monotonic()
        result2 = server.memory_maintenance({"action": "consolidate", "dry_run": True})
        consolidate_elapsed = time.monotonic() - t2
        if result2.get("isError"):
            print(f"  ERROR: {result2}")
            ok = False
        else:
            sc2 = result2["structuredContent"]
            clusters = sc2.get("clusters", [])
            exact = sum(1 for c in clusters if c.get("duplicate_type") == "content_hash")
            near = sum(1 for c in clusters if c.get("duplicate_type") == "near_duplicate")
            status = "PASS" if consolidate_elapsed < 10.0 else f"FAIL (limit 10s)"
            print(f"  Clusters:            {len(clusters):,} ({exact} exact, {near} near-dup)")
            print(f"  Candidates examined: {sc2.get('candidates_examined', '?'):,}")
            print(f"  Similarity calls:    {sc2.get('similarity_calls', '?'):,}")
            print(f"  Time:                {consolidate_elapsed:.2f}s  {status}")
            if consolidate_elapsed >= 10.0:
                ok = False
            if exact < 20:
                print(f"  FAIL -- expected at least 20 exact duplicate clusters, found {exact}")
                ok = False
            if near <= 30:
                print(f"  FAIL -- expected >30 near-duplicate clusters, found {near}")
                ok = False

        # --- Benchmark 3: consolidate_full confirmation gate ---
        print(f"\n[3] consolidate_full confirmation gate...")
        result3 = server.memory_maintenance({"action": "consolidate_full"})
        if result3.get("isError"):
            sc3 = result3["structuredContent"]
            estimated = sc3.get("estimated_pair_count", 0)
            print(f"  Gate enforced  (estimated {estimated:,} pairs for n={count:,})  PASS")
        else:
            print("  FAIL -- consolidate_full ran without confirm_full_scan=true")
            ok = False

    print(f"\n{'=== ALL BENCHMARKS PASS ===' if ok else '=== BENCHMARK FAILED ==='}\n")
    return ok


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=50_000)
    args = parser.parse_args()
    passed = run_benchmark(args.rows)
    sys.exit(0 if passed else 1)
