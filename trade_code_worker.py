import argparse
import json
import os
import re
import time
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List

from dotenv import load_dotenv

from db import get_connection, mysql_enabled


SYSTEM_PROMPT = """
You are classifying EcoWorld piling contract BQ items into reusable trade codes.

Goal:
- Same work, same pile/spec, same measurement basis, and same UOM should receive the same trade code.
- Different pile size, pile type, work activity, testing method, or UOM should receive a different trade code.
- The trade code will later be used to compare unit rates across contracts.

Rules:
- Use concise uppercase trade codes using this format:
  PIL-{TYPE}-{SIZE}-{ACTIVITY} or PIL-{TYPE}-{SIZE}-{ACTIVITY}-{LOAD}
- TYPE must be one of: GEN, PRC, SPUN, BORED.
- SIZE must be GEN for general items, otherwise use normalized size like 125X125, 150X150, 200X200, 600MM.
- ACTIVITY must be one of: SURVEY, RECORD, EQUIP-MOB, OBSTRUCTION, SUPPLY, DRIVE, CUT, CUT-OBSTRUCTION, JOINT, PDA, LOAD-PRELIM, LOAD-SUBSEQ, LOAD-MAINT, LOAD, PREBORE, REDRIVE, OTHER.
- LOAD is required only for static/preliminary/subsequent/maintained load test items, like 36T, 45T, 50T, 90T.
- Separate supply, driving/installation, static load test, PDA/dynamic test, extension/jointing, cutting, hacking, disposal, attendance, and other distinct work.
- Ignore contract-specific wording unless it changes the physical work, specification, or measurement basis.
- Do not invent rates or quantities.
- Return JSON only. No markdown.
- Preferred top-level shape is a JSON array.
- If the provider requires a JSON object, return { "suggestions": [ ... ] }.

Classification priority:
- If text explicitly says "PDA", "Pile Driving Analysis", or "PDA load test", classify as PDA even when a nearby header says load test.
- If text says "supply and deliver", classify as SUPPLY.
- If text says "uplift from stack", "handle", "transport", "pitch and drive", classify as DRIVE, not SUPPLY.
- If text says "cut-off", "cut pile head", "cutting of pile heads", classify as CUT.
- If text says "cut ... to ground level due to obstruction", classify as CUT-OBSTRUCTION.
- If text says "Preliminary load test", classify as LOAD-PRELIM.
- If text says "Subsequent load test", classify as LOAD-SUBSEQ.
- If text says "Maintained load test" or "static load test", classify as LOAD-MAINT.
- If text says "load test" or "loading arrangement" but the sequence is unclear, classify as LOAD.
- Do not classify as LOAD only because text says "working load", "load bearing pile", or "tonnes"; those are pile capacity/spec words unless the item also says load test.
- If text says "splice", "weld", "sleeve joint", or "pile joints", classify as JOINT.
- Survey / setting out / as-built survey must be PIL-GEN-GEN-SURVEY.
- Piling records / driving records / blow count records must be PIL-GEN-GEN-RECORD.
- Moving, handling, provision, transportation, assembling, removal of piling equipment must be PIL-GEN-GEN-EQUIP-MOB.
- Removal of underground obstructions must be PIL-GEN-GEN-OBSTRUCTION.

Examples:
- PDA test on 125 mm x 125 mm prestressed concrete pile => PIL-PRC-125X125-PDA
- Preliminary load test on 150 mm x 150 mm pile to 50 tons => PIL-PRC-150X150-LOAD-PRELIM-50T
- Subsequent load test on 150 mm x 150 mm pile to 50 tons => PIL-PRC-150X150-LOAD-SUBSEQ-50T
- Maintained load test on 150 mm x 150 mm pile to 50 tons => PIL-PRC-150X150-LOAD-MAINT-50T
- Preliminary load test on 125 mm x 125 mm pile to 36 tons => PIL-PRC-125X125-LOAD-PRELIM-36T
- Ditto for PDA load test on 150 mm x 150 mm pile => PIL-PRC-150X150-PDA
- Cut-off exposed end of 150 mm x 150 mm prestressed concrete pile => PIL-PRC-150X150-CUT
- Cut-off exposed end of 125 mm x 125 mm pile to ground level due to obstruction => PIL-PRC-125X125-CUT-OBSTRUCTION
- Supply and deliver 125 mm x 125 mm prestressed concrete pile => PIL-PRC-125X125-SUPPLY
- Uplift, handle, transport, pitch and drive 125 mm x 125 mm prestressed concrete pile => PIL-PRC-125X125-DRIVE

For each input item, return:
- id: source staging table id
- suggested_trade_code
- suggested_trade_name
- spec_key
- confidence: number from 0 to 1
- reasoning: short reason for the classification

Important:
- Return exactly one result for every input item id.
- Do not skip minor, provisional, lump sum, survey, equipment, record, obstruction, or attendance items.
""".strip()

INFRASTRUCTURE_SYSTEM_PROMPT = """
You are classifying EcoWorld Infrastructure Works contract BQ items into reusable trade codes.

Goal:
- Same work scope, same asset/system, same specification, same measurement basis, and same UOM should receive the same trade code.
- Different infrastructure system, asset type, work activity, material/specification, or UOM should receive a different trade code.
- The trade code will later be used to compare unit rates across contracts.

Rules:
- Use concise uppercase trade codes using this format:
  INF-{SYSTEM}-{ASSET}-{ACTIVITY}
- SYSTEM must be one of: GEN, EW, ROAD, DRAIN, SEWER, WATER, ELEC, TELCO, LIGHT, LAND, STRUCT.
- ASSET must be GEN for general items, otherwise use a normalized asset/spec key like PIPE-150MM, RC-DRAIN-600MM, MANHOLE, ROADBASE, ASPHALT, KERB, CULVERT, TRENCH, CHAMBER, POLE.
- ACTIVITY must be one of: SURVEY, MOB, CLEAR, EXCAVATE, FILL, COMPACT, DISPOSE, SUPPLY, INSTALL, LAY, TEST, CONNECT, CONSTRUCT, REINSTATE, PROTECT, MARKING, OTHER.
- Separate supply, installation/laying, excavation, backfilling, compaction, testing, connection, reinstatement, disposal, protection, and other distinct work.
- Keep road, drainage, sewerage, water reticulation, electrical, telecom, street lighting, earthwork, landscape, and general infrastructure scopes separate.
- Do not invent rates or quantities.
- Return JSON only. No markdown.
- Preferred top-level shape is a JSON array.
- If the provider requires a JSON object, return { "suggestions": [ ... ] }.

Classification priority:
- If text says road base, crusher run, sub-base, premix, asphalt, wearing course, binder course, kerb, road marking, classify as ROAD.
- If text says drain, culvert, sump, catchpit, swale, U-drain, V-drain, RC drain, classify as DRAIN.
- If text says sewer, sewerage, manhole, inspection chamber, septic, classify as SEWER.
- If text says water main, water reticulation, HDPE, MSCL, DI pipe, valve, hydrant, classify as WATER.
- If text says TNB, electrical duct, cable trench, substation external ducting, classify as ELEC.
- If text says telecom, fibre, communication duct, classify as TELCO.
- If text says street lighting, lamp pole, feeder pillar, classify as LIGHT.
- If text says turfing, planting, landscape, hydroseeding, classify as LAND.
- If text says clearing, grubbing, excavation, cut, fill, backfill, compaction, disposal, classify as EW unless a more specific system is clearly stated.
- Use GEN only for general preliminaries, survey, mobilisation, traffic management, temporary works, or items that span multiple systems.

Examples:
- Supply and lay 150mm HDPE water pipe => INF-WATER-PIPE-150MM-LAY
- Pressure test water main => INF-WATER-PIPE-GEN-TEST
- Construct 600mm wide precast RC U-drain => INF-DRAIN-RC-DRAIN-600MM-CONSTRUCT
- Excavate trench for sewer pipe => INF-SEWER-TRENCH-EXCAVATE
- Construct sewer manhole => INF-SEWER-MANHOLE-CONSTRUCT
- Supply and lay asphaltic concrete wearing course => INF-ROAD-ASPHALT-LAY
- Crusher run road base compacted in layers => INF-ROAD-ROADBASE-COMPACT
- Road line marking => INF-ROAD-MARKING-MARKING
- Street lighting pole installation => INF-LIGHT-POLE-INSTALL
- Site clearing and grubbing => INF-EW-GEN-CLEAR

For each input item, return:
- id: source staging table id
- suggested_trade_code
- suggested_trade_name
- spec_key
- confidence: number from 0 to 1
- reasoning: short reason for the classification

Important:
- Return exactly one result for every input item id.
- Do not skip minor, provisional, lump sum, survey, testing, connection, reinstatement, or attendance items.
""".strip()


