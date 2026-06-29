import os
from contextlib import contextmanager

import pymysql


def mysql_enabled() -> bool:
    return os.getenv("MYSQL_ENABLED", "false").lower() == "true"


@contextmanager
def get_connection():
    connection = pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DATABASE", "max_purchasing"),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )
    try:
        yield connection
    finally:
        connection.close()


def fetch_contract_bq_summary(company_id: int = 1452, limit: int = 20):
    """Optional real-data query matching Contract BQ screen columns."""
    if not mysql_enabled():
        return []

    sql = """
        SELECT
            la.id AS letter_award_id,
            COALESCE(la.custom_letter_award_no, la.letter_award_no) AS contract_no,
            la.letter_award_date AS awarded_date,
            COALESCE(vendor.company_name, '-') AS contractor,
            ch.description AS business_unit,
            pwc.description AS category_of_work,
            COUNT(lai.id) AS total_bq_items,
            SUM(COALESCE(lai.material_amount, 0)) AS total_material_amount,
            SUM(COALESCE(lai.service_amount, 0)) AS total_service_amount,
            SUM(COALESCE(lai.material_amount, 0) + COALESCE(lai.service_amount, 0)) AS total_contract_amount,
            COUNT(CASE
                WHEN COALESCE(lai.order_qty, 0) > 0
                 AND COALESCE(NULLIF(lai.tender_rate, 0), NULLIF(COALESCE(lai.material_rate, 0) + COALESCE(lai.service_rate, 0), 0), 0) > 0
                THEN lai.id
            END) AS bq_with_qty_and_rate
        FROM max_purchasing.letter_awards la
        JOIN max_purchasing.tenders t ON t.id = la.tender_id
        JOIN max_project.projects p ON p.id = la.project_id
        LEFT JOIN max_base.company_profiles vendor ON vendor.id = la.vendor_id
        LEFT JOIN max_base.company_hierarchies ch ON ch.id = p.company_hierarchy_id
        LEFT JOIN max_project.project_work_categories pwc
            ON pwc.id = COALESCE(la.project_work_category_id, t.project_work_category_id)
        JOIN max_purchasing.letter_award_tabs lat ON lat.letter_award_id = la.id
        JOIN max_purchasing.letter_award_items lai ON lai.letter_award_tab_id = lat.id
        WHERE la.company_id = %s
          AND t.status = 9
          AND la.status NOT IN (4, 5)
          AND COALESCE(lai.is_deleted, 0) = 0
          AND COALESCE(lai.is_include, 1) = 1
          AND COALESCE(lai.by_others, 0) = 0
        GROUP BY la.id, COALESCE(la.custom_letter_award_no, la.letter_award_no),
            la.letter_award_date, vendor.company_name, ch.description, pwc.description
        ORDER BY la.letter_award_date DESC
        LIMIT %s
    """

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, (company_id, limit))
            return cursor.fetchall()


def fetch_latest_contract(company_id=1452, letter_award_id=None):
    if not mysql_enabled():
        return None

    id_filter = "AND la.id = %s" if letter_award_id else ""
    params = [company_id]
    if letter_award_id:
        params.append(letter_award_id)

    sql = f"""
        SELECT
            la.id AS letter_award_id,
            COALESCE(la.custom_letter_award_no, la.letter_award_no) AS contract_no,
            la.letter_award_no AS loa_no,
            la.letter_award_date AS awarded_date,
            la.status AS contract_status_id,
            la.vendor_id,
            COALESCE(vendor.company_name, '-') AS contractor,
            la.awarded_contract_amount,
            la.original_total_amount,
            ch.description AS business_unit,
            COALESCE(la.project_work_category_id, t.project_work_category_id) AS project_work_category_id,
            pwc.description AS category_of_work
        FROM max_purchasing.letter_awards la
        JOIN max_purchasing.tenders t ON t.id = la.tender_id
        JOIN max_project.projects p ON p.id = la.project_id
        LEFT JOIN max_base.company_profiles vendor ON vendor.id = la.vendor_id
        LEFT JOIN max_base.company_hierarchies ch ON ch.id = p.company_hierarchy_id
        LEFT JOIN max_project.project_work_categories pwc
            ON pwc.id = COALESCE(la.project_work_category_id, t.project_work_category_id)
        WHERE la.company_id = %s
          AND t.status = 9
          AND la.status NOT IN (4, 5)
          {id_filter}
        ORDER BY la.letter_award_date DESC, la.id DESC
        LIMIT 1
    """

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchone()


