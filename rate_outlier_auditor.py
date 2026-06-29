import argparse
import os
import re
import statistics
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Tuple

from dotenv import load_dotenv

from db import get_connection, mysql_enabled
from trade_code_worker import (
    apply_canonical_overrides,
    clean_text,
    safe_confidence,
)


def main() -> None:
    started_at = time.perf_counter()
    load_dotenv()
    args = parse_args()

    if not mysql_enabled():
        raise SystemExit("MYSQL_ENABLED must be true in .env before auditing rates.")

    audit_run_id = args.audit_run_id or f"rate-audit-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"

    log(
        "audit_start",
        source=args.source,
        ai_run_id=args.ai_run_id,
        audit_run_id=audit_run_id,
        min_group_count=args.min_group_count,
        ratio_threshold=args.ratio_threshold,
    )

    if not args.dry_run:
        ensure_audit_table()

    rows = fetch_rows(args.source, args.ai_run_id)
    log("rows_loaded", rows=len(rows))

    groups = group_rows(rows)
    audit_rows: List[Dict[str, Any]] = []

    for (trade_code, uom_norm), group in groups.items():
        if len(group) < args.min_group_count:
            continue

        common = common_rate(group, args.rate_precision)
        if not common:
            continue

        common_rate_value, common_rate_count = common
        if common_rate_count < args.min_common_count:
            continue

        for row in group:
            ratio = rate_ratio(row["unit_rate"], common_rate_value)
            if ratio < args.ratio_threshold:
                continue

            audit_rows.append(
                build_audit_row(
                    row=row,
                    group=group,
                    uom_norm=uom_norm,
                    common_rate_value=common_rate_value,
                    common_rate_count=common_rate_count,
                    ratio=ratio,
                    audit_run_id=audit_run_id,
                    rate_precision=args.rate_precision,
                )
            )

    audit_rows.sort(key=lambda item: (item["rate_ratio_to_common"] or 0, item["source_suggestion_id"]), reverse=True)
    if args.limit:
        audit_rows = audit_rows[: args.limit]

    for audit_row in audit_rows:
        log(
            "outlier",
            source_id=audit_row["source_suggestion_id"],
            current=audit_row["current_trade_code"],
            inferred=audit_row["inferred_trade_code"],
            unit_rate=audit_row["unit_rate"],
            common_rate=audit_row["common_rate"],
            ratio=audit_row["rate_ratio_to_common"],
            status=audit_row["audit_status"],
        )
        if not args.dry_run:
            insert_audit_row(audit_row)

    log("audit_done", outliers=len(audit_rows), dry_run=args.dry_run, elapsed_seconds=elapsed(started_at))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit trade-code unit-rate outliers.")
    parser.add_argument(
        "--source",
        choices=["latest", "history"],
        default="latest",
        help="Use latest columns from ai_bq_trade_code_suggestions or a result history run.",
    )
    parser.add_argument("--ai-run-id", help="Required when --source history.")
    parser.add_argument("--audit-run-id", help="Audit run id stored in ai_bq_trade_code_rate_audits.")
    parser.add_argument("--min-group-count", type=int, default=5, help="Minimum rows in same trade code/UOM group.")
    parser.add_argument("--min-common-count", type=int, default=2, help="Minimum rows sharing the common rounded rate.")
    parser.add_argument("--ratio-threshold", type=float, default=2.0, help="Flag rows this far from common rate.")
    parser.add_argument("--rate-precision", type=int, default=2, help="Decimal places used for common-rate bucket.")
    parser.add_argument("--limit", type=int, help="Limit number of outlier rows written.")
    parser.add_argument("--dry-run", action="store_true", help="Print outliers without writing DB table.")
    return parser.parse_args()


