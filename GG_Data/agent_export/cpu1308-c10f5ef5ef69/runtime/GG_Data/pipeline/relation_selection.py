"""Exact existing consumer helper bodies; see accompanying Apache LICENSE."""
from .relations import SourceException

def year_span(
    year: int, precision: int, observation_id: str | None = None
) -> tuple[int, int]:
    # S2T 8 / Wikidata Help:Dates#Precision: decades start at x0;
    # historical centuries/millennia start at x01 and end at x00.
    # These bounds are comparison-only; never write them over source dates.
    if precision >= 9:
        return year, year
    if precision not in (6, 7, 8) or year <= 0:
        raise SourceException(
            "Coarse date requires period interpretation",
            observation_id=observation_id,
            year=year,
            precision=precision,
        )
    width = 10 ** (9 - precision)
    offset = 0 if precision == 8 else 1
    start = ((year - offset) // width) * width + offset
    return start, start + width - 1