def active_system_prompt(prompt_version: str = "piling-v1", category_of_work: str | None = None) -> str:
    base_prompt = resolve_system_prompt(prompt_version, category_of_work)
    if is_infrastructure_category(category_of_work) or prompt_version.startswith("infra"):
        return base_prompt

    rules_path = os.getenv("TRADE_CODE_PROMPT_RULES_FILE", "prompt_tuning_rules.txt")
    if not rules_path or not os.path.exists(rules_path):
        return base_prompt

    with open(rules_path, "r", encoding="utf-8") as rules_file:
        extra_rules = rules_file.read().strip()

    if not extra_rules:
        return base_prompt

    return f"{base_prompt}\n\nAdditional audit-derived rules:\n{extra_rules}"


def resolve_system_prompt(prompt_version: str = "piling-v1", category_of_work: str | None = None) -> str:
    if is_infrastructure_category(category_of_work) or prompt_version.startswith("infra"):
        return INFRASTRUCTURE_SYSTEM_PROMPT
    return SYSTEM_PROMPT


def prompt_version_for_category(category_of_work: str | None) -> str:
    if is_infrastructure_category(category_of_work):
        return "infrastructure-v1"
    return "piling-v1"


def is_infrastructure_category(category_of_work: str | None) -> bool:
    return "infrastructure" in (category_of_work or "").lower()


def main() -> None:
    started_at = time.perf_counter()
    load_dotenv()
    args = parse_args()
    if args.limit < 1:
        raise SystemExit("--limit must be at least 1.")
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be at least 1.")

    if not mysql_enabled():
        raise SystemExit("MYSQL_ENABLED must be true in .env before reading ai_bq_trade_code_suggestions.")

    if args.list_categories:
        list_categories()
        return
    if args.list_source_categories:
        list_source_categories()
        return
    if args.load_source_category:
        load_source_category(
            category_of_work=args.load_source_category,
            company_id=args.company_id,
            limit=args.limit,
            dry_run=args.dry_run,
        )
        return

    provider = args.provider or os.getenv("AI_PROVIDER", "mock").lower().strip()
    model = args.model or default_model(provider)
    run_id = args.run_id or f"trade-code-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    args.prompt_version = args.prompt_version or prompt_version_for_category(args.category_of_work)

    log(
        "worker_start",
        provider=provider,
        model=model,
        run_id=run_id,
        limit=args.limit,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        include_processed=args.include_processed,
        prompt_version=args.prompt_version,
    )

    if args.dry_run:
        log("ensure_result_table_skipped", reason="dry_run")
    else:
        ensure_result_table()

    total_processed = 0
    while True:
        fetch_started_at = time.perf_counter()
        log("fetch_rows_start")
        exclude_run_id = run_id if not args.dry_run and (args.include_processed or args.history_only) else None
        rows = fetch_rows(
            limit=args.limit,
            ids=parse_ids(args.ids),
            include_processed=args.include_processed,
            exclude_run_id=exclude_run_id,
            category_of_work=args.category_of_work,
        )
        log("fetch_rows_done", rows=len(rows), elapsed_seconds=elapsed(fetch_started_at))
        if not rows:
            log("no_rows_to_process")
            break

        log("rows_loaded", rows=len(rows))

        batches = chunked(rows, args.batch_size)
        for batch_index, batch in enumerate(batches, start=1):
            batch_started_at = time.perf_counter()
            log(
                "batch_start",
                batch_index=batch_index,
                batch_count=len(batches),
                row_count=len(batch),
                first_row_id=batch[0]["id"],
                last_row_id=batch[-1]["id"],
            )
            try:
                ai_started_at = time.perf_counter()
                log("ai_request_start", batch_index=batch_index, row_count=len(batch))
                suggestions = suggest_trade_codes(provider, model, batch, args.prompt_version)
                log(
                    "ai_request_done",
                    batch_index=batch_index,
                    suggestions=len(suggestions),
                    elapsed_seconds=elapsed(ai_started_at),
                )
            except Exception as exc:
                error = f"{exc.__class__.__name__}: {exc}"
                log("ai_request_error", batch_index=batch_index, error=error, elapsed_seconds=elapsed(batch_started_at))
                for row in batch:
                    mark_error(row["id"], error, args.dry_run)
                continue

            suggestions_by_id = suggestions_by_source_id(suggestions)

            for row in batch:
                suggestion = suggestions_by_id.get(int(row["id"]))
                if not suggestion:
                    retry_started_at = time.perf_counter()
                    log("missing_row_retry_start", source_id=row["id"], batch_index=batch_index)
                    try:
                        retry_suggestions = suggest_trade_codes(provider, model, [row], args.prompt_version)
                        suggestion = suggestions_by_source_id(retry_suggestions).get(int(row["id"]))
                        log(
                            "missing_row_retry_done",
                            source_id=row["id"],
                            found=bool(suggestion),
                            elapsed_seconds=elapsed(retry_started_at),
                        )
                    except Exception as exc:
                        error = f"Retry failed. {exc.__class__.__name__}: {exc}"
                        log("missing_row_retry_error", source_id=row["id"], error=error)
                        mark_error(row["id"], error, args.dry_run)
                        continue

                if not suggestion:
                    mark_error(row["id"], "AI response did not include this row id, including one-row retry.", args.dry_run)
                    continue

                suggestion = apply_canonical_overrides(row, suggestion)

                if args.dry_run:
                    log("dry_run_suggestion", source_id=row["id"])
                    print(json.dumps({"source_id": row["id"], **suggestion}, ensure_ascii=False))
                else:
                    insert_result_started_at = time.perf_counter()
                    log("db_result_insert_start", source_id=row["id"])
                    insert_result_row(
                        source_row=row,
                        suggestion=suggestion,
                        provider=provider,
                        model=model,
                        run_id=run_id,
                        prompt_version=args.prompt_version,
                    )
                    log("db_result_insert_done", source_id=row["id"], elapsed_seconds=elapsed(insert_result_started_at))

                if not args.dry_run and not args.history_only:
                    update_started_at = time.perf_counter()
                    log("db_update_start", source_id=row["id"])
                    update_row(
                        row_id=row["id"],
                        suggestion=suggestion,
                        provider=provider,
                        model=model,
                        run_id=run_id,
                        prompt_version=args.prompt_version,
                    )
                    log("db_update_done", source_id=row["id"], elapsed_seconds=elapsed(update_started_at))
                total_processed += 1

            if args.sleep_seconds > 0:
                log("sleep_start", seconds=args.sleep_seconds)
                time.sleep(args.sleep_seconds)
                log("sleep_done", seconds=args.sleep_seconds)

            log(
                "batch_done",
                batch_index=batch_index,
                processed=total_processed,
                elapsed_seconds=elapsed(batch_started_at),
            )

        if args.dry_run or args.ids or len(rows) < args.limit:
            break

    log("worker_done", processed=total_processed, dry_run=args.dry_run, elapsed_seconds=elapsed(started_at))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Suggest BQ trade codes using OpenAI or Claude.")
    parser.add_argument("--provider", choices=["openai", "claude", "mock"], help="AI provider. Defaults to AI_PROVIDER.")
    parser.add_argument("--model", help="Model override. Defaults to OPENAI_MODEL or CLAUDE_MODEL.")
    parser.add_argument("--limit", type=int, default=50, help="Rows to load per loop.")
    parser.add_argument("--batch-size", type=int, default=10, help="Rows per AI request.")
    parser.add_argument(
        "--company-id",
        type=int,
        default=int(os.getenv("MYSQL_DEFAULT_COMPANY_ID", "1452")),
        help="Company id for source category loading/diagnostics.",
    )
    parser.add_argument("--ids", help="Comma-separated ai_bq_trade_code_suggestions.id values to process.")
    parser.add_argument("--include-processed", action="store_true", help="Also process rows that already have a trade code.")
    parser.add_argument("--prompt-version", default="", help="Prompt version stored back to DB. Defaults from category.")
    parser.add_argument(
        "--category-of-work",
        default=os.getenv("TRADE_CODE_CATEGORY", ""),
        help='Optional category filter, e.g. "Piling" or "Infrastructure Works".',
    )
    parser.add_argument("--run-id", help="Run id stored back to DB.")
    parser.add_argument("--sleep-seconds", type=float, default=0.2, help="Delay between AI requests.")
    parser.add_argument("--dry-run", action="store_true", help="Print suggestions without updating DB.")
    parser.add_argument(
        "--history-only",
        action="store_true",
        help="Insert into ai_bq_trade_code_suggestion_results without updating latest columns on the source table.",
    )
    parser.add_argument(
        "--list-categories",
        action="store_true",
        help="Print category_of_work counts from ai_bq_trade_code_suggestions and exit.",
    )
    parser.add_argument(
        "--list-source-categories",
        action="store_true",
        help="Print category_of_work counts from awarded Contract BQ source tables and exit.",
    )
    parser.add_argument(
        "--load-source-category",
        help='Load awarded Contract BQ rows for this category into ai_bq_trade_code_suggestions, e.g. "Infrastructure Works".',
    )
    return parser.parse_args()