def fetch_rows(source: str, ai_run_id: str | None) -> List[Dict[str, Any]]:
    if source == "history" and not ai_run_id:
        raise SystemExit("--ai-run-id is required when --source history.")

    if source == "latest":
        sql = """
            SELECT
                s.id AS source_suggestion_id,
                s.letter_award_id,
                s.contract_no,
                s.business_unit,
                s.project_name,
                s.project_shortname,
                s.awarded_date,
                s.contractor_name,
                s.letter_award_tab_id,
                s.tab_name,
                s.bq_item_id,
                s.item_ref_no,
                s.bq_item_no,
                s.parent_header_3,
                s.parent_header_2,
                s.parent_header_1,
                s.bq_item_description,
                s.full_bq_description,
                s.ai_matching_text,
                s.uom_code,
                s.quantity,
                s.unit_rate,
                s.tender_amount,
                s.suggested_trade_code AS current_trade_code,
                s.suggested_trade_name AS current_trade_name,
                s.spec_key AS current_spec_key,
                s.prompt_version,
                s.ai_model,
                s.ai_run_id
            FROM max_purchasing.ai_bq_trade_code_suggestions s
            WHERE s.review_status = 'ai_suggested'
              AND s.suggested_trade_code IS NOT NULL
              AND s.suggested_trade_code <> ''
              AND s.unit_rate IS NOT NULL
              AND s.unit_rate > 0
        """
        params: Tuple[Any, ...] = ()
    else:
        sql = """
            SELECT
                s.id AS source_suggestion_id,
                s.letter_award_id,
                s.contract_no,
                s.business_unit,
                s.project_name,
                s.project_shortname,
                s.awarded_date,
                s.contractor_name,
                s.letter_award_tab_id,
                s.tab_name,
                s.bq_item_id,
                s.item_ref_no,
                s.bq_item_no,
                s.parent_header_3,
                s.parent_header_2,
                s.parent_header_1,
                s.bq_item_description,
                s.full_bq_description,
                s.ai_matching_text,
                s.uom_code,
                s.quantity,
                s.unit_rate,
                s.tender_amount,
                r.suggested_trade_code AS current_trade_code,
                r.suggested_trade_name AS current_trade_name,
                r.spec_key AS current_spec_key,
                r.prompt_version,
                r.ai_model,
                r.ai_run_id
            FROM max_purchasing.ai_bq_trade_code_suggestion_results r
            JOIN max_purchasing.ai_bq_trade_code_suggestions s
                ON s.id = r.source_suggestion_id
            WHERE r.ai_run_id = %s
              AND r.suggested_trade_code IS NOT NULL
              AND r.suggested_trade_code <> ''
              AND s.unit_rate IS NOT NULL
              AND s.unit_rate > 0
        """
        params = (ai_run_id,)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()


def group_rows(rows: Iterable[Dict[str, Any]]) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["current_trade_code"], normalize_uom(row.get("uom_code")))].append(row)
    return groups


def common_rate(group: List[Dict[str, Any]], precision: int) -> Tuple[float, int] | None:
    buckets = Counter(round(float(row["unit_rate"]), precision) for row in group if row.get("unit_rate") is not None)
    if not buckets:
        return None
    return buckets.most_common(1)[0]


def build_audit_row(
    row: Dict[str, Any],
    group: List[Dict[str, Any]],
    uom_norm: str,
    common_rate_value: float,
    common_rate_count: int,
    ratio: float,
    audit_run_id: str,
    rate_precision: int,
) -> Dict[str, Any]:
    inferred = infer_trade_code(row)
    current_code = row["current_trade_code"]
    status, explanation, prompt_rule = explain_outlier(row, current_code, inferred, common_rate_value, ratio)
    rates = [float(item["unit_rate"]) for item in group]

    return {
        "audit_run_id": audit_run_id,
        "source_suggestion_id": row["source_suggestion_id"],
        "letter_award_id": row.get("letter_award_id"),
        "contract_no": row.get("contract_no"),
        "item_ref_no": row.get("item_ref_no"),
        "bq_item_id": row.get("bq_item_id"),
        "contractor_name": row.get("contractor_name"),
        "business_unit": row.get("business_unit"),
        "project_name": row.get("project_name"),
        "awarded_date": row.get("awarded_date"),
        "tab_name": row.get("tab_name"),
        "current_trade_code": current_code,
        "inferred_trade_code": inferred.get("suggested_trade_code"),
        "uom_code": row.get("uom_code"),
        "uom_norm": uom_norm,
        "quantity": row.get("quantity"),
        "unit_rate": row.get("unit_rate"),
        "common_rate": Decimal(str(common_rate_value)),
        "common_rate_count": common_rate_count,
        "group_count": len(group),
        "group_min_rate": Decimal(str(min(rates))),
        "group_avg_rate": Decimal(str(statistics.mean(rates))),
        "group_max_rate": Decimal(str(max(rates))),
        "rate_ratio_to_common": Decimal(str(round(ratio, 4))),
        "audit_status": status,
        "audit_explanation": explanation,
        "prompt_rule_suggestion": prompt_rule,
        "full_bq_description": row.get("full_bq_description"),
        "parent_header_3": row.get("parent_header_3"),
        "parent_header_2": row.get("parent_header_2"),
        "parent_header_1": row.get("parent_header_1"),
        "bq_item_description": row.get("bq_item_description"),
        "source_prompt_version": row.get("prompt_version"),
        "source_ai_model": row.get("ai_model"),
        "source_ai_run_id": row.get("ai_run_id"),
    }


