#!/usr/bin/env python3
"""Flask web frontend for Pilot Trip Analyzer.

Run:  python3 trip_analyzer_frontend.py
Then open: http://localhost:5050
"""

import os
import sys
import re
import glob
import calendar

from flask import Flask, request, jsonify, send_file, make_response

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trip_analyzer import (
    parse_llv, load_trips, load_legs, load_layovers,
    score_trip, get_bid_period, parse_days_off,
    min_to_hhmm, MIN_CREDIT_DAY_MIN, BID_PERIOD_DATES,
    WEIGHT_KEYS,
)

app = Flask(__name__)
HERE = os.path.dirname(os.path.abspath(__file__))
CSV_DIR = os.path.join(HERE, 'CSVs')


def find_csv(suffix):
    """Return the first *_{suffix}.csv found in the script directory."""
    matches = glob.glob(os.path.join(CSV_DIR, f'*_{suffix}.csv'))
    # case-insensitive fallback
    if not matches:
        matches = glob.glob(os.path.join(CSV_DIR, f'*_{suffix.upper()}.csv'))
    return matches[0] if matches else None


@app.route('/')
def index():
    resp = make_response(send_file(os.path.join(HERE, 'index.html')))
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/files')
def files():
    """Return the auto-discovered CSV filenames."""
    return jsonify({
        'trips':    os.path.basename(find_csv('trips')    or '') or None,
        'legs':     os.path.basename(find_csv('legs')     or '') or None,
        'layovers': os.path.basename(find_csv('layovers') or '') or None,
    })


@app.route('/analyze', methods=['POST'])
def analyze():
    try:
        tp = find_csv('trips')
        lp = find_csv('legs')
        lv = find_csv('layovers')

        missing = [s for s, p in [('trips', tp), ('legs', lp), ('layovers', lv)] if not p]
        if missing:
            return jsonify({'error': f"Cannot find CSV files for: {', '.join(missing)}. "
                                     f"Place *_trips.csv, *_legs.csv, and *_layovers.csv in {CSV_DIR}"}), 400

        llv_str      = request.form.get('llv', '70:53')
        days_off_str = request.form.get('days_off', '')
        month        = int(request.form.get('month', 4))
        year         = int(request.form.get('year', 2026))
        top_n        = int(request.form.get('top_n', 78))
        bid_period   = request.form.get('bid_period', '').strip()

        trips    = load_trips(tp)
        legs     = load_legs(lp)
        layovers = load_layovers(lv)

        # Resolve bid period (accepts "26apr", "april", "apr", etc.)
        bp_name = calendar.month_name[month].lower()
        if bid_period:
            m = re.match(r'^(\d{2})([a-zA-Z]{3,})$', bid_period)
            if m:
                year    = 2000 + int(m.group(1))
                bp_name = m.group(2).lower()
                month   = next(
                    (i for i in range(1, 13)
                     if calendar.month_abbr[i].lower() == bp_name[:3]),
                    month,
                )
            else:
                bp_name = bid_period.lower()

        if bp_name not in BID_PERIOD_DATES:
            bp_name = next(
                (k for k in BID_PERIOD_DATES if k.startswith(bp_name[:3])),
                bp_name,
            )
        if bp_name not in BID_PERIOD_DATES:
            return jsonify({
                'error': f"Unknown bid period '{bid_period}'. "
                         f"Valid: {', '.join(BID_PERIOD_DATES)}"
            }), 400

        bid_start, bid_end, bid_period_label = get_bid_period(bp_name, year)
        llv_min  = parse_llv(llv_str)
        days_off = parse_days_off(days_off_str, bid_start)

        weights = {key: float(request.form.get(f'w_{key}', 1.0)) for key, _ in WEIGHT_KEYS}

        scored, excluded = [], []
        for trip_number in trips:
            result = score_trip(
                trip_number, trips, legs, layovers,
                llv_min, days_off, month, year, bid_start, bid_end,
                weights=weights,
            )
            if result is None:
                pass
            elif result.get('excluded'):
                excluded.append(result)
            else:
                scored.append(result)

        # TAFB relative score adjustment
        TAFB_MAX_PTS = 20
        tafb_weight = weights.get('tafb', 1.0)
        for tl in range(2, 6):
            grp = [s for s in scored if s['trip_length'] == tl]
            if not grp:
                continue
            lo  = min(s['tafb_min'] for s in grp)
            hi  = max(s['tafb_min'] for s in grp)
            mid = (lo + hi) / 2
            rng = hi - lo
            for s in grp:
                adj = (s['tafb_min'] - mid) / rng * (TAFB_MAX_PTS * 2) if rng else 0.0
                adj = round(adj * tafb_weight, 1)
                s['score'] = round(s['score'] + adj, 1)
                s['_breakdown']['tafb_relative'] = adj

        for rank_i, s in enumerate(sorted(scored, key=lambda x: x['score']), 1):
            s['rank'] = rank_i

        # Attach raw leg/layover data for trip sheet display in the UI
        for s in scored:
            tn = s['trip_number']
            s['_legs'] = list(legs.get(tn, []))
            s['_layovers'] = list(layovers.get(tn, []))
            raw = trips.get(tn, {})
            s['total_pay'] = raw.get('total_pay', '')
            s['total_block'] = raw.get('total_block', '')

        llv_d = max(1, int(llv_min / MIN_CREDIT_DAY_MIN))
        llv_thresholds = {
            n: min_to_hhmm(round(n * llv_min / llv_d))
            for n in range(1, 6)
        }

        return jsonify({
            'ok':               True,
            'scored':           scored,
            'excluded':         excluded,
            'llv':              min_to_hhmm(llv_min),
            'llv_thresholds':   llv_thresholds,
            'bid_period_label': bid_period_label,
            'days_off':         days_off_str,
            'bid_start':        bid_start.isoformat(),
            'total_trips':      len(trips),
            'top_n':            top_n,
            'files': {
                'trips':    os.path.basename(tp),
                'legs':     os.path.basename(lp),
                'layovers': os.path.basename(lv),
            },
        })

    except Exception as exc:
        import traceback
        return jsonify({'error': str(exc), 'traceback': traceback.format_exc()}), 500


if __name__ == '__main__':
    print("\n  ✈  Pilot Trip Analyzer — Web UI")
    print("  Open: http://localhost:5050\n")
    app.run(debug=True, port=5050)
