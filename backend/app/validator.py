import re
from datetime import datetime, timezone
from typing import Optional, Tuple

ALLOWED_CATEGORIES = {"PAYMENT", "REFUND", "TRANSFER", "FEE", "SUBSCRIPTION"}
ISO_TIMESTAMP_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")

class RowValidator:
    def __init__(self):
        self.seen_event_ids: set[str] = set()

    def reset(self):
        self.seen_event_ids.clear()

    def validate_row(self, row: dict, row_idx: int) -> Tuple[bool, Optional[str], Optional[str], Optional[int], Optional[str]]:
        """
        Validates a single CSV row.
        Returns:
            (is_valid, error_code, error_message, parsed_amount_minor, normalized_category)
        """
        event_id = (row.get("event_id") or "").strip()
        category = (row.get("category") or "").strip()
        timestamp_str = (row.get("timestamp_utc") or "").strip()
        amount_str = (row.get("amount_minor") or "").strip()

        # 1. Event ID check
        if not event_id:
            return False, "MISSING_EVENT_ID", f"Row {row_idx}: event_id is empty or missing", None, None

        if event_id in self.seen_event_ids:
            return False, "DUPLICATE_EVENT_ID", f"Row {row_idx}: Duplicate event_id '{event_id}'", None, None

        # 2. Category check
        if not category:
            return False, "EMPTY_CATEGORY", f"Row {row_idx}: category is empty", None, None

        if category not in ALLOWED_CATEGORIES:
            return False, "INVALID_CATEGORY", f"Row {row_idx}: Invalid category '{category}'. Allowed: {', '.join(sorted(ALLOWED_CATEGORIES))}", None, None

        # 3. Timestamp check
        if not timestamp_str:
            return False, "EMPTY_TIMESTAMP", f"Row {row_idx}: timestamp_utc is empty", None, None

        if not ISO_TIMESTAMP_REGEX.match(timestamp_str):
            return False, "BAD_TIMESTAMP_FORMAT", f"Row {row_idx}: Invalid ISO 8601 timestamp '{timestamp_str}'", None, None

        try:
            # Check calendar validity (e.g. 2026-02-31 will fail)
            clean_ts = timestamp_str.replace("Z", "+00:00")
            parsed_dt = datetime.fromisoformat(clean_ts)
        except ValueError as ex:
            return False, "INVALID_CALENDAR_DATE", f"Row {row_idx}: Date parsing failed: {ex}", None, None

        # 4. Amount minor check
        if not amount_str:
            return False, "EMPTY_AMOUNT", f"Row {row_idx}: amount_minor is empty", None, None

        if "." in amount_str:
            return False, "FLOAT_AMOUNT", f"Row {row_idx}: amount_minor must be integer minor unit, got float '{amount_str}'", None, None

        try:
            amount_minor = int(amount_str)
            if amount_minor <= 0:
                return False, "NON_POSITIVE_AMOUNT", f"Row {row_idx}: amount_minor must be positive, got {amount_minor}", None, None
        except ValueError:
            return False, "NON_NUMERIC_AMOUNT", f"Row {row_idx}: amount_minor must be numeric digits, got '{amount_str}'", None, None

        # Row is valid
        self.seen_event_ids.add(event_id)
        return True, None, None, amount_minor, category