def infer_trade_code(row: Dict[str, Any]) -> Dict[str, Any]:
    seed = {
        "id": row["source_suggestion_id"],
        "suggested_trade_code": row["current_trade_code"],
        "suggested_trade_name": row.get("current_trade_name"),
        "spec_key": row.get("current_spec_key"),
        "confidence": 0.5,
        "reasoning": "Seeded from current trade code before deterministic audit override.",
    }
    return apply_canonical_overrides(row, seed)


def explain_outlier(
    row: Dict[str, Any],
    current_code: str,
    inferred: Dict[str, Any],
    common_rate: float,
    ratio: float,
) -> Tuple[str, str, str | None]:
    inferred_code = inferred.get("suggested_trade_code")
    text = searchable_text(row)
    uom_norm = normalize_uom(row.get("uom_code"))

    if inferred_code and inferred_code != current_code:
        return (
            "likely_wrong_trade_code",
            f"Description indicates {inferred_code}, but current code is {current_code}. Rate is {ratio:.2f}x away from common rate {common_rate}.",
            prompt_rule_for(inferred_code, current_code, text),
        )

    if ("PDA" in current_code or "LOAD" in current_code) and uom_norm in {"M", "METER", "METRE"}:
        return (
            "likely_wrong_trade_code",
            f"Test code has length UOM {row.get('uom_code')}; likely supply/drive/joint text was missed. Rate is {ratio:.2f}x from common.",
            "Do not classify length-based m items as PDA/LOAD unless the item itself says load test, PDA, or Pile Driving Analysis.",
        )

    if "LOAD" in current_code and "pda" in text:
        return (
            "likely_wrong_trade_code",
            "Current code is LOAD but description mentions PDA/Pile Driving Analysis.",
            "PDA/Pile Driving Analysis must classify as PDA even under a generic load-test heading.",
        )

    if "CUT" in current_code and "ground level" in text and "obstruction" in text and "CUT-OBSTRUCTION" not in current_code:
        return (
            "possible_trade_code_split",
            "Cut item mentions ground level due to obstruction; this is a narrower scope than normal pile-head cut.",
            "Cut to ground level due to obstruction should use CUT-OBSTRUCTION.",
        )

    context = []
    if row.get("contractor_name"):
        context.append(f"contractor={row['contractor_name']}")
    if row.get("awarded_date"):
        context.append(f"awarded_date={row['awarded_date']}")
    if row.get("quantity"):
        context.append(f"quantity={row['quantity']}")

    return (
        "possible_real_rate_outlier",
        f"Description still matches {current_code}. Rate is {ratio:.2f}x away from common rate {common_rate}; investigate commercial context ({', '.join(context)}).",
        None,
    )


def prompt_rule_for(inferred_code: str, current_code: str, text: str) -> str:
    if "SUPPLY" in inferred_code:
        return "Supply and deliver wording must classify as SUPPLY even when the text also contains working load or tonnes."
    if "DRIVE" in inferred_code:
        return "Handle/transport/pitch/drive wording must classify as DRIVE even when the text also contains working load or tonnes."
    if "JOINT" in inferred_code:
        return "Splice/weld/sleeve/pile joint wording must classify as JOINT."
    if "PDA" in inferred_code:
        return "PDA/Pile Driving Analysis wording must classify as PDA, not LOAD."
    if "CUT-OBSTRUCTION" in inferred_code:
        return "Cut to ground level due to obstruction must classify as CUT-OBSTRUCTION."
    if "CUT" in inferred_code:
        return "Cut-off/cutting pile head wording must classify as CUT."
    if "LOAD" in inferred_code:
        return "Only load-test/loading-arrangement wording should classify as LOAD; working load alone is capacity, not activity."
    return f"Prefer deterministic classification {inferred_code} over {current_code} when source text matches."