def fetch_contract_bq_items(letter_award_id, limit=500):
    if not mysql_enabled():
        return []

    sql = """
        SELECT
            lai.id AS item_id,
            lat.id AS tab_id,
            lat.content AS tab_name,
            lai.item,
            lai.content AS description,
            lai.uom_id,
            cu.uom_code AS unit,
            lai.order_qty,
            COALESCE(NULLIF(lai.tender_rate, 0), NULLIF(COALESCE(lai.material_rate, 0) + COALESCE(lai.service_rate, 0), 0)) AS effective_rate,
            COALESCE(lai.material_rate, 0) AS material_rate,
            COALESCE(lai.service_rate, 0) AS service_rate,
            COALESCE(lai.tender_amount, 0) AS tender_amount,
            COALESCE(lai.material_amount, 0) AS material_amount,
            COALESCE(lai.service_amount, 0) AS service_amount
        FROM max_purchasing.letter_award_tabs lat
        JOIN max_purchasing.letter_award_items lai ON lai.letter_award_tab_id = lat.id
        LEFT JOIN max_base.config_uom cu ON cu.id = lai.uom_id
        WHERE lat.letter_award_id = %s
          AND COALESCE(lai.is_deleted, 0) = 0
          AND COALESCE(lai.is_include, 1) = 1
          AND COALESCE(lai.by_others, 0) = 0
        HAVING effective_rate > 0
        ORDER BY lat.seq, lai.parent_id, lai.seq
        LIMIT %s
    """

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, (letter_award_id, limit))
            return cursor.fetchall()


def fetch_contract_bq_debug(letter_award_id):
    if not mysql_enabled():
        return {"mysql_enabled": False}

    sql = """
        SELECT
            COUNT(DISTINCT lat.id) AS tab_count,
            COUNT(lai.id) AS total_item_rows,
            SUM(CASE WHEN COALESCE(lai.is_deleted, 0) = 0 THEN 1 ELSE 0 END) AS not_deleted_rows,
            SUM(CASE
                WHEN COALESCE(lai.is_deleted, 0) = 0
                 AND COALESCE(lai.is_include, 1) = 1
                 AND COALESCE(lai.by_others, 0) = 0
                THEN 1 ELSE 0
            END) AS included_rows,
            SUM(CASE
                WHEN COALESCE(lai.is_deleted, 0) = 0
                 AND COALESCE(lai.is_include, 1) = 1
                 AND COALESCE(lai.by_others, 0) = 0
                 AND COALESCE(lai.order_qty, 0) > 0
                THEN 1 ELSE 0
            END) AS included_with_qty_rows,
            SUM(CASE
                WHEN COALESCE(lai.is_deleted, 0) = 0
                 AND COALESCE(lai.is_include, 1) = 1
                 AND COALESCE(lai.by_others, 0) = 0
                 AND COALESCE(NULLIF(lai.tender_rate, 0), NULLIF(COALESCE(lai.material_rate, 0) + COALESCE(lai.service_rate, 0), 0), 0) > 0
                THEN 1 ELSE 0
            END) AS included_with_effective_rate_rows,
            SUM(CASE
                WHEN COALESCE(lai.is_deleted, 0) = 0
                 AND COALESCE(lai.is_include, 1) = 1
                 AND COALESCE(lai.by_others, 0) = 0
                 AND COALESCE(lai.order_qty, 0) > 0
                 AND COALESCE(NULLIF(lai.tender_rate, 0), NULLIF(COALESCE(lai.material_rate, 0) + COALESCE(lai.service_rate, 0), 0), 0) > 0
                THEN 1 ELSE 0
            END) AS included_with_qty_and_effective_rate_rows
        FROM max_purchasing.letter_award_tabs lat
        LEFT JOIN max_purchasing.letter_award_items lai ON lai.letter_award_tab_id = lat.id
        WHERE lat.letter_award_id = %s
    """

    sample_sql = """
        SELECT
            lat.id AS tab_id,
            lat.content AS tab_name,
            lai.id AS item_id,
            lai.item,
            LEFT(lai.content, 140) AS description,
            lai.order_qty,
            lai.tender_rate,
            lai.material_rate,
            lai.service_rate,
            COALESCE(NULLIF(lai.tender_rate, 0), NULLIF(COALESCE(lai.material_rate, 0) + COALESCE(lai.service_rate, 0), 0)) AS effective_rate,
            lai.is_deleted,
            lai.is_include,
            lai.by_others
        FROM max_purchasing.letter_award_tabs lat
        LEFT JOIN max_purchasing.letter_award_items lai ON lai.letter_award_tab_id = lat.id
        WHERE lat.letter_award_id = %s
        ORDER BY lat.seq, lai.parent_id, lai.seq
        LIMIT 20
    """

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, (letter_award_id,))
            counts = cursor.fetchone() or {}
            cursor.execute(sample_sql, (letter_award_id,))
            samples = cursor.fetchall()
            return {
                "mysql_enabled": True,
                "letter_award_id": letter_award_id,
                "counts": counts,
                "samples": samples,
            }


