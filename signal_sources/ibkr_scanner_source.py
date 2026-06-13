from ib_insync import IB, ScannerSubscription

from config import (
    IBKR_HOST,
    IBKR_PORT,
    IBKR_SCANNER_CLIENT_ID,
    IBKR_SCANNER_LIMIT_PER_CODE,
    IBKR_SCANNER_LOCATION_CODE,
    IBKR_SCANNER_SCAN_CODES,
)
from .common import SignalSourceCandidate, SourceRunResult, clean_ticker


SCAN_CODE_REASONS = {
    "HOT_BY_VOLUME": "IBKR scanner: unusual volume",
    "TOP_PERC_GAIN": "IBKR scanner: top percentage gainer",
    "TOP_PERC_LOSE": "IBKR scanner: top percentage decliner",
    "MOST_ACTIVE": "IBKR scanner: most active",
}


def parse_scan_codes(raw):
    return [item.strip().upper() for item in str(raw or "").replace(";", ",").split(",") if item.strip()]


def connect_ibkr_readonly():
    ib = IB()
    ib.connect(IBKR_HOST, IBKR_PORT, clientId=IBKR_SCANNER_CLIENT_ID, timeout=8, readonly=True)
    return ib


def row_symbol(row):
    details = getattr(row, "contractDetails", None)
    contract = getattr(details, "contract", None)
    return clean_ticker(getattr(contract, "symbol", ""))


def run_ibkr_scanner_source(limit=160):
    result = SourceRunResult(source="ibkr_scanner")
    scan_codes = parse_scan_codes(IBKR_SCANNER_SCAN_CODES)
    if not scan_codes:
        result.warnings.append("no IBKR scanner scan codes configured")
        return result.finish()

    ib = None
    seen = set()
    try:
        ib = connect_ibkr_readonly()
        for scan_code in scan_codes:
            subscription = ScannerSubscription(
                instrument="STK",
                locationCode=IBKR_SCANNER_LOCATION_CODE,
                scanCode=scan_code,
            )
            try:
                rows = ib.reqScannerData(subscription, [], [])
            except Exception as exc:
                result.warnings.append(f"{scan_code} failed: {exc}")
                continue

            for row in rows[: max(1, int(IBKR_SCANNER_LIMIT_PER_CODE))]:
                ticker = row_symbol(row)
                if not ticker:
                    continue
                key = (ticker, scan_code)
                if key in seen:
                    continue
                seen.add(key)
                rank = getattr(row, "rank", None)
                try:
                    score = max(20.0, 95.0 - float(rank or 50))
                except (TypeError, ValueError):
                    score = 50.0
                result.candidates.append(
                    SignalSourceCandidate(
                        ticker=ticker,
                        source=result.source,
                        score=round(score, 2),
                        action="WATCH",
                        reason=SCAN_CODE_REASONS.get(scan_code, f"IBKR scanner: {scan_code}"),
                        metadata={
                            "scan_code": scan_code,
                            "rank": rank,
                            "distance": getattr(row, "distance", None),
                            "benchmark": getattr(row, "benchmark", None),
                            "projection": getattr(row, "projection", None),
                            "legs": getattr(row, "legsStr", None),
                        },
                    )
                )
                if len(result.candidates) >= limit:
                    break
            if len(result.candidates) >= limit:
                break
    except Exception as exc:
        result.warnings.append(f"IBKR scanner unavailable: {exc}")
    finally:
        if ib is not None and ib.isConnected():
            ib.disconnect()

    result.metadata.update(
        {
            "scan_codes": scan_codes,
            "location_code": IBKR_SCANNER_LOCATION_CODE,
            "candidate_count": len(result.candidates),
        }
    )
    return result.finish()