def default_model(provider: str) -> str:
    if provider == "openai":
        return os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    if provider == "claude":
        return os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-latest")
    return "mock"


def parse_ids(raw_ids: str | None) -> List[int]:
    if not raw_ids:
        return []
    return [int(part.strip()) for part in raw_ids.split(",") if part.strip()]


def fetch_rows(
    limit: int,
    ids: List[int],
    include_processed: bool,
    exclude_run_id: str | None = None,
    category_of_work: str | None = None,
) -> List[Dict[str, Any]]:
    id_filter = ""
    params: List[Any] = []
    if ids:
        placeholders = ", ".join(["%s"] * len(ids))
        id_filter = f"AND id IN ({placeholders})"
        params.extend(ids)

    category_filter = ""
    if category_of_work:
        if is_infrastructure_category(category_of_work):
            category_filter = "AND LOWER(category_of_work) LIKE %s"
            params.append("%infrastructure%")
        elif "piling" in category_of_work.lower():
            category_filter = "AND LOWER(category_of_work) LIKE %s"
            params.append("%piling%")
        else:
            category_filter = "AND category_of_work = %s"
            params.append(category_of_work)

    processed_filter = ""
    if not include_processed:
        processed_filter = """
            AND (
                review_status IN ('pending', 'needs_rerun', 'ai_error')
                OR review_status IS NULL
                OR suggested_trade_code IS NULL
                OR suggested_trade_code = ''
            )
        """

    exclude_existing_result_filter = ""
    if exclude_run_id:
        exclude_existing_result_filter = """
            AND NOT EXISTS (
                SELECT 1
                FROM max_purchasing.ai_bq_trade_code_suggestion_results existing_result
                WHERE existing_result.source_suggestion_id = src.id
                  AND existing_result.ai_run_id = %s
            )
        """
        params.append(exclude_run_id)

    sql = f"""
        SELECT
            id,
            letter_award_id,
            contract_no,
            business_unit,
            project_name,
            project_shortname,
            awarded_date,
            contractor_name,
            category_of_work,
            letter_award_tab_id,
            tab_name,
            bq_item_id,
            item_ref_no,
            bq_item_no,
            parent_header_3,
            parent_header_2,
            parent_header_1,
            bq_item_description,
            full_bq_description,
            uom_code,
            quantity,
            unit_rate,
            tender_amount,
            ai_matching_text
        FROM max_purchasing.ai_bq_trade_code_suggestions src
        WHERE 1 = 1
          {id_filter}
          {category_filter}
          {processed_filter}
          {exclude_existing_result_filter}
        ORDER BY letter_award_id, letter_award_tab_id, item_ref_no, id
        LIMIT %s
    """
    params.append(limit)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()


def list_categories() -> None:
    sql = """
        SELECT
            COALESCE(NULLIF(category_of_work, ''), '(blank)') AS category_of_work,
            COALESCE(NULLIF(review_status, ''), '(blank)') AS review_status,
            COUNT(*) AS row_count,
            SUM(CASE
                WHEN suggested_trade_code IS NULL OR suggested_trade_code = ''
                THEN 1 ELSE 0
            END) AS without_trade_code
        FROM max_purchasing.ai_bq_trade_code_suggestions
        GROUP BY
            COALESCE(NULLIF(category_of_work, ''), '(blank)'),
            COALESCE(NULLIF(review_status, ''), '(blank)')
        ORDER BY category_of_work, review_status
    """
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()

    if not rows:
        print("No rows found in max_purchasing.ai_bq_trade_code_suggestions.")
        return

    print("category_of_work\treview_status\trow_count\twithout_trade_code")
    for row in rows:
        print(
            f"{row['category_of_work']}\t{row['review_status']}\t"
            f"{row['row_count']}\t{row['without_trade_code']}"
        )


