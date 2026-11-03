"""Exact existing consumer helper bodies; see accompanying Apache LICENSE."""
import re

class SourceException(ValueError):
    """A business/data exception must be shown, not repaired automatically."""

    def __init__(self, reason: str, **case: object):
        self.case = dict(reason=reason, **case)
        super().__init__(f"{reason}: {case.get('source_record_id', '')}")

def date_parts(value: str) -> tuple[int, ...]:
    match = re.fullmatch(r"([+-]\d+)-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z", value)
    if not match:
        raise SourceException("Unsupported native date format", value=value)
    return tuple(map(int, match.groups()))
