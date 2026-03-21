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
from flask import Flask, request, jsonify, send_file, make_response, Response
from werkzeug.utils import secure_filename

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trip_analyzer import (
    parse_llv, load_trips, load_legs, load_layovers,
    score_trip, get_bid_period, parse_days_off,
    min_to_hhmm, MIN_CREDIT_DAY_MIN, BID_PERIOD_DATES,
    WEIGHT_KEYS,
)

app = Flask(__name__)
HERE = os.path.dirname(os.path.abspath(__file__))
CSV_DIR = os.environ.get('CSV_DIR', os.path.join(HERE, 'CSVs'))

# ── Basic Auth ────────────────────────────────────────────────────────────
_APP_USER = os.environ.get('APP_USER', '')
_APP_PASS = os.environ.get('APP_PASS', '')

@app.before_request
def check_auth():
    if request.endpoint == 'health':
        return  # health check is always open
    if _APP_USER and _APP_PASS:
        auth = request.authorization
        if not auth or auth.username != _APP_USER or auth.password != _APP_PASS:
            return Response(
                'Authentication required', 401,
                {'WWW-Authenticate': 'Basic realm="Trip Analyzer"'}
            )


def find_csv(suffix):
    """Return the first *_{suffix}.csv found in the script directory."""
    matches = glob.glob(os.path.join(CSV_DIR, f'*_{suffix}.csv'))
    # case-insensitive fallback
    if not matches:
        matches = glob.glob(os.path.join(CSV_DIR, f'*_{suffix.upper()}.csv'))
    return matches[0] if matches else None


@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


@app.route('/upload_csv', methods=['POST'])
def upload_csv():
    files = request.files.getlist('files')
    if len(files) != 3:
        return jsonify(ok=False, error='Send exactly 3 CSV files (trips, legs, layovers)'), 400

    allowed = {'trips', 'legs', 'layovers'}
    # Validate names first before touching the filesystem
    file_map = {}  # suffix -> (file_object, sanitized_name)
    for f in files:
        name = secure_filename(f.filename)
        m = re.match(r'^(.+)_(trips|legs|layovers)\.csv$', name, re.IGNORECASE)
        if not m:
            return jsonify(ok=False,
                           error=f'Bad filename "{name}". Expected PREFIX_trips/legs/layovers.csv'), 400
        suffix = m.group(2).lower()
        file_map[suffix] = (f, name)

    if file_map.keys() != allowed:
        return jsonify(ok=False, error='Need one trips, one legs, and one layovers file'), 400

    os.makedirs(CSV_DIR, exist_ok=True)
    for f, name in file_map.values():
        f.save(os.path.join(CSV_DIR, name.upper()))

    trips_name = file_map['trips'][1]
    bid_period = re.match(r'^(.+)_trips\.csv$', trips_name, re.IGNORECASE).group(1).upper()
    return jsonify(ok=True, bid_period=bid_period)


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
    port = int(os.environ.get('PORT', 5050))
    debug = os.environ.get('FLASK_ENV') != 'production'
    print("\n  ✈  Pilot Trip Analyzer — Web UI")
    print(f"  Open: http://localhost:{port}\n")
    app.run(debug=debug, host='0.0.0.0', port=port)
