import argparse
import os
from datetime import datetime
from typing import Any, Dict, List

from dotenv import load_dotenv

from db import get_connection, mysql_enabled


def main() -> None:
    load_dotenv()
    args = parse_args()

    if not mysql_enabled():
        raise SystemExit("MYSQL_ENABLED must be true in .env before generating prompt rules.")

    rules = fetch_rule_summary(args.audit_run_id, args.min_affected_rows, args.limit)
    examples = fetch_examples(args.audit_run_id, args.examples_per_rule)

    if not rules:
        raise SystemExit(f"No prompt tuning rules found for audit_run_id={args.audit_run_id}.")

    content = build_rules_file(args.audit_run_id, rules, examples)

    if args.dry_run:
        print(content)
        return

    with open(args.output, "w", encoding="utf-8") as output_file:
        output_file.write(content)

    print(f"Wrote {len(rules)} prompt rule(s) to {args.output}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate extra trade-code prompt rules from rate audit results.")
    parser.add_argument("--audit-run-id", required=True, help="Audit run id from ai_bq_trade_code_rate_audits.")
    parser.add_argument("--output", default="prompt_tuning_rules.txt", help="Output file loaded by trade_code_worker.py.")
    parser.add_argument("--min-affected-rows", type=int, default=2, help="Only include repeated prompt rules.")
    parser.add_argument("--limit", type=int, default=30, help="Maximum prompt rules to write.")
    parser.add_argument("--examples-per-rule", type=int, default=3, help="Examples included as comments for review.")
    parser.add_argument("--dry-run", action="store_true", help="Print generated rules without writing file.")
    return parser.parse_args()


def fetch_rule_summary(audit_run_id: str, min_affected_rows: int, limit: int) -> List[Dict[str, Any]]:
    sql = """
        SELECT
            prompt_rule_suggestion,
            audit_status,
            COUNT(*) AS affected_rows,
            COUNT(DISTINCT current_trade_code) AS current_code_count,
            COUNT(DISTINCT inferred_trade_code) AS inferred_code_count
        FROM max_purchasing.ai_bq_trade_code_rate_audits
        WHERE audit_run_id = %s
          AND prompt_rule_suggestion IS NOT NULL
          AND prompt_rule_suggestion <> ''
        GROUP BY prompt_rule_suggestion, audit_status
        HAVING COUNT(*) >= %s
        ORDER BY
            CASE audit_status
                WHEN 'likely_wrong_trade_code' THEN 1
                WHEN 'possible_trade_code_split' THEN 2
                ELSE 3
            END,
            affected_rows DESC,
            prompt_rule_suggestion
        LIMIT %s
    """
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, (audit_run_id, min_affected_rows, limit))
            return cursor.fetchall()


def fetch_examples(audit_run_id: str, examples_per_rule: int) -> Dict[str, List[Dict[str, Any]]]:
    sql = """
        SELECT
            prompt_rule_suggestion,
            current_trade_code,
            inferred_trade_code,
            uom_code,
            unit_rate,
            common_rate,
            rate_ratio_to_common,
            contract_no,
            item_ref_no,
            LEFT(full_bq_description, 500) AS description_sample
        FROM max_purchasing.ai_bq_trade_code_rate_audits
        WHERE audit_run_id = %s
          AND prompt_rule_suggestion IS NOT NULL
          AND prompt_rule_suggestion <> ''
        ORDER BY prompt_rule_suggestion, rate_ratio_to_common DESC
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, (audit_run_id,))
            for row in cursor.fetchall():
                rule = row["prompt_rule_suggestion"]
                grouped.setdefault(rule, [])
                if len(grouped[rule]) < examples_per_rule:
                    grouped[rule].append(row)
    return grouped


def build_rules_file(audit_run_id: str, rules: List[Dict[str, Any]], examples: Dict[str, List[Dict[str, Any]]]) -> str:
    lines = [
        f"# Generated from rate audit: {audit_run_id}",
        f"# Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "# Review these rules before using them for a full rerun.",
        "",
        "Apply these extra rules strictly:",
    ]

    for index, rule in enumerate(rules, start=1):
        suggestion = rule["prompt_rule_suggestion"].strip()
        lines.append(f"{index}. {suggestion}")
        lines.append(
            f"   # audit_status={rule['audit_status']} affected_rows={rule['affected_rows']} "
            f"current_code_count={rule['current_code_count']} inferred_code_count={rule['inferred_code_count']}"
        )
        for example in examples.get(rule["prompt_rule_suggestion"], []):
            lines.append(
                "   # example: "
                f"current={example.get('current_trade_code')} inferred={example.get('inferred_trade_code')} "
                f"uom={example.get('uom_code')} rate={example.get('unit_rate')} common={example.get('common_rate')} "
                f"contract={example.get('contract_no')} item_ref={example.get('item_ref_no')}"
            )
            sample = str(example.get("description_sample") or "").replace("\n", " ").strip()
            if sample:
                lines.append(f"   # text: {sample}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


if __name__ == "__main__":
    main()