def fetch_item_benchmark(company_id, item, description, uom_id, project_work_category_id, exclude_letter_award_id):
    if not mysql_enabled():
        return None

    token = _benchmark_token(item, description)
    like_token = f"%{token}%"
    sql = """
        SELECT
            COUNT(*) AS records_found,
            AVG(rate) AS avg_rate,
            MIN(rate) AS min_rate,
            MAX(rate) AS max_rate
        FROM (
            SELECT
                COALESCE(NULLIF(lai.tender_rate, 0), NULLIF(COALESCE(lai.material_rate, 0) + COALESCE(lai.service_rate, 0), 0)) AS rate
            FROM max_purchasing.letter_awards la
            JOIN max_purchasing.tenders t ON t.id = la.tender_id
            JOIN max_purchasing.letter_award_tabs lat ON lat.letter_award_id = la.id
            JOIN max_purchasing.letter_award_items lai ON lai.letter_award_tab_id = lat.id
            WHERE la.company_id = %s
              AND la.id <> %s
              AND t.status = 9
              AND la.status NOT IN (4, 5)
              AND (%s IS NULL OR COALESCE(la.project_work_category_id, t.project_work_category_id) = %s)
              AND COALESCE(lai.is_deleted, 0) = 0
              AND COALESCE(lai.is_include, 1) = 1
              AND COALESCE(lai.by_others, 0) = 0
              AND (%s IS NULL OR lai.uom_id = %s)
              AND (lai.item LIKE %s OR lai.content LIKE %s)
        ) rates
        WHERE rate > 0
    """

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql,
                (
                    company_id,
                    exclude_letter_award_id,
                    project_work_category_id,
                    project_work_category_id,
                    uom_id,
                    uom_id,
                    like_token,
                    like_token,
                ),
            )
            return cursor.fetchone()