def list_source_categories() -> None:
    sql = """
        SELECT
            COALESCE(pwc.description, '(blank)') AS category_of_work,
            COUNT(DISTINCT la.id) AS contract_count,
            COUNT(lai.id) AS bq_item_count
        FROM max_purchasing.letter_awards la
        JOIN max_purchasing.tenders t ON t.id = la.tender_id
        JOIN max_purchasing.letter_award_tabs lat ON lat.letter_award_id = la.id
        JOIN max_purchasing.letter_award_items lai ON lai.letter_award_tab_id = lat.id
        LEFT JOIN max_project.project_work_categories pwc
            ON pwc.id = COALESCE(la.project_work_category_id, t.project_work_category_id)
        WHERE t.status = 9
          AND la.status NOT IN (4, 5)
          AND COALESCE(lai.is_deleted, 0) = 0
          AND COALESCE(lai.is_include, 1) = 1
          AND COALESCE(lai.by_others, 0) = 0
        GROUP BY COALESCE(pwc.description, '(blank)')
        ORDER BY bq_item_count DESC, category_of_work
    """
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()

    if not rows:
        print("No awarded Contract BQ source rows found.")
        return

    print("category_of_work\tcontract_count\tbq_item_count")
    for row in rows:
        print(f"{row['category_of_work']}\t{row['contract_count']}\t{row['bq_item_count']}")


def source_category_filter_sql(category_of_work: str) -> tuple[str, List[Any]]:
    if is_infrastructure_category(category_of_work):
        return "AND LOWER(COALESCE(pwc.description, '')) LIKE %s", ["%infrastructure%"]
    if "piling" in category_of_work.lower():
        return "AND LOWER(COALESCE(pwc.description, '')) LIKE %s", ["%piling%"]
    return "AND pwc.description = %s", [category_of_work]


def load_source_category(category_of_work: str, company_id: int, limit: int, dry_run: bool) -> None:
    category_filter, category_params = source_category_filter_sql(category_of_work)
    prompt_version = prompt_version_for_category(category_of_work)

    count_sql = f"""
        SELECT COUNT(*) AS row_count
        FROM max_purchasing.letter_awards la
        JOIN max_purchasing.tenders t ON t.id = la.tender_id
        JOIN max_project.projects p ON p.id = la.project_id
        JOIN max_purchasing.letter_award_tabs lat ON lat.letter_award_id = la.id
        JOIN max_purchasing.letter_award_items lai ON lai.letter_award_tab_id = lat.id
        LEFT JOIN max_project.project_work_categories pwc
            ON pwc.id = COALESCE(la.project_work_category_id, t.project_work_category_id)
        WHERE la.company_id = %s
          AND t.status = 9
          AND la.status NOT IN (4, 5)
          AND COALESCE(lai.is_deleted, 0) = 0
          AND COALESCE(lai.is_include, 1) = 1
          AND COALESCE(lai.by_others, 0) = 0
          AND COALESCE(NULLIF(lai.tender_rate, 0), NULLIF(COALESCE(lai.material_rate, 0) + COALESCE(lai.service_rate, 0), 0), 0) > 0
          {category_filter}
          AND NOT EXISTS (
              SELECT 1
              FROM max_purchasing.ai_bq_trade_code_suggestions existing
              WHERE existing.bq_item_id = lai.id
          )
    """

    insert_sql = f"""
        INSERT INTO max_purchasing.ai_bq_trade_code_suggestions (
            letter_award_id,
            contract_no,
            business_unit,
            project_name,
            project_shortname,
            awarded_date,
            contractor_name,
            category_of_work,
            letter_award_tab_id,
            tab_name,
            bq_item_id,
            item_ref_no,
            bq_item_no,
            parent_header_3,
            parent_header_2,
            parent_header_1,
            bq_item_description,
            full_bq_description,
            uom_code,
            quantity,
            unit_rate,
            tender_amount,
            ai_matching_text,
            review_status,
            prompt_version
        )
        SELECT
            la.id AS letter_award_id,
            COALESCE(la.custom_letter_award_no, la.letter_award_no) AS contract_no,
            ch.description AS business_unit,
            p.project_name,
            NULL AS project_shortname,
            la.letter_award_date AS awarded_date,
            COALESCE(vendor.company_name, '-') AS contractor_name,
            pwc.description AS category_of_work,
            lat.id AS letter_award_tab_id,
            lat.content AS tab_name,
            lai.id AS bq_item_id,
            lai.id AS item_ref_no,
            lai.item AS bq_item_no,
            h3.content AS parent_header_3,
            h2.content AS parent_header_2,
            h1.content AS parent_header_1,
            lai.content AS bq_item_description,
            CONCAT_WS(' > ', lat.content, h3.content, h2.content, h1.content, lai.content) AS full_bq_description,
            cu.uom_code,
            lai.order_qty AS quantity,
            COALESCE(NULLIF(lai.tender_rate, 0), NULLIF(COALESCE(lai.material_rate, 0) + COALESCE(lai.service_rate, 0), 0)) AS unit_rate,
            COALESCE(lai.tender_amount, COALESCE(lai.material_amount, 0) + COALESCE(lai.service_amount, 0)) AS tender_amount,
            CONCAT_WS(' > ', lat.content, h3.content, h2.content, h1.content, lai.content) AS ai_matching_text,
            'pending' AS review_status,
            %s AS prompt_version
        FROM max_purchasing.letter_awards la
        JOIN max_purchasing.tenders t ON t.id = la.tender_id
        JOIN max_project.projects p ON p.id = la.project_id
        LEFT JOIN max_base.company_profiles vendor ON vendor.id = la.vendor_id
        LEFT JOIN max_base.company_hierarchies ch ON ch.id = p.company_hierarchy_id
        LEFT JOIN max_project.project_work_categories pwc
            ON pwc.id = COALESCE(la.project_work_category_id, t.project_work_category_id)
        JOIN max_purchasing.letter_award_tabs lat ON lat.letter_award_id = la.id
        JOIN max_purchasing.letter_award_items lai ON lai.letter_award_tab_id = lat.id
        LEFT JOIN max_purchasing.letter_award_items h1 ON h1.id = lai.parent_id
        LEFT JOIN max_purchasing.letter_award_items h2 ON h2.id = h1.parent_id
        LEFT JOIN max_purchasing.letter_award_items h3 ON h3.id = h2.parent_id
        LEFT JOIN max_base.config_uom cu ON cu.id = lai.uom_id
        WHERE la.company_id = %s
          AND t.status = 9
          AND la.status NOT IN (4, 5)
          AND COALESCE(lai.is_deleted, 0) = 0
          AND COALESCE(lai.is_include, 1) = 1
          AND COALESCE(lai.by_others, 0) = 0
          AND COALESCE(NULLIF(lai.tender_rate, 0), NULLIF(COALESCE(lai.material_rate, 0) + COALESCE(lai.service_rate, 0), 0), 0) > 0
          {category_filter}
          AND NOT EXISTS (
              SELECT 1
              FROM max_purchasing.ai_bq_trade_code_suggestions existing
              WHERE existing.bq_item_id = lai.id
          )
        ORDER BY la.letter_award_date DESC, la.id DESC, lat.seq, lai.parent_id, lai.seq
        LIMIT %s
    """

    count_params = [company_id, *category_params]
    insert_params = [prompt_version, company_id, *category_params, limit]
    started_at = time.perf_counter()

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(count_sql, count_params)
            available = (cursor.fetchone() or {}).get("row_count", 0)
            if dry_run:
                print(
                    f"Would load up to {min(int(available or 0), limit)} of {available} source rows "
                    f"for category={category_of_work!r}, company_id={company_id}."
                )
                return

            cursor.execute(insert_sql, insert_params)
            inserted = cursor.rowcount

    log(
        "source_category_loaded",
        category_of_work=category_of_work,
        company_id=company_id,
        available=available,
        inserted=inserted,
        prompt_version=prompt_version,
        elapsed_seconds=elapsed(started_at),
    )