def ensure_audit_table() -> None:
    sql = """
        CREATE TABLE IF NOT EXISTS max_purchasing.ai_bq_trade_code_rate_audits (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            audit_run_id VARCHAR(100) NOT NULL,
            source_suggestion_id BIGINT NOT NULL,
            letter_award_id BIGINT NULL,
            contract_no VARCHAR(255) NULL,
            item_ref_no BIGINT NULL,
            bq_item_id BIGINT NULL,
            contractor_name VARCHAR(500) NULL,
            business_unit VARCHAR(255) NULL,
            project_name VARCHAR(500) NULL,
            awarded_date DATETIME NULL,
            tab_name VARCHAR(255) NULL,
            current_trade_code VARCHAR(100) NULL,
            inferred_trade_code VARCHAR(100) NULL,
            uom_code VARCHAR(100) NULL,
            uom_norm VARCHAR(100) NULL,
            quantity DECIMAL(20, 4) NULL,
            unit_rate DECIMAL(20, 4) NULL,
            common_rate DECIMAL(20, 4) NULL,
            common_rate_count INT NULL,
            group_count INT NULL,
            group_min_rate DECIMAL(20, 4) NULL,
            group_avg_rate DECIMAL(20, 4) NULL,
            group_max_rate DECIMAL(20, 4) NULL,
            rate_ratio_to_common DECIMAL(20, 4) NULL,
            audit_status VARCHAR(100) NULL,
            audit_explanation TEXT NULL,
            prompt_rule_suggestion TEXT NULL,
            full_bq_description TEXT NULL,
            parent_header_3 TEXT NULL,
            parent_header_2 TEXT NULL,
            parent_header_1 TEXT NULL,
            bq_item_description TEXT NULL,
            source_prompt_version VARCHAR(50) NULL,
            source_ai_model VARCHAR(100) NULL,
            source_ai_run_id VARCHAR(100) NULL,
            created_date DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_rate_audit_run_source (audit_run_id, source_suggestion_id),
            KEY idx_rate_audit_status (audit_status),
            KEY idx_rate_audit_current_code (current_trade_code),
            KEY idx_rate_audit_inferred_code (inferred_trade_code)
        )
    """
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql)


def insert_audit_row(row: Dict[str, Any]) -> None:
    columns = [
        "audit_run_id",
        "source_suggestion_id",
        "letter_award_id",
        "contract_no",
        "item_ref_no",
        "bq_item_id",
        "contractor_name",
        "business_unit",
        "project_name",
        "awarded_date",
        "tab_name",
        "current_trade_code",
        "inferred_trade_code",
        "uom_code",
        "uom_norm",
        "quantity",
        "unit_rate",
        "common_rate",
        "common_rate_count",
        "group_count",
        "group_min_rate",
        "group_avg_rate",
        "group_max_rate",
        "rate_ratio_to_common",
        "audit_status",
        "audit_explanation",
        "prompt_rule_suggestion",
        "full_bq_description",
        "parent_header_3",
        "parent_header_2",
        "parent_header_1",
        "bq_item_description",
        "source_prompt_version",
        "source_ai_model",
        "source_ai_run_id",
    ]
    placeholders = ", ".join(["%s"] * len(columns))
    update_clause = ", ".join(f"{column} = VALUES({column})" for column in columns[2:])
    sql = f"""
        INSERT INTO max_purchasing.ai_bq_trade_code_rate_audits (
            {", ".join(columns)}
        ) VALUES ({placeholders})
        ON DUPLICATE KEY UPDATE {update_clause}
    """
    params = tuple(clean_db_value(row.get(column)) for column in columns)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)


def normalize_uom(value: Any) -> str:
    text = str(value or "").strip().upper().replace(".", "")
    aliases = {
        "NO": "NO",
        "NOS": "NO",
        "NO ": "NO",
        "UNIT": "UNIT",
        "UNITS": "UNIT",
        "L S": "LS",
        "LS": "LS",
        "LUMP SUM": "LS",
        "M": "M",
        "METER": "M",
        "METRE": "M",
    }
    return aliases.get(text, text or "UNKNOWN")


def rate_ratio(rate: Any, common_rate_value: float) -> float:
    value = float(rate)
    if value <= 0 or common_rate_value <= 0:
        return 0
    return max(value / common_rate_value, common_rate_value / value)


def searchable_text(row: Dict[str, Any]) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in [
            "parent_header_3",
            "parent_header_2",
            "parent_header_1",
            "bq_item_description",
            "full_bq_description",
            "ai_matching_text",
        ]
    ).lower()


def clean_db_value(value: Any) -> Any:
    if isinstance(value, str):
        return clean_text(value, 65000)
    return value


def log(event: str, **fields: Any) -> None:
    payload = " ".join(f"{key}={format_log_value(value)}" for key, value in fields.items())
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(f"[{timestamp}] {event}" + (f" {payload}" if payload else ""), flush=True)


def elapsed(started_at: float) -> str:
    return f"{time.perf_counter() - started_at:.3f}"


def format_log_value(value: Any) -> str:
    text = str(value)
    if " " in text:
        return repr(text)
    return text


if __name__ == "__main__":
    main()