def fetch_similar_project_rates(company_id, item, description, uom_id, project_work_category_id, exclude_letter_award_id, limit=3):
    if not mysql_enabled():
        return []

    token = _benchmark_token(item, description)
    like_token = f"%{token}%"
    sql = """
        SELECT
            p.project_name AS project,
            COALESCE(vendor.company_name, '-') AS contractor,
            COALESCE(NULLIF(lai.tender_rate, 0), NULLIF(COALESCE(lai.material_rate, 0) + COALESCE(lai.service_rate, 0), 0)) AS rate
        FROM max_purchasing.letter_awards la
        JOIN max_purchasing.tenders t ON t.id = la.tender_id
        JOIN max_project.projects p ON p.id = la.project_id
        LEFT JOIN max_base.company_profiles vendor ON vendor.id = la.vendor_id
        JOIN max_purchasing.letter_award_tabs lat ON lat.letter_award_id = la.id
        JOIN max_purchasing.letter_award_items lai ON lai.letter_award_tab_id = lat.id
        WHERE la.company_id = %s
          AND la.id <> %s
          AND t.status = 9
          AND la.status NOT IN (4, 5)
          AND (%s IS NULL OR COALESCE(la.project_work_category_id, t.project_work_category_id) = %s)
          AND COALESCE(lai.is_deleted, 0) = 0
          AND COALESCE(lai.is_include, 1) = 1
          AND COALESCE(lai.by_others, 0) = 0
          AND (%s IS NULL OR lai.uom_id = %s)
          AND (lai.item LIKE %s OR lai.content LIKE %s)
        HAVING rate > 0
        ORDER BY ABS(rate - (
            SELECT AVG(rate2) FROM (
                SELECT COALESCE(NULLIF(lai2.tender_rate, 0), NULLIF(COALESCE(lai2.material_rate, 0) + COALESCE(lai2.service_rate, 0), 0)) AS rate2
                FROM max_purchasing.letter_awards la2
                JOIN max_purchasing.tenders t2 ON t2.id = la2.tender_id
                JOIN max_purchasing.letter_award_tabs lat2 ON lat2.letter_award_id = la2.id
                JOIN max_purchasing.letter_award_items lai2 ON lai2.letter_award_tab_id = lat2.id
                WHERE la2.company_id = %s
                  AND la2.id <> %s
                  AND t2.status = 9
                  AND la2.status NOT IN (4, 5)
                  AND (%s IS NULL OR COALESCE(la2.project_work_category_id, t2.project_work_category_id) = %s)
                  AND (%s IS NULL OR lai2.uom_id = %s)
                  AND (lai2.item LIKE %s OR lai2.content LIKE %s)
            ) avg_rates WHERE rate2 > 0
        ))
        LIMIT %s
    """

    params = (
        company_id,
        exclude_letter_award_id,
        project_work_category_id,
        project_work_category_id,
        uom_id,
        uom_id,
        like_token,
        like_token,
        company_id,
        exclude_letter_award_id,
        project_work_category_id,
        project_work_category_id,
        uom_id,
        uom_id,
        like_token,
        like_token,
        limit,
    )

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()


def _benchmark_token(item, description):
    text = " ".join(part for part in [str(item or ""), str(description or "")] if part).strip()
    words = [
        word.strip(".,;:()[]{}").lower()
        for word in text.split()
        if len(word.strip(".,;:()[]{}")) >= 5
    ]
    if not words:
        return (text[:24] or "").strip()
    return " ".join(words[:2])


def fetch_trade_code_library(search: str = "", review_status_filter: str = "") -> list:
    """Grouped summary of all trade codes from ai_bq_trade_code_suggestions."""
    if not mysql_enabled():
        return []

    params: list = []
    where_parts: list[str] = []

    if search:
        where_parts.append("(suggested_trade_code LIKE %s OR suggested_trade_name LIKE %s)")
        params.extend([f"%{search}%", f"%{search}%"])

    if review_status_filter:
        where_parts.append("review_status = %s")
        params.append(review_status_filter)

    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    sql = f"""
        SELECT
            COALESCE(NULLIF(suggested_trade_code, ''), 'UNCLASSIFIED') AS trade_code,
            MAX(COALESCE(suggested_trade_name, ''))                    AS trade_name,
            COUNT(*)                                                   AS item_count,
            COUNT(DISTINCT contract_no)                                AS contract_count,
            AVG(CASE WHEN unit_rate > 0 THEN unit_rate END)           AS avg_rate,
            MIN(CASE WHEN unit_rate > 0 THEN unit_rate END)           AS min_rate,
            MAX(CASE WHEN unit_rate > 0 THEN unit_rate END)           AS max_rate,
            COUNT(DISTINCT CASE WHEN unit_rate > 0 THEN ROUND(unit_rate, 2) END) AS rate_count,
            AVG(confidence)                                            AS avg_confidence,
            SUM(CASE WHEN review_status = 'approved'  THEN 1 ELSE 0 END) AS approved_count,
            SUM(CASE WHEN review_status = 'rejected'  THEN 1 ELSE 0 END) AS rejected_count,
            SUM(CASE WHEN review_status IN ('ai_error','pending','needs_rerun')
                          OR review_status IS NULL
                          OR suggested_trade_code IS NULL
                          OR suggested_trade_code = ''
                     THEN 1 ELSE 0 END)                                AS problem_count
        FROM max_purchasing.ai_bq_trade_code_suggestions
        {where}
        GROUP BY COALESCE(NULLIF(suggested_trade_code, ''), 'UNCLASSIFIED')
        ORDER BY item_count DESC
    """

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()