def suggest_trade_codes(provider: str, model: str, rows: List[Dict[str, Any]], prompt_version: str) -> List[Dict[str, Any]]:
    category_of_work = first_category(rows)
    resolved_prompt_version = prompt_version or prompt_version_for_category(category_of_work)
    if provider == "mock":
        return [mock_suggestion(row) for row in rows]

    payload = {
        "prompt_version": resolved_prompt_version,
        "category_of_work": category_of_work,
        "items": [serialize_row(row) for row in rows],
    }
    user_prompt = "Classify these BQ items:\n" + json.dumps(payload, ensure_ascii=False, indent=2)

    if provider == "openai":
        return ask_openai(model, user_prompt, resolved_prompt_version, category_of_work)
    if provider == "claude":
        return ask_claude(model, user_prompt, resolved_prompt_version, category_of_work)
    raise ValueError(f"Unsupported provider: {provider}")


def first_category(rows: List[Dict[str, Any]]) -> str:
    for row in rows:
        category = str(row.get("category_of_work") or "").strip()
        if category:
            return category
    return ""


def ask_openai(model: str, user_prompt: str, prompt_version: str, category_of_work: str | None) -> List[Dict[str, Any]]:
    from openai import OpenAI

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    request = {
        "model": model,
        "messages": [
            {"role": "system", "content": active_system_prompt(prompt_version, category_of_work)},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    try:
        response = client.chat.completions.create(**request)
    except Exception:
        request.pop("response_format", None)
        response = client.chat.completions.create(**request)
    return parse_json_array(response.choices[0].message.content or "")


def ask_claude(model: str, user_prompt: str, prompt_version: str, category_of_work: str | None) -> List[Dict[str, Any]]:
    from anthropic import Anthropic

    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model=model,
        max_tokens=4000,
        temperature=0.1,
        system=active_system_prompt(prompt_version, category_of_work),
        messages=[{"role": "user", "content": user_prompt}],
    )
    content = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
    return parse_json_array(content)


def parse_json_array(content: str) -> List[Dict[str, Any]]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", text)
        if not match:
            raise ValueError(f"AI response is not valid JSON. preview={text[:500]!r}")
        parsed = json.loads(match.group(0))

    if isinstance(parsed, dict):
        for key in ["items", "suggestions", "results", "classifications", "data"]:
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]
                break
        else:
            if all(field in parsed for field in ["id", "suggested_trade_code"]):
                parsed = [parsed]

    if not isinstance(parsed, list):
        raise ValueError(f"AI response must be a JSON array. preview={text[:500]!r}")
    return parsed


