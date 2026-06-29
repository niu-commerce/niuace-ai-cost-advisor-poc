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


def active_system_prompt() -> str:
    rules_path = os.getenv("TRADE_CODE_PROMPT_RULES_FILE", "prompt_tuning_rules.txt")
    if not rules_path or not os.path.exists(rules_path):
        return SYSTEM_PROMPT

    with open(rules_path, "r", encoding="utf-8") as rules_file:
        extra_rules = rules_file.read().strip()

    if not extra_rules:
        return SYSTEM_PROMPT

    return f"{SYSTEM_PROMPT}\n\nAdditional audit-derived rules:\n{extra_rules}"


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

    provider = args.provider or os.getenv("AI_PROVIDER", "mock").lower().strip()
    model = args.model or default_model(provider)
    run_id = args.run_id or f"trade-code-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"

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
    parser = argparse.ArgumentParser(description="Suggest piling BQ trade codes using OpenAI or Claude.")
    parser.add_argument("--provider", choices=["openai", "claude", "mock"], help="AI provider. Defaults to AI_PROVIDER.")
    parser.add_argument("--model", help="Model override. Defaults to OPENAI_MODEL or CLAUDE_MODEL.")
    parser.add_argument("--limit", type=int, default=50, help="Rows to load per loop.")
    parser.add_argument("--batch-size", type=int, default=10, help="Rows per AI request.")
    parser.add_argument("--ids", help="Comma-separated ai_bq_trade_code_suggestions.id values to process.")
    parser.add_argument("--include-processed", action="store_true", help="Also process rows that already have a trade code.")
    parser.add_argument("--prompt-version", default="piling-v1", help="Prompt version stored back to DB.")
    parser.add_argument("--run-id", help="Run id stored back to DB.")
    parser.add_argument("--sleep-seconds", type=float, default=0.2, help="Delay between AI requests.")
    parser.add_argument("--dry-run", action="store_true", help="Print suggestions without updating DB.")
    parser.add_argument(
        "--history-only",
        action="store_true",
        help="Insert into ai_bq_trade_code_suggestion_results without updating latest columns on the source table.",
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
) -> List[Dict[str, Any]]:
    id_filter = ""
    params: List[Any] = []
    if ids:
        placeholders = ", ".join(["%s"] * len(ids))
        id_filter = f"AND id IN ({placeholders})"
        params.extend(ids)

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


def suggest_trade_codes(provider: str, model: str, rows: List[Dict[str, Any]], prompt_version: str) -> List[Dict[str, Any]]:
    if provider == "mock":
        return [mock_suggestion(row) for row in rows]

    payload = {
        "prompt_version": prompt_version,
        "items": [serialize_row(row) for row in rows],
    }
    user_prompt = "Classify these BQ items:\n" + json.dumps(payload, ensure_ascii=False, indent=2)

    if provider == "openai":
        return ask_openai(model, user_prompt)
    if provider == "claude":
        return ask_claude(model, user_prompt)
    raise ValueError(f"Unsupported provider: {provider}")


def ask_openai(model: str, user_prompt: str) -> List[Dict[str, Any]]:
    from openai import OpenAI

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    request = {
        "model": model,
        "messages": [
            {"role": "system", "content": active_system_prompt()},
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


def ask_claude(model: str, user_prompt: str) -> List[Dict[str, Any]]:
    from anthropic import Anthropic

    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model=model,
        max_tokens=4000,
        temperature=0.1,
        system=active_system_prompt(),
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