def fetch_trade_code_stats() -> dict:
    """Overall stats for the trade code library."""
    if not mysql_enabled():
        return {}

    sql = """
        SELECT
            COUNT(*)                                                     AS total_items,
            COUNT(DISTINCT COALESCE(NULLIF(suggested_trade_code,''),'UNCLASSIFIED')) AS total_codes,
            COUNT(DISTINCT contract_no)                                  AS total_contracts,
            SUM(CASE WHEN review_status = 'approved'  THEN 1 ELSE 0 END) AS approved,
            SUM(CASE WHEN review_status = 'rejected'  THEN 1 ELSE 0 END) AS rejected,
            SUM(CASE WHEN review_status = 'ai_suggested' THEN 1 ELSE 0 END) AS ai_suggested,
            SUM(CASE WHEN review_status IN ('ai_error','pending','needs_rerun')
                          OR review_status IS NULL
                          OR suggested_trade_code IS NULL
                          OR suggested_trade_code = ''
                     THEN 1 ELSE 0 END)                                  AS problems,
            ROUND(AVG(confidence) * 100, 1)                             AS avg_confidence_pct
        FROM max_purchasing.ai_bq_trade_code_suggestions
    """

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            return cursor.fetchone() or {}


def fetch_trade_code_items(trade_code: str, limit: int = 1000) -> list:
    """All BQ suggestion rows for a specific trade code."""
    if not mysql_enabled():
        return []

    if trade_code == "UNCLASSIFIED":
        where = "WHERE (suggested_trade_code IS NULL OR suggested_trade_code = '')"
        params: list = []
    else:
        where = "WHERE suggested_trade_code = %s"
        params = [trade_code]

    sql = f"""
        SELECT
            id,
            contract_no,
            project_name,
            project_shortname,
            contractor_name,
            business_unit,
            awarded_date,
            tab_name,
            bq_item_no,
            item_ref_no,
            parent_header_3,
            parent_header_2,
            parent_header_1,
            bq_item_description,
            full_bq_description,
            uom_code,
            quantity,
            unit_rate,
            tender_amount,
            confidence,
            review_status,
            reasoning,
            suggested_trade_code,
            suggested_trade_name,
            ai_error
        FROM max_purchasing.ai_bq_trade_code_suggestions
        {where}
        ORDER BY contract_no, parent_header_3, parent_header_2, parent_header_1, item_ref_no
        LIMIT %s
    """
    params.append(limit)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()


def fetch_business_units() -> list:
    """Distinct business units from ai_bq_trade_code_suggestions."""
    if not mysql_enabled():
        return []
    sql = """
        SELECT DISTINCT business_unit
        FROM max_purchasing.ai_bq_trade_code_suggestions
        WHERE business_unit IS NOT NULL AND business_unit != ''
        ORDER BY business_unit
    """
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            return [r["business_unit"] for r in cursor.fetchall()]