def suggestions_by_source_id(suggestions: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    mapped: Dict[int, Dict[str, Any]] = {}
    for suggestion in suggestions:
        if not isinstance(suggestion, dict):
            continue
        raw_id = first_present(suggestion, ["id", "source_id", "source_suggestion_id", "row_id"])
        if raw_id is None:
            continue
        try:
            mapped[int(raw_id)] = suggestion
        except (TypeError, ValueError):
            continue
    return mapped


def first_present(data: Dict[str, Any], keys: List[str]) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def apply_canonical_overrides(row: Dict[str, Any], suggestion: Dict[str, Any]) -> Dict[str, Any]:
    if is_infrastructure_category(row.get("category_of_work")):
        return apply_infrastructure_overrides(row, suggestion)

    text = source_text(row)
    size = detect_pile_size(text)
    pile_type = detect_pile_type(text)
    load = detect_test_load(text)

    is_pda = "pda" in text or "pile driving analysis" in text
    is_static_load = any(
        phrase in text
        for phrase in [
            "preliminary load test",
            "subsequent load test",
            "maintained load test",
            "mantained load test",
            "static load test",
            "load test",
            "loading arrangement",
        ]
    )

    if is_pda and size:
        code = f"PIL-{pile_type}-{size}-PDA"
        return {
            **suggestion,
            "suggested_trade_code": code,
            "suggested_trade_name": f"PDA test on {size} pile",
            "spec_key": f"{size}-PDA",
            "confidence": max(safe_confidence(suggestion.get("confidence")) or 0, 0.95),
            "reasoning": "Canonical override: source text explicitly says PDA/Pile Driving Analysis.",
        }

    is_supply = any(
        phrase in text
        for phrase in [
            "supply and deliver",
            "supply & deliver",
            "supply, deliver",
            "supply of",
            "deliver to site only",
        ]
    )
    is_drive = any(
        phrase in text
        for phrase in [
            "pitch and drive",
            "pitching and driving",
            "pitching and  driving",
            "drive to the required set",
            "drive to the required sets",
            "driving measured separately",
            "uplift from stack",
        ]
    )
    is_joint = any(
        phrase in text
        for phrase in [
            "splice",
            "weld",
            "sleeve joint",
            "pile joint",
            "pile joints",
            "extension joint",
        ]
    )
    is_cut = any(
        phrase in text
        for phrase in ["cut-off", "cut off", "cut pile head", "cutting of pile heads", "cutting pile heads"]
    )
    is_cut_obstruction = is_cut and "ground level" in text and "obstruction" in text

    if is_cut_obstruction and size:
        code = f"PIL-{pile_type}-{size}-CUT-OBSTRUCTION"
        return {
            **suggestion,
            "suggested_trade_code": code,
            "suggested_trade_name": f"Cut {size} pile head to ground level due to obstruction",
            "spec_key": f"{size}-CUT-OBSTRUCTION",
            "confidence": max(safe_confidence(suggestion.get("confidence")) or 0, 0.95),
            "reasoning": "Canonical override: cut pile head to ground level due to obstruction is a separate scope.",
        }

    if is_cut and size:
        code = f"PIL-{pile_type}-{size}-CUT"
        return {
            **suggestion,
            "suggested_trade_code": code,
            "suggested_trade_name": f"Cut {size} pile head",
            "spec_key": f"{size}-CUT",
            "confidence": max(safe_confidence(suggestion.get("confidence")) or 0, 0.95),
            "reasoning": "Canonical override: source text indicates cut-off/cutting of pile heads.",
        }

    if is_supply and size:
        code = f"PIL-{pile_type}-{size}-SUPPLY"
        return {
            **suggestion,
            "suggested_trade_code": code,
            "suggested_trade_name": f"Supply {size} pile",
            "spec_key": f"{size}-SUPPLY",
            "confidence": max(safe_confidence(suggestion.get("confidence")) or 0, 0.95),
            "reasoning": "Canonical override: source text indicates supply and delivery.",
        }

    if is_joint and size:
        code = f"PIL-{pile_type}-{size}-JOINT"
        return {
            **suggestion,
            "suggested_trade_code": code,
            "suggested_trade_name": f"Joint {size} pile",
            "spec_key": f"{size}-JOINT",
            "confidence": max(safe_confidence(suggestion.get("confidence")) or 0, 0.95),
            "reasoning": "Canonical override: source text indicates splice/weld/sleeve/pile joint.",
        }

    if is_drive and size:
        code = f"PIL-{pile_type}-{size}-DRIVE"
        return {
            **suggestion,
            "suggested_trade_code": code,
            "suggested_trade_name": f"Drive {size} pile",
            "spec_key": f"{size}-DRIVE",
            "confidence": max(safe_confidence(suggestion.get("confidence")) or 0, 0.95),
            "reasoning": "Canonical override: source text indicates pitch/drive/uplift from stack.",
        }

    if is_static_load and size:
        load_activity = detect_load_activity(text)
        code = f"PIL-{pile_type}-{size}-{load_activity}" + (f"-{load}" if load else "")
        return {
            **suggestion,
            "suggested_trade_code": code,
            "suggested_trade_name": f"{load_activity.replace('-', ' ').title()} on {size} pile" + (f" to {load}" if load else ""),
            "spec_key": f"{size}-{load_activity}" + (f"-{load}" if load else ""),
            "confidence": max(safe_confidence(suggestion.get("confidence")) or 0, 0.95),
            "reasoning": "Canonical override: source text indicates load test sequence and does not explicitly say PDA/supply/drive/joint/cut.",
        }

    general_override = detect_general_activity(text)
    if general_override:
        activity, trade_name, spec_key, reason = general_override
        return {
            **suggestion,
            "suggested_trade_code": f"PIL-GEN-GEN-{activity}",
            "suggested_trade_name": trade_name,
            "spec_key": spec_key,
            "confidence": max(safe_confidence(suggestion.get("confidence")) or 0, 0.95),
            "reasoning": reason,
        }

    return suggestion


def apply_infrastructure_overrides(row: Dict[str, Any], suggestion: Dict[str, Any]) -> Dict[str, Any]:
    text = source_text(row)
    system = detect_infra_system(text)
    asset = detect_infra_asset(text, system)
    activity = detect_infra_activity(text)
    code = f"INF-{system}-{asset}-{activity}"
    trade_name = infra_trade_name(system, asset, activity)

    return {
        **suggestion,
        "suggested_trade_code": code,
        "suggested_trade_name": trade_name,
        "spec_key": f"{system}-{asset}-{activity}",
        "confidence": max(safe_confidence(suggestion.get("confidence")) or 0, 0.85),
        "reasoning": "Canonical override: Infrastructure Works keyword classification by system, asset, and activity.",
    }


def detect_infra_system(text: str) -> str:
    if any(word in text for word in ["street light", "streetlight", "lamp pole", "lighting pole", "feeder pillar"]):
        return "LIGHT"
    if any(word in text for word in ["telecom", "telco", "fibre", "fiber", "communication duct"]):
        return "TELCO"
    if any(word in text for word in ["tnb", "electrical", "electric", "cable trench", "cable duct", "duct bank"]):
        return "ELEC"
    if any(word in text for word in ["water main", "water reticulation", "hdpe", "mscl", "di pipe", "sluice valve", "hydrant"]):
        return "WATER"
    if any(word in text for word in ["sewer", "sewerage", "manhole", "inspection chamber", "septic"]):
        return "SEWER"
    if any(word in text for word in ["drain", "culvert", "catchpit", "catch pit", "sump", "swale", "u-drain", "v-drain"]):
        return "DRAIN"
    if any(word in text for word in ["road", "pavement", "asphalt", "premix", "wearing course", "binder course", "crusher run", "roadbase", "kerb", "road marking"]):
        return "ROAD"
    if any(word in text for word in ["turfing", "planting", "landscape", "hydroseeding", "grass"]):
        return "LAND"
    if any(word in text for word in ["retaining wall", "headwall", "box culvert", "bridge", "rc wall"]):
        return "STRUCT"
    if any(word in text for word in ["earthwork", "excavat", "cut ", " fill", "backfill", "compaction", "dispose", "clearing", "grubbing"]):
        return "EW"
    return "GEN"


def detect_infra_asset(text: str, system: str) -> str:
    pipe_size = detect_pipe_size(text)
    drain_size = detect_infra_dimension(text)

    if "manhole" in text:
        return "MANHOLE"
    if "inspection chamber" in text:
        return "CHAMBER"
    if "catchpit" in text or "catch pit" in text:
        return "CATCHPIT"
    if "culvert" in text:
        return f"CULVERT-{drain_size}" if drain_size else "CULVERT"
    if "u-drain" in text or "u drain" in text or "rc drain" in text or "precast drain" in text:
        return f"RC-DRAIN-{drain_size}" if drain_size else "RC-DRAIN"
    if "trench" in text:
        return "TRENCH"
    if "pipe" in text or system in ["WATER", "SEWER"]:
        return f"PIPE-{pipe_size}" if pipe_size else "PIPE-GEN"
    if any(word in text for word in ["asphalt", "premix", "wearing course", "binder course"]):
        return "ASPHALT"
    if "crusher run" in text or "roadbase" in text or "road base" in text or "sub-base" in text:
        return "ROADBASE"
    if "kerb" in text:
        return "KERB"
    if "road marking" in text or "line marking" in text:
        return "MARKING"
    if "hydrant" in text:
        return "HYDRANT"
    if "valve" in text:
        return "VALVE"
    if "pole" in text:
        return "POLE"
    if "duct" in text:
        return "DUCT"
    if "cable" in text:
        return "CABLE"
    return "GEN"


def detect_pipe_size(text: str) -> str | None:
    match = re.search(r"(\d{2,4})\s*mm", text)
    if match:
        return f"{match.group(1)}MM"
    return None


def detect_infra_dimension(text: str) -> str | None:
    square_match = re.search(r"(\d{2,4})\s*mm?\s*x\s*(\d{2,4})\s*mm?", text)
    if square_match:
        return f"{square_match.group(1)}X{square_match.group(2)}"
    return detect_pipe_size(text)


def detect_infra_activity(text: str) -> str:
    if any(word in text for word in ["survey", "setting out", "as-built", "as built"]):
        return "SURVEY"
    if any(word in text for word in ["mobilisation", "mobilization", "demobilisation", "demobilization"]):
        return "MOB"
    if any(word in text for word in ["clearing", "grubbing", "clear site"]):
        return "CLEAR"
    if any(word in text for word in ["excavat", "trench"]):
        return "EXCAVATE"
    if any(word in text for word in ["backfill", "filling", " fill"]):
        return "FILL"
    if any(word in text for word in ["compact", "compaction"]):
        return "COMPACT"
    if any(word in text for word in ["cart away", "dispose", "disposal", "remove surplus"]):
        return "DISPOSE"
    if any(word in text for word in ["pressure test", "water test", "testing", "test "]):
        return "TEST"
    if any(word in text for word in ["connect", "connection", "tie-in", "tie in"]):
        return "CONNECT"
    if any(word in text for word in ["reinstate", "reinstatement", "make good"]):
        return "REINSTATE"
    if any(word in text for word in ["protect", "protection", "temporary support"]):
        return "PROTECT"
    if any(word in text for word in ["road marking", "line marking"]):
        return "MARKING"
    if any(word in text for word in ["supply and lay", "supply & lay", "lay ", "laying"]):
        return "LAY"
    if any(word in text for word in ["install", "installation", "fixing", "erect"]):
        return "INSTALL"
    if any(word in text for word in ["construct", "construction", "cast in-situ", "cast in situ", "build"]):
        return "CONSTRUCT"
    if any(word in text for word in ["supply", "provide"]):
        return "SUPPLY"
    return "OTHER"


def infra_trade_name(system: str, asset: str, activity: str) -> str:
    system_names = {
        "GEN": "General infrastructure",
        "EW": "Earthwork",
        "ROAD": "Roadwork",
        "DRAIN": "Drainage",
        "SEWER": "Sewerage",
        "WATER": "Water reticulation",
        "ELEC": "Electrical infrastructure",
        "TELCO": "Telecommunication infrastructure",
        "LIGHT": "Street lighting",
        "LAND": "Landscape infrastructure",
        "STRUCT": "Infrastructure structure",
    }
    return f"{activity.replace('-', ' ').title()} {asset.replace('-', ' ').title()} ({system_names.get(system, system)})"


def source_text(row: Dict[str, Any]) -> str:
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


def detect_pile_size(text: str) -> str | None:
    square_match = re.search(r"(\d{3,4})\s*mm?\s*x\s*(\d{3,4})\s*mm?", text)
    if square_match:
        return f"{square_match.group(1)}X{square_match.group(2)}"

    compact_match = re.search(r"(\d{3,4})\s*x\s*(\d{3,4})", text)
    if compact_match:
        return f"{compact_match.group(1)}X{compact_match.group(2)}"

    diameter_match = re.search(r"(\d{3,4})\s*mm", text)
    if diameter_match:
        return f"{diameter_match.group(1)}MM"

    return None


def detect_pile_type(text: str) -> str:
    if "spun" in text:
        return "SPUN"
    if "bored" in text or "bore" in text:
        return "BORED"
    if any(word in text for word in ["prestressed", "precast", "reinforced concrete", "rc pile"]):
        return "PRC"
    return "PRC"


def detect_test_load(text: str) -> str | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:tons?|tonnes?|ton\b|t\b)", text)
    if not match:
        return None
    number = match.group(1)
    if number.endswith(".0"):
        number = number[:-2]
    return f"{number}T"


