#!/usr/bin/env python3
"""
generate_dataset.py - Python 3.13
Generates repeatable synthetic financial CSV datasets with known accepted and quarantined totals.
Target schema:
  - event_id: string (UUIDv4)
  - category: string enum (PAYMENT, REFUND, TRANSFER, FEE, SUBSCRIPTION)
  - timestamp_utc: string ISO 8601 UTC (YYYY-MM-DDTHH:MM:SSZ)
  - amount_minor: integer (e.g., cents, positive integer)
"""

import argparse
import csv
import hashlib
import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

CATEGORIES = ["PAYMENT", "REFUND", "TRANSFER", "FEE", "SUBSCRIPTION"]

def parse_args():
    parser = argparse.ArgumentParser(description="Generate synthetic financial transaction CSV with known totals.")
    parser.add_argument("--rows", type=int, default=100000, help="Total number of rows to generate.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility.")
    parser.add_argument("--malformed-rate", type=float, default=0.015, help="Ratio of intentionally malformed rows (default 1.5%).")
    parser.add_argument("--output-csv", type=str, default="data/benchmark_100k.csv", help="Output CSV path.")
    parser.add_argument("--output-metrics", type=str, default="data/benchmark_100k_metrics.json", help="Expected reference metrics JSON path.")
    return parser.parse_args()

def generate_valid_row(rng: random.Random, base_time: datetime, index: int):
    event_id = str(uuid.UUID(int=rng.getrandbits(128), version=4))
    category = rng.choice(CATEGORIES)
    seconds_offset = rng.randint(0, 30 * 86400)
    row_time = base_time + timedelta(seconds=seconds_offset)
    timestamp_utc = row_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    amount_minor = rng.randint(100, 500000)
    return {
        "event_id": event_id,
        "category": category,
        "timestamp_utc": timestamp_utc,
        "amount_minor": str(amount_minor)
    }, int(amount_minor), category

def generate_malformed_row(rng: random.Random, base_time: datetime, valid_sample: dict, seen_ids: list):
    error_type = rng.choice([
        "missing_event_id",
        "duplicate_event_id",
        "invalid_category",
        "bad_timestamp",
        "float_amount",
        "non_numeric_amount",
        "empty_amount"
    ])
    
    event_id = str(uuid.UUID(int=rng.getrandbits(128), version=4))
    category = rng.choice(CATEGORIES)
    timestamp_utc = base_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    amount_minor = str(rng.randint(100, 50000))

    if error_type == "missing_event_id":
        event_id = ""
    elif error_type == "duplicate_event_id":
        event_id = rng.choice(seen_ids) if seen_ids else "duplicate-fixed-id-0000"
    elif error_type == "invalid_category":
        category = rng.choice(["UNKNOWN_CAT", "INVALID", "NULL", ""])
    elif error_type == "bad_timestamp":
        timestamp_utc = rng.choice([
            "2026-02-31T12:00:00Z",
            "15/03/2026 12:00:00",
            "invalid-timestamp-string",
            "2026-01-01 00:00:00"
        ])
    elif error_type == "float_amount":
        amount_minor = f"{rng.randint(10, 5000)}.99"
    elif error_type == "non_numeric_amount":
        amount_minor = f"{rng.randint(10, 5000)}USD"
    elif error_type == "empty_amount":
        amount_minor = ""

    return {
        "event_id": event_id,
        "category": category,
        "timestamp_utc": timestamp_utc,
        "amount_minor": amount_minor
    }, error_type

def main():
    args = parse_args()
    rng = random.Random(args.seed)
    base_time = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    out_csv = Path(args.output_csv)
    out_metrics = Path(args.output_metrics)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    out_metrics.parent.mkdir(parents=True, exist_ok=True)

    hasher = hashlib.sha256()

    total_rows = args.rows
    malformed_count_target = int(total_rows * args.malformed_rate)
    malformed_indices = set(rng.sample(range(total_rows), malformed_count_target))

    seen_valid_ids = []
    accepted_count = 0
    quarantined_count = 0
    category_counts = {cat: 0 for cat in CATEGORIES}
    category_sums = {cat: 0 for cat in CATEGORIES}
    grand_total_amount_minor = 0
    quarantine_breakdown = {}

    with open(out_csv, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["event_id", "category", "timestamp_utc", "amount_minor"])
        writer.writeheader()

        for idx in range(total_rows):
            if idx in malformed_indices:
                row_dict, err_type = generate_malformed_row(rng, base_time, None, seen_valid_ids[-100:] if seen_valid_ids else [])
                quarantined_count += 1
                quarantine_breakdown[err_type] = quarantine_breakdown.get(err_type, 0) + 1
            else:
                row_dict, amount_val, cat = generate_valid_row(rng, base_time, idx)
                accepted_count += 1
                seen_valid_ids.append(row_dict["event_id"])
                category_counts[cat] += 1
                category_sums[cat] += amount_val
                grand_total_amount_minor += amount_val

            writer.writerow(row_dict)

    # Calculate SHA-256 of the generated file
    with open(out_csv, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    file_sha256 = hasher.hexdigest()

    metrics = {
        "metadata": {
            "dataset_file": out_csv.name,
            "seed": args.seed,
            "total_rows_generated": total_rows,
            "source_sha256": file_sha256,
            "schema_version": "1.0.0"
        },
        "validation_summary": {
            "total_rows": total_rows,
            "accepted_rows": accepted_count,
            "quarantined_rows": quarantined_count,
            "quarantine_reasons": quarantine_breakdown
        },
        "aggregate_results": {
            "grand_total_amount_minor": grand_total_amount_minor,
            "categories": {
                cat: {
                    "count": category_counts[cat],
                    "total_amount_minor": category_sums[cat]
                }
                for cat in CATEGORIES
            }
        }
    }

    with open(out_metrics, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)

    print(f"[OK] Generated {total_rows:,} rows to {out_csv}")
    print(f"     Accepted: {accepted_count:,} | Quarantined: {quarantined_count:,}")
    print(f"     Grand Total Amount (minor): {grand_total_amount_minor:,}")
    print(f"     SHA-256: {file_sha256}")
    print(f"[OK] Saved reference metrics to {out_metrics}")

if __name__ == "__main__":
    main()