def fetch_rate_analysis(trade_code: str, business_unit: str = None) -> dict:
    """Historical rate stats, year trend, and recent contracts for a trade code."""
    if not mysql_enabled() or not trade_code:
        return {"stats": {}, "by_year": [], "recent": []}

    base_params: list = [trade_code]
    bu_filter = ""
    if business_unit:
        bu_filter = "AND business_unit = %s"
        base_params = [trade_code, business_unit]

    stats_sql = f"""
        SELECT
            COUNT(*)                                                               AS item_count,
            COUNT(DISTINCT contract_no)                                            AS contract_count,
            COUNT(DISTINCT business_unit)                                          AS bu_count,
            ROUND(AVG(unit_rate), 2)                                               AS avg_rate,
            ROUND(MIN(unit_rate), 2)                                               AS min_rate,
            ROUND(MAX(unit_rate), 2)                                               AS max_rate,
            ROUND(AVG(CASE WHEN awarded_date >= DATE_SUB(NOW(), INTERVAL 2 YEAR)
                           THEN unit_rate END), 2)                                 AS recent_2yr_avg,
            COUNT(CASE WHEN awarded_date >= DATE_SUB(NOW(), INTERVAL 2 YEAR)
                       THEN 1 END)                                                 AS recent_2yr_count,
            ROUND(AVG(CASE WHEN awarded_date >= DATE_SUB(NOW(), INTERVAL 5 YEAR)
                           THEN unit_rate END), 2)                                 AS recent_5yr_avg,
            COUNT(CASE WHEN awarded_date >= DATE_SUB(NOW(), INTERVAL 5 YEAR)
                       THEN 1 END)                                                 AS recent_5yr_count
        FROM max_purchasing.ai_bq_trade_code_suggestions
        WHERE suggested_trade_code = %s
          AND unit_rate > 0
          AND (review_status IS NULL OR review_status != 'rejected')
          {bu_filter}
    """

    trend_sql = f"""
        SELECT
            YEAR(awarded_date)        AS yr,
            COUNT(*)                  AS cnt,
            ROUND(AVG(unit_rate), 2)  AS avg_rate,
            ROUND(MIN(unit_rate), 2)  AS min_rate,
            ROUND(MAX(unit_rate), 2)  AS max_rate
        FROM max_purchasing.ai_bq_trade_code_suggestions
        WHERE suggested_trade_code = %s
          AND unit_rate > 0
          AND awarded_date IS NOT NULL
          AND (review_status IS NULL OR review_status != 'rejected')
          {bu_filter}
        GROUP BY YEAR(awarded_date)
        ORDER BY yr DESC
        LIMIT 6
    """

    recent_sql = f"""
        SELECT
            MIN(id)                    AS id,
            contract_no,
            MIN(contractor_name)       AS contractor_name,
            MIN(business_unit)         AS business_unit,
            MIN(awarded_date)          AS awarded_date,
            MIN(parent_header_3)       AS parent_header_3,
            MIN(parent_header_2)       AS parent_header_2,
            MIN(parent_header_1)       AS parent_header_1,
            MIN(bq_item_description)   AS bq_item_description,
            MIN(uom_code)              AS uom_code,
            COUNT(*)                   AS item_count,
            MIN(quantity)              AS quantity,
            ROUND(unit_rate, 2)        AS unit_rate
        FROM max_purchasing.ai_bq_trade_code_suggestions
        WHERE suggested_trade_code = %s
          AND unit_rate > 0
          AND (review_status IS NULL OR review_status != 'rejected')
          {bu_filter}
        GROUP BY contract_no, ROUND(unit_rate, 2)
        ORDER BY MIN(awarded_date) DESC, unit_rate DESC
        LIMIT 10
    """

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(stats_sql, base_params)
            stats = cursor.fetchone() or {}
            cursor.execute(trend_sql, base_params)
            by_year = cursor.fetchall()
            cursor.execute(recent_sql, base_params)
            recent = cursor.fetchall()

    return {"stats": stats, "by_year": by_year, "recent": recent}


def fetch_contract_rate_items(trade_code: str, contract_no: str, unit_rate: float) -> list:
    """All BQ items for a specific contract + trade code + unit rate combination."""
    if not mysql_enabled():
        return []
    sql = """
        SELECT
            id,
            parent_header_3,
            parent_header_2,
            parent_header_1,
            bq_item_description,
            uom_code,
            quantity,
            ROUND(unit_rate, 2) AS unit_rate,
            contractor_name,
            business_unit,
            awarded_date
        FROM max_purchasing.ai_bq_trade_code_suggestions
        WHERE suggested_trade_code = %s
          AND contract_no = %s
          AND ROUND(unit_rate, 2) = %s
          AND (review_status IS NULL OR review_status != 'rejected')
        ORDER BY parent_header_3, parent_header_2, parent_header_1, id
    """
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, [trade_code, contract_no, round(unit_rate, 2)])
            return cursor.fetchall()


def update_suggestion_trade_code(
    row_id: int,
    trade_code: str,
    trade_name: str,
    review_status: str = "approved",
) -> bool:
    """Manually fix / approve / reject a trade code suggestion."""
    if not mysql_enabled():
        return False

    sql = """
        UPDATE max_purchasing.ai_bq_trade_code_suggestions
        SET suggested_trade_code = %s,
            suggested_trade_name = %s,
            review_status        = %s,
            ai_processed_at      = NOW()
        WHERE id = %s
    """

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, (trade_code, trade_name, review_status, row_id))
            return cursor.rowcount > 0