def detect_load_activity(text: str) -> str:
    if "preliminary load test" in text:
        return "LOAD-PRELIM"
    if "subsequent load test" in text:
        return "LOAD-SUBSEQ"
    if "maintained load test" in text or "mantained load test" in text or "static load test" in text:
        return "LOAD-MAINT"
    return "LOAD"


def detect_general_activity(text: str) -> tuple[str, str, str, str] | None:
    if any(
        phrase in text
        for phrase in [
            "removal of obstructions",
            "remove obstructions",
            "underground obstructions",
            "obstructions below ground",
            "below ground not exceeding",
        ]
    ):
        return (
            "OBSTRUCTION",
            "Removal of underground obstructions",
            "GEN-OBSTRUCTION",
            "Canonical override: source text indicates removal of underground obstructions.",
        )

    if any(
        phrase in text
        for phrase in [
            "licensed surveyor",
            "setting out",
            "as-built",
            "as built",
            "final survey",
            "pile eccentricities",
            "survey of pile",
        ]
    ):
        return (
            "SURVEY",
            "Survey for pile setting out and as-built",
            "GEN-SURVEY",
            "Canonical override: source text indicates survey, setting out, or as-built survey.",
        )

    if any(
        phrase in text
        for phrase in [
            "piling records",
            "driving records",
            "pile records",
            "blow count records",
            "number of blows",
            "record them in the piling records",
        ]
    ):
        return (
            "RECORD",
            "Piling driving records",
            "GEN-RECORD",
            "Canonical override: source text indicates piling records or blow count records.",
        )

    if any(
        phrase in text
        for phrase in [
            "piling equipment",
            "piling plant",
            "mobilisation",
            "mobilization",
            "demobilisation",
            "demobilization",
            "assembling",
            "dismantling",
            "removal of piling equipment",
        ]
    ):
        return (
            "EQUIP-MOB",
            "Mobilisation and demobilisation of piling equipment",
            "GEN-EQUIP-MOB",
            "Canonical override: source text indicates mobilisation, demobilisation, or removal of piling equipment.",
        )

    return None


def update_row(
    row_id: int,
    suggestion: Dict[str, Any],
    provider: str,
    model: str,
    run_id: str,
    prompt_version: str,
) -> None:
    sql = """
        UPDATE max_purchasing.ai_bq_trade_code_suggestions
        SET
            suggested_trade_code = %s,
            suggested_trade_name = %s,
            spec_key = %s,
            confidence = %s,
            reasoning = %s,
            review_status = 'ai_suggested',
            prompt_version = %s,
            ai_model = %s,
            ai_run_id = %s,
            ai_processed_at = NOW(),
            ai_error = NULL
        WHERE id = %s
    """
    params = (
        clean_text(suggestion.get("suggested_trade_code"), 100),
        clean_text(suggestion.get("suggested_trade_name"), 255),
        clean_text(suggestion.get("spec_key"), 255),
        safe_confidence(suggestion.get("confidence")),
        clean_text(suggestion.get("reasoning"), 2000),
        prompt_version,
        f"{provider}:{model}",
        run_id,
        row_id,
    )

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)


def ensure_result_table() -> None:
    sql = """
        CREATE TABLE IF NOT EXISTS max_purchasing.ai_bq_trade_code_suggestion_results (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            source_suggestion_id BIGINT NOT NULL,
            letter_award_id BIGINT NULL,
            contract_no VARCHAR(255) NULL,
            letter_award_tab_id BIGINT NULL,
            tab_name VARCHAR(255) NULL,
            bq_item_id BIGINT NULL,
            item_ref_no BIGINT NULL,
            uom_code VARCHAR(100) NULL,
            quantity DECIMAL(20, 4) NULL,
            unit_rate DECIMAL(20, 4) NULL,
            full_bq_description TEXT NULL,
            ai_matching_text TEXT NULL,

            ai_provider VARCHAR(50) NOT NULL,
            ai_model VARCHAR(100) NOT NULL,
            ai_run_id VARCHAR(100) NOT NULL,
            prompt_version VARCHAR(50) NOT NULL,

            suggested_trade_code VARCHAR(100) NULL,
            suggested_trade_name VARCHAR(255) NULL,
            spec_key VARCHAR(255) NULL,
            confidence DECIMAL(5, 2) NULL,
            reasoning TEXT NULL,
            ai_error TEXT NULL,
            created_date DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            modified_date DATETIME NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,

            UNIQUE KEY uq_ai_bq_result_source_run (source_suggestion_id, ai_run_id),
            KEY idx_ai_bq_result_source (source_suggestion_id),
            KEY idx_ai_bq_result_provider_run (ai_provider, ai_run_id),
            KEY idx_ai_bq_result_trade_code (suggested_trade_code),
            KEY idx_ai_bq_result_bq_item (bq_item_id)
        )
    """
    started_at = time.perf_counter()
    log("ensure_result_table_start")
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql)
    log("ensure_result_table_done", elapsed_seconds=elapsed(started_at))


def insert_result_row(
    source_row: Dict[str, Any],
    suggestion: Dict[str, Any],
    provider: str,
    model: str,
    run_id: str,
    prompt_version: str,
) -> None:
    sql = """
        INSERT INTO max_purchasing.ai_bq_trade_code_suggestion_results (
            source_suggestion_id,
            letter_award_id,
            contract_no,
            letter_award_tab_id,
            tab_name,
            bq_item_id,
            item_ref_no,
            uom_code,
            quantity,
            unit_rate,
            full_bq_description,
            ai_matching_text,
            ai_provider,
            ai_model,
            ai_run_id,
            prompt_version,
            suggested_trade_code,
            suggested_trade_name,
            spec_key,
            confidence,
            reasoning,
            ai_error
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL
        )
        ON DUPLICATE KEY UPDATE
            ai_provider = VALUES(ai_provider),
            ai_model = VALUES(ai_model),
            prompt_version = VALUES(prompt_version),
            suggested_trade_code = VALUES(suggested_trade_code),
            suggested_trade_name = VALUES(suggested_trade_name),
            spec_key = VALUES(spec_key),
            confidence = VALUES(confidence),
            reasoning = VALUES(reasoning),
            ai_error = NULL
    """
    params = (
        source_row.get("id"),
        source_row.get("letter_award_id"),
        source_row.get("contract_no"),
        source_row.get("letter_award_tab_id"),
        source_row.get("tab_name"),
        source_row.get("bq_item_id"),
        source_row.get("item_ref_no"),
        source_row.get("uom_code"),
        source_row.get("quantity"),
        source_row.get("unit_rate"),
        source_row.get("full_bq_description"),
        source_row.get("ai_matching_text"),
        provider,
        model,
        run_id,
        prompt_version,
        clean_text(suggestion.get("suggested_trade_code"), 100),
        clean_text(suggestion.get("suggested_trade_name"), 255),
        clean_text(suggestion.get("spec_key"), 255),
        safe_confidence(suggestion.get("confidence")),
        clean_text(suggestion.get("reasoning"), 2000),
    )

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)


def mark_error(row_id: int, error: str, dry_run: bool) -> None:
    if dry_run:
        print(json.dumps({"source_id": row_id, "error": error}))
        return

    sql = """
        UPDATE max_purchasing.ai_bq_trade_code_suggestions
        SET review_status = 'ai_error',
            ai_error = %s,
            ai_processed_at = NOW()
        WHERE id = %s
    """
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, (error[:2000], row_id))


def serialize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    keys = [
        "id",
        "contract_no",
        "business_unit",
        "category_of_work",
        "project_shortname",
        "tab_name",
        "item_ref_no",
        "bq_item_no",
        "parent_header_3",
        "parent_header_2",
        "parent_header_1",
        "bq_item_description",
        "full_bq_description",
        "uom_code",
        "quantity",
        "unit_rate",
    ]
    serialized = {key: json_value(row.get(key)) for key in keys}
    serialized["full_bq_description"] = truncate_text(serialized.get("full_bq_description"), 1200)
    serialized["bq_item_description"] = truncate_text(serialized.get("bq_item_description"), 700)
    return serialized


def json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def clean_text(value: Any, max_len: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_len]


def truncate_text(value: Any, max_len: int) -> Any:
    if value is None:
        return None
    text = str(value).strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "..."


def safe_confidence(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
        return max(0.0, min(1.0, number))
    except (TypeError, ValueError):
        return None


def mock_suggestion(row: Dict[str, Any]) -> Dict[str, Any]:
    if is_infrastructure_category(row.get("category_of_work")):
        return apply_infrastructure_overrides(
            row,
            {
                "id": row["id"],
                "suggested_trade_code": "",
                "suggested_trade_name": "",
                "spec_key": "",
                "confidence": 0.5,
                "reasoning": "Mock infrastructure classification based on keywords.",
            },
        )

    text = " ".join(
        str(row.get(key) or "")
        for key in ["full_bq_description", "bq_item_description", "ai_matching_text", "uom_code"]
    ).lower()
    uom = (row.get("uom_code") or "ITEM").upper()

    size_match = re.search(r"(\d{3,4})\s*mm", text)
    size = size_match.group(1) if size_match else "GEN"

    if "test" in text or "pda" in text or "load" in text:
        activity = "TEST"
        name = "Piling Test"
    elif "supply" in text:
        activity = "SUPPLY"
        name = "Supply Pile"
    elif any(word in text for word in ["drive", "driving", "install", "installation"]):
        activity = "DRIVE"
        name = "Drive Pile"
    elif any(word in text for word in ["joint", "extension", "weld"]):
        activity = "JOINT"
        name = "Pile Joint or Extension"
    elif any(word in text for word in ["cut", "hacking", "trim"]):
        activity = "CUT"
        name = "Pile Cut Off"
    else:
        activity = "WORK"
        name = "Piling Work"

    pile_type = "SPUN" if "spun" in text else "RC" if "rc" in text or "reinforced concrete" in text else "PILE"

    return {
        "id": row["id"],
        "suggested_trade_code": f"PIL-{pile_type}-{size}-{activity}-{uom}",
        "suggested_trade_name": name,
        "spec_key": f"{size}mm {pile_type.lower()} {activity.lower()} {uom.lower()}",
        "confidence": 0.5,
        "reasoning": "Mock classification based on keywords. Use OpenAI or Claude for production suggestions.",
    }


def chunked(items: List[Dict[str, Any]], size: int) -> List[List[Dict[str, Any]]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def log(event: str, **fields: Any) -> None:
    payload = " ".join(f"{key}={format_log_value(value)}" for key, value in fields.items())
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(f"[{timestamp}] {event}" + (f" {payload}" if payload else ""), flush=True)


def elapsed(started_at: float) -> str:
    return f"{time.perf_counter() - started_at:.3f}"


def format_log_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.3f}"
    text = str(value)
    if " " in text:
        return json.dumps(text, ensure_ascii=False)
    return text


if __name__ == "__main__":
    main()
