#!/usr/bin/env python3
__version__ = "1.2.9"
"""
Pilot Trip Analyzer
===================
Ranks and scores pilot trips from bid package CSV files based on multiple
quality-of-life and efficiency criteria.
===================
CHANGES: changed sit penalty logic and normalized penalty
===================

Usage:
    python3 trip_analyzer.py --llv 70:53 --bid-period 26apr is default for PDF output

    python3 trip_analyzer.py --llv 70:53 [--days-off 1,2,6,7,22-31] [--base-tz MST]

    python3 trip_analyzer.py --llv 70:53 \
        --trips-file /some/other/path/myfile_trips.csv \
        --legs-file /some/other/path/myfile_legs.csv \
        --lay-file /some/other/path/myfile_layovers.csv
        
    # Basic usage with your LLV
    python3 trip_analyzer.py --llv 70:53

    # With days off
    python3 trip_analyzer.py --llv 70:53 --days-off "1,2,6,7,22-31"

    # Detailed score breakdown for a specific trip
    python3 trip_analyzer.py --llv 70:53 --detail 2603

    # Export results to CSV
    python3 trip_analyzer.py --llv 70:53 --export-csv results.csv

    # Export results to TXT
    python3 trip_analyzer.py --llv 70:53 --export-txt results.pdf

    # Show top 30 per group
    python3 trip_analyzer.py --llv 70:53 --top 30

Arguments:
    --llv         Low Line Value in HH:MM format (e.g. 70:53)
    --days-off    Comma-separated days/ranges to exclude (e.g. 1,2,6,7,22-31)
    --base-tz     Base timezone offset from UTC (default: MST = UTC-7)
    --trips-file  Path to trips CSV (default: *_trips.csv)
    --legs-file   Path to legs CSV  (default: *_legs.csv)
    --lay-file    Path to layovers CSV (default: *_layovers.csv)
    --top         Number of top trips to show per length (default: 78)
    --month       Month number for date context (default: 4 = April)
    --year        Year for date context (default: 2026)
"""

import argparse
import csv
import sys
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional
import calendar
import datetime

# ─────────────────────────────────────────────────────────────
# Scoring weight keys and defaults (1.0 = normal, 0 = off, 3.0 = 3×)
# ─────────────────────────────────────────────────────────────

WEIGHT_KEYS = [
    ('early_flying',   'Early Flying Penalty'),
    ('late_flying',    'Late Flying Penalty'),
    ('legs',           'Legs Per Day'),
    ('sit',            'Sit Time'),
    ('weekend',        'Weekend Penalty'),
    ('circadian',      'Circadian / Layover'),
    ('fdp',            'FDP Penalty'),
    ('length',         'Trip Length Bonus'),
    ('llv_threshold',  'LLV Threshold Bonus'),
    ('real_credit',    'Real Credit Bonus'),
    ('tafb',           'TAFB Relative'),
    ('deadhead',       'Dead Head Penalty'),
]

WEIGHT_DEFAULTS = {k: 1.0 for k, _ in WEIGHT_KEYS}


def launch_weight_gui(defaults: dict) -> dict:
    """Open a browser-based weight panel. Returns weights dict or None if cancelled."""
    import threading
    import json
    import webbrowser
    from http.server import HTTPServer, BaseHTTPRequestHandler

    slider_rows = ''
    for key, label in WEIGHT_KEYS:
        val = defaults.get(key, 1.0)
        slider_rows += f"""
        <div class="row">
          <div class="row-header">
            <span class="label">{label}</span>
            <span class="val" id="val_{key}">{val:.2f}x</span>
          </div>
          <input type="range" min="0" max="3" step="0.05" value="{val}"
                 id="{key}" oninput="upd('{key}')">
          <div class="ticks"><span>0</span><span>1x</span><span>2x</span><span>3x</span></div>
        </div>"""

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Scoring Weights</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{ height: 100%; }}
  body {{ font-family: -apple-system, Helvetica, sans-serif; background: #f0f2f5;
          display: flex; justify-content: flex-end; height: 100vh; overflow: hidden; }}
  .panel {{ width: 400px; background: white; display: flex; flex-direction: column;
            height: 100vh; box-shadow: -4px 0 20px rgba(0,0,0,.15); }}
  .header {{ background: #1a2535; color: white; padding: 14px 18px 10px; flex-shrink: 0; }}
  .header-top {{ display: flex; justify-content: space-between; align-items: center;
                 margin-bottom: 4px; }}
  .header h2 {{ font-size: 17px; }}
  .header p {{ font-size: 12px; color: #aac4e8; }}
  .btn-apply-header {{ background: #2a5298; color: white; border: none; border-radius: 6px;
                       padding: 6px 16px; font-size: 13px; font-weight: 700; cursor: pointer; }}
  .btn-apply-header:hover {{ background: #1a3a78; }}
  .sliders {{ flex: 1; overflow-y: auto; padding: 6px 0; min-height: 0; }}
  .row {{ padding: 8px 16px 4px; }}
  .row:nth-child(even) {{ background: #f8f9fb; }}
  .row-header {{ display: flex; justify-content: space-between; align-items: baseline;
                 margin-bottom: 3px; }}
  .label {{ font-size: 13px; font-weight: 600; color: #222; }}
  .val {{ font-family: monospace; font-size: 13px; color: #2a5298; font-weight: 700;
          min-width: 42px; text-align: right; }}
  input[type=range] {{ width: 100%; accent-color: #2a5298; cursor: pointer; }}
  .ticks {{ display: flex; justify-content: space-between; font-size: 10px;
            color: #999; margin-top: 1px; padding: 0 2px; }}
  .footer {{ padding: 12px 16px; border-top: 1px solid #e0e0e0; background: #f8f9fb;
             display: flex; gap: 8px; flex-shrink: 0; }}
  button {{ flex: 1; padding: 9px; border: none; border-radius: 6px;
            font-size: 14px; cursor: pointer; font-weight: 600; }}
  .btn-reset {{ background: #e8ecf2; color: #333; }}
  .btn-reset:hover {{ background: #d8dce6; }}
  .btn-zero {{ background: #fde8e8; color: #b00; }}
  .btn-zero:hover {{ background: #f5c6c6; }}
  #status {{ text-align: center; padding: 8px; font-size: 13px; color: #2a5298;
             display: none; flex-shrink: 0; }}
</style>
</head>
<body>
<div class="panel">
  <div class="header">
    <div class="header-top">
      <h2>&#9878; Scoring Weights</h2>
      <button class="btn-apply-header" onclick="run()">Apply</button>
    </div>
    <p>0 = off &nbsp;&nbsp; 1.0 = normal &nbsp;&nbsp; 3.0 = 3x weight</p>
  </div>
  <div class="sliders">{slider_rows}</div>
  <div id="status">Running analysis...</div>
  <div class="footer">
    <button class="btn-zero" onclick="zeroAll()">Zero All</button>
    <button class="btn-reset" onclick="reset()">Reset to Default</button>
  </div>
</div>
<script>
const defaults = {json.dumps(defaults)};
function upd(key) {{
  const v = parseFloat(document.getElementById(key).value);
  document.getElementById('val_' + key).textContent = v.toFixed(2) + 'x';
}}
function getWeights() {{
  const w = {{}};
  {'; '.join(f'w["{k}"] = parseFloat(document.getElementById("{k}").value)' for k, _ in WEIGHT_KEYS)};
  return w;
}}
function reset() {{
  for (const [k, v] of Object.entries(defaults)) {{
    const el = document.getElementById(k);
    if (el) {{ el.value = v; upd(k); }}
  }}
}}
function zeroAll() {{
  for (const [k] of Object.entries(defaults)) {{
    const el = document.getElementById(k);
    if (el) {{ el.value = 0; upd(k); }}
  }}
}}
function run() {{
  document.getElementById('status').style.display = 'block';
  document.querySelectorAll('button').forEach(b => b.disabled = true);
  fetch('/run', {{method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify(getWeights())}})
  .then(() => window.close());
}}
function cancel() {{
  fetch('/cancel', {{method:'POST'}}).then(() => window.close());
}}
</script>
</body>
</html>"""

    weights_result = [None]
    cancelled = [False]
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(html.encode('utf-8'))

        def do_POST(self):
            if self.path == '/run':
                length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(length))
                weights_result[0] = data
            else:
                cancelled[0] = True
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            done.set()

        def log_message(self, *_):
            pass  # silence HTTP logs

    server = HTTPServer(('127.0.0.1', 0), Handler)
    port = server.server_address[1]

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    url = f'http://127.0.0.1:{port}'
    print(f"  Opening weight panel at {url}")
    webbrowser.open(url)

    done.wait()
    server.shutdown()

    if cancelled[0] or weights_result[0] is None:
        return None
    return weights_result[0]


# ─────────────────────────────────────────────────────────────
# Time helpers  (times stored as decimal H.MM → minutes)
# ─────────────────────────────────────────────────────────────

def hhmm_to_min(s: str) -> int:
    """Convert 'H.MM' or 'HH.MM' decimal time notation to total minutes."""
    if not s or not s.strip():
        return 0
    s = s.strip()
    try:
        if '.' in s:
            parts = s.split('.')
            h = int(parts[0]) if parts[0] else 0
            m = int(parts[1].ljust(2, '0')[:2])
        elif ':' in s:
            parts = s.split(':')
            h = int(parts[0])
            m = int(parts[1]) if len(parts) > 1 else 0
        else:
            h = int(s)
            m = 0
        return h * 60 + m
    except (ValueError, IndexError):
        return 0


def hhcolon_to_min(s: str) -> int:
    """Convert 'HH:MM' colon-separated time to minutes."""
    if not s or not s.strip():
        return 0
    s = s.strip()
    try:
        parts = s.split(':')
        return int(parts[0]) * 60 + int(parts[1])
    except:
        return 0


def min_to_hhmm(total: int) -> str:
    """Convert minutes to 'H:MM' string."""
    h, m = divmod(abs(total), 60)
    return f"{h}:{m:02d}"


def parse_llv(s: str) -> int:
    """Parse LLV like '70:53' into total minutes."""
    parts = s.split(':')
    return int(parts[0]) * 60 + int(parts[1])


def clock_to_min(s: str) -> int:
    """Convert 4-digit clock time '0600' to minutes since midnight."""
    s = s.strip().zfill(4)
    return int(s[:2]) * 60 + int(s[2:])


def _next_month(d: datetime.date) -> datetime.date:
    """Return the first day of the month following d."""
    if d.month == 12:
        return datetime.date(d.year + 1, 1, 1)
    return datetime.date(d.year, d.month + 1, 1)


MIN_CREDIT_DAY_MIN = 315  # 5:15 per day — minimum credit threshold for LLV efficiency

# Bid period start/end dates: (start_month, start_day, end_month, end_day)
BID_PERIOD_DATES = {
    'january':   (1,  1,  1, 30),
    'february':  (1, 31,  3,  1),
    'march':     (3,  2,  3, 31),
    'april':     (4,  1,  5,  1),
    'may':       (5,  2,  6,  1),
    'june':      (6,  2,  7,  1),
    'july':      (7,  2,  7, 31),
    'august':    (8,  1,  8, 30),
    'september': (8, 31,  9, 30),
    'october':   (10, 1, 10, 31),
    'november':  (11, 1, 11, 30),
    'december':  (12, 1, 12, 31),
}

def get_bid_period(name: str, year: int) -> tuple:
    """Return (start_date, end_date, label) for a named bid period."""
    sm, sd, em, ed = BID_PERIOD_DATES[name.lower()]
    start = datetime.date(year, sm, sd)
    end   = datetime.date(year, em, ed)
    label = f"{start.strftime('%b %d')} – {end.strftime('%b %d, %Y')}"
    return start, end, label


def parse_days_off(s: str, bid_start: datetime.date) -> set:
    """Parse '1,2,6,7,22-31' into a set of datetime.date objects.

    Day numbers are 1-based relative to the bid period start date, so day 1
    = bid_start, day 31 = bid_start + 30 days.  This correctly handles bid
    periods that span two months (e.g. April: Apr 1 – May 1).
    """
    dates = set()
    if not s:
        return dates
    cur_month = bid_start
    last_num = -1
    for part in s.split(','):
        part = part.strip()
        if '-' in part:
            lo_s, hi_s = part.split('-', 1)
            lo, hi = int(lo_s.strip()), int(hi_s.strip())
            if last_num >= 0 and lo <= last_num:
                cur_month = _next_month(cur_month)
            for d in range(lo, hi + 1):
                try:
                    dates.add(datetime.date(cur_month.year, cur_month.month, d))
                except ValueError:
                    pass
            last_num = hi
        else:
            day_num = int(part)
            if last_num >= 0 and day_num <= last_num:
                cur_month = _next_month(cur_month)
            try:
                dates.add(datetime.date(cur_month.year, cur_month.month, day_num))
            except ValueError:
                pass
            last_num = day_num
    return dates


# ─────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────

def load_trips(path: str) -> dict:
    """Return dict keyed by trip_number."""
    with open(path, newline='') as f:
        return {r['trip_number']: r for r in csv.DictReader(f)}


def load_legs(path: str) -> dict:
    """Return dict keyed by trip_number → list of leg rows."""
    d = defaultdict(list)
    with open(path, newline='') as f:
        for row in csv.DictReader(f):
            row['dep_city'] = row['dep_city'].lstrip('*')
            row['arr_city'] = row['arr_city'].lstrip('*')
            d[row['trip_number']].append(row)
    return d


def load_layovers(path: str) -> dict:
    """Return dict keyed by trip_number → list of layover rows."""
    d = defaultdict(list)
    with open(path, newline='') as f:
        for row in csv.DictReader(f):
            d[row['trip_number']].append(row)
    return d


# ─────────────────────────────────────────────────────────────
# Trip structure helpers
# ─────────────────────────────────────────────────────────────

DAY_LETTERS = list('ABCDEFGHIJ')
EASTERN_AIRPORTS = {
    # Northeast
    'BOS','MHT','PVD','BDL','ALB','SYR','ROC','BUF',
    'JFK','LGA','EWR','TEB','ISP','MMU',
    'PHL','PIT',
    # Mid-Atlantic / Southeast
    'DCA','IAD','BWI','RIC','CHO','ORF',
    'CLT','GSO','RDU','ILM','MYR','CHS','GSP',
    'ATL','SAV','JAX',
    'MCO','TPA','SRQ','RSW','PBI','FLL','MIA','DAB',
    # Eastern Ohio / Midwest
    'DTW','CLE','CVG','CMH','DAY','TOL','GRR',
    'IND','SDF','LEX',
    # Appalachia
    'CRW','HTS','CHA','TYS',
    # Canada (Eastern)
    'YYZ',
    # Caribbean (UTC-5, no DST — same offset as Eastern Standard)
    'PLS','KIN','MBJ','CUN',
}
ATLANTIC_AIRPORTS = {
    # Caribbean (UTC-4, no DST — Atlantic Standard Time)
    'GND','AUA','PUJ','SJU',
}
ALASKA_AIRPORTS = {
    # Alaska (UTC-9 standard / UTC-8 DST)
    'ANC','FAI','JNU','KTN','SIT','BET','OME','OTZ','ADQ','CDV','YAK',
}
CENTRAL_AIRPORTS = {
    # Great Lakes / Midwest
    'ORD','MDW','MKE','MSN','GRB','RST','MSP','DSM','OMA',
    # South-Central
    'STL','MCI','ICT',
    'BNA','MEM',
    'MSY','BTR','SHV',
    'LIT','XNA',
    'TUL','OKC',
    'AUS','DAL','SAT',
    # Alabama / Gulf Coast / FL Panhandle
    'BHM','HSV','MOB','PNS','VPS','ECP',
    # Mexico Central
    'PVR',
}
MOUNTAIN_AIRPORTS = {
    'DEN','SLC','BOI','ABQ','ELP',
    'BZN','GJT','MTJ','BIL','GTF','MSO','FCA','JAC',
    'PVU','YYC',
    # Arizona (UTC-7 year-round, no DST)
    'PHX','TUS',
    # Mexico Mountain
    'SJD',
}

# Base is SLC (Utah) = Mountain Time = UTC-7
BASE_TZ_OFFSET = -7


def get_tz_offset(airport: str) -> int:
    """Return UTC offset for an airport (standard time)."""
    airport = airport.lstrip('*')
    if airport in ATLANTIC_AIRPORTS:
        return -4
    elif airport in EASTERN_AIRPORTS:
        return -5
    elif airport in CENTRAL_AIRPORTS:
        return -6
    elif airport in MOUNTAIN_AIRPORTS:
        return -7
    elif airport in ALASKA_AIRPORTS:
        return -9
    else:
        # Default to Pacific for western airports
        return -8



def to_mountain_time(clock_min: int, airport: str) -> int:
    """Convert local clock minutes at airport to Mountain Time minutes."""
    tz = get_tz_offset(airport)
    diff = tz - BASE_TZ_OFFSET  # e.g., Eastern(-5) - Mountain(-7) = +2
    return clock_min - diff * 60


def get_trip_length(trip_number: str, layovers: dict, legs: dict = None) -> int:
    """Number of calendar days in a trip, based on the max leg day index + 1.
    Falls back to layover count + 1 if no leg data is available."""
    if legs is not None:
        leg_rows = legs.get(trip_number, [])
        letters = [r['trip_day'] for r in leg_rows if r['trip_day'] in DAY_LETTERS]
        if letters:
            return max(DAY_LETTERS.index(l) for l in letters) + 1
    lays = layovers.get(trip_number, [])
    if not lays:
        return 1
    letters = [r['trip_day'] for r in lays if r['trip_day'] in DAY_LETTERS]
    return max(DAY_LETTERS.index(l) for l in letters) + 2  # layover days + return day


def get_trip_day_legs(trip_number: str, legs: dict) -> dict:
    """Return dict of trip_day → sorted list of leg rows."""
    day_legs = defaultdict(list)
    for leg in legs.get(trip_number, []):
        day_legs[leg['trip_day']].append(leg)
    for k in day_legs:
        day_legs[k].sort(key=lambda x: int(x['leg_seq']))
    return day_legs


def is_red_eye(legs_for_day: list) -> bool:
    """Return True if any leg overlaps 02:00-04:00 base (Mountain) time, or if the
    last leg of the day lands after the late-arrival threshold for its timezone
    (Eastern ≥22:00, Central ≥23:00, Pacific ≥23:00 local — all ~midnight MT)."""
    RED_START = 2 * 60   # 120 min = 02:00
    RED_END   = 4 * 60   # 240 min = 04:00
    for leg in legs_for_day:
        dep_mtn = to_mountain_time(clock_to_min(leg['dep_time']), leg['dep_city'])
        arr_mtn = to_mountain_time(clock_to_min(leg['arr_time']), leg['arr_city'])
        if arr_mtn >= dep_mtn:
            # Normal flight (no midnight crossing): overlaps 2-4am if ranges intersect
            if dep_mtn < RED_END and arr_mtn >= RED_START:
                return leg
        elif dep_mtn - arr_mtn > 360:
            # True overnight (gap > 6 hrs rules out timezone-conversion artifacts):
            # split into [dep_mtn, 1440) and [0, arr_mtn] and check each segment
            if dep_mtn < RED_END:    # segment 1 overlaps 2-4am window
                return leg
            if arr_mtn >= RED_START:  # segment 2 overlaps 2-4am window
                return leg
        else:
            # Small apparent gap is a TZ-artifact (not truly overnight); treat as normal
            if dep_mtn < RED_END and arr_mtn >= RED_START:
                return leg

    # Late-arrival check: last leg arrives after 23:59 Mountain Time (body-clock threshold)
    # Uses MT conversion so Eastern/Central arrivals are evaluated correctly —
    # e.g. IND arr 2255 ET = 20:55 MT (fine), not flagged by the raw 22:00 local check.
    # Also catches midnight-crossing legs (e.g. dep 2300 arr 0100): if arr_mtn < dep_mtn
    # the flight crossed midnight, so the true arrival is arr_mtn + 1440 — always >= 1439.
    if legs_for_day:
        last_leg = legs_for_day[-1]
        dep_mtn = to_mountain_time(clock_to_min(last_leg['dep_time']), last_leg['dep_city'])
        arr_mtn = to_mountain_time(clock_to_min(last_leg['arr_time']), last_leg['arr_city'])
        effective_arr_mtn = arr_mtn + 1440 if arr_mtn < dep_mtn else arr_mtn
        if effective_arr_mtn >= 23 * 60 + 59:   # 23:59 MT = midnight threshold — eliminate if landing at or past midnight
            return last_leg

    return None


def parse_operates_on(s: str, bid_start: datetime.date) -> list:
    """Parse operates_on field like '01' or '03,06' or '29,01' (wraps to next month)
    into a list of datetime.date objects. Numbers are calendar day-of-month; when a
    number is <= the previous, the month advances (e.g. '29,01' -> Apr 29, May 01)."""
    dates = []
    if not s or not s.strip():
        return dates
    cur_month = bid_start
    last_num = -1
    for part in s.split(','):
        part = part.strip()
        if part:
            try:
                day_num = int(part)
                if last_num >= 0 and day_num <= last_num:
                    cur_month = _next_month(cur_month)
                try:
                    dates.append(datetime.date(cur_month.year, cur_month.month, day_num))
                    last_num = day_num
                except ValueError:
                    pass
            except ValueError:
                pass
    return dates


def parse_effective_dates(s: str, except_s: str, month: int, year: int) -> list:
    """Return list of (month, day) tuples this trip operates."""
    # Format: 'APR01 ONLY' or 'APR03-APR. 06' or 'APR14-APR. 22'
    month_map = {'JAN':1,'FEB':2,'MAR':3,'APR':4,'MAY':5,'JUN':6,
                 'JUL':7,'AUG':8,'SEP':9,'OCT':10,'NOV':11,'DEC':12}
    
    def parse_date(ds):
        ds = ds.strip().replace('.','').replace(' ','')
        for mname, mn in month_map.items():
            if ds.startswith(mname):
                rest = ds[len(mname):]
                if not rest:
                    return None
                try:
                    day = int(rest)
                except ValueError:
                    return None
                return (mn, day)
        return None
    
    s = s.strip()
    except_days = set()
    if except_s and except_s.strip():
        for part in except_s.replace('APR','APR ').split():
            if part.strip():
                parts2 = except_s.strip().split()
                for p in parts2:
                    d = parse_date(p)
                    if d:
                        except_days.add(d)
        except_days = set()
        for part in except_s.strip().split():
            d = parse_date(part)
            if d:
                except_days.add(d)
    
    dates = []
    if 'ONLY' in s:
        d = parse_date(s.replace('ONLY','').strip())
        if d:
            dates = [d]
    elif '-' in s:
        parts = s.split('-')
        if len(parts) == 2:
            start = parse_date(parts[0].strip())
            end = parse_date(parts[1].strip())
            if start and end:
                sm, sd = start
                em, ed = end
                # Generate date range
                cur = datetime.date(year, sm, sd)
                end_dt = datetime.date(year, em, ed)
                while cur <= end_dt:
                    dates.append((cur.month, cur.day))
                    cur += datetime.timedelta(days=1)
    
    dates = [(m, d) for (m, d) in dates if (m, d) not in except_days]
    return dates


def days_touch_weekend(trip_row: dict, layovers_for_trip: list, trip_length: int, month: int, year: int,
                       bid_start: datetime.date = None) -> tuple:
    """Return (has_weekday, touches_weekend) booleans.

    Expands each start date across all trip_length days so a 2-day trip
    starting Friday correctly registers as touching Saturday, etc.
    Supports both the 'operates_on' field (day numbers relative to bid_start)
    and the legacy 'effective_dates'/'days_of_week' fields.
    """
    dates = []
    if 'operates_on' in trip_row and bid_start:
        dates = parse_operates_on(trip_row['operates_on'], bid_start)
    elif 'effective_dates' in trip_row:
        date_tuples = parse_effective_dates(trip_row['effective_dates'], trip_row.get('except_dates', ''), month, year)
        dates = [datetime.date(year, m, d) for (m, d) in date_tuples]

    if not dates:
        # Fall back to days_of_week field — these are START days of the trip
        dow_map = {'MO': 0, 'TU': 1, 'WE': 2, 'TH': 3, 'FR': 4, 'SA': 5, 'SU': 6}
        dow_str = trip_row.get('days_of_week', '')
        start_days = [v for k, v in dow_map.items() if k in dow_str]
        if not start_days:
            return True, False
        has_weekday = has_weekend = False
        for sd in start_days:
            for offset in range(trip_length):
                wd = (sd + offset) % 7
                if wd < 5:
                    has_weekday = True
                else:
                    has_weekend = True
        return has_weekday, has_weekend

    has_weekday = has_weekend = False
    for start_dt in dates:
        try:
            for offset in range(trip_length):
                wd = (start_dt + datetime.timedelta(days=offset)).weekday()  # 0=Mon, 6=Sun
                if wd < 5:
                    has_weekday = True
                else:
                    has_weekend = True
        except:
            pass
    return has_weekday, has_weekend


# ─────────────────────────────────────────────────────────────
# Scoring engine
# ─────────────────────────────────────────────────────────────

def score_trip(trip_number: str, trips: dict, legs: dict, layovers: dict,
               llv_min: int, days_off: set, month: int, year: int,
               bid_start: datetime.date = None, bid_end: datetime.date = None,
               weights: dict = None) -> Optional[dict]:
    """
    Compute a composite score for a trip. Lower raw penalty = better trip.
    Returns a dict with score breakdown or None if trip should be excluded.
    """
    trip = trips.get(trip_number)
    if not trip:
        return None

    trip_len = get_trip_length(trip_number, layovers, legs)
    day_legs_map = get_trip_day_legs(trip_number, legs)
    all_day_keys = sorted(set(list(day_legs_map.keys()) + [r['trip_day'] for r in layovers.get(trip_number, [])]))

    # Check-out = last leg arr_time on last day + 30 min
    check_out_str = '—'
    if all_day_keys:
        last_day_legs = day_legs_map.get(all_day_keys[-1], [])
        if last_day_legs:
            arr_min = clock_to_min(last_day_legs[-1]['arr_time']) + 30
            check_out_str = f"{arr_min // 60}:{arr_min % 60:02d}"

    # ── 1. Days-off conflict ──────────────────────────────────
    # Support both 'operates_on' (new format) and 'effective_dates' (legacy format)
    if 'operates_on' in trip and bid_start:
        op_dates = parse_operates_on(trip['operates_on'], bid_start)
    elif 'effective_dates' in trip:
        date_tuples = parse_effective_dates(trip['effective_dates'], trip.get('except_dates', ''), month, year)
        op_dates = [datetime.date(year, m, d) for (m, d) in date_tuples]
    else:
        op_dates = []

    # Filter out trips whose effective dates are entirely outside the bid period
    if op_dates and bid_start and bid_end:
        in_period = any(bid_start <= d <= bid_end for d in op_dates)
        if not in_period:
            return {'excluded': True, 'reason': 'Outside bid period', 'trip_number': trip_number}

    if op_dates and days_off:
        if all(d in days_off for d in op_dates):
            return {'excluded': True, 'reason': 'Days-off conflict', 'trip_number': trip_number}

    # ── 2. Red-eye elimination ────────────────────────────────
    all_legs_list = legs.get(trip_number, [])
    day_groups = get_trip_day_legs(trip_number, legs)
    for dk, dlegs in day_groups.items():
        offending_leg = is_red_eye(dlegs)
        if offending_leg:
            return {
                'excluded': True,
                'reason': f'Red-eye (day {dk})',
                'trip_number': trip_number,
                'red_eye_arr_city': offending_leg['arr_city'],
                'red_eye_arr_time': offending_leg['arr_time'],
            }

    # ── 4. LLV threshold analysis ─────────────────────────────
    total_credit_min = hhmm_to_min(trip['total_credit'])
    real_credit_str = trip.get('real_credit', '').strip()
    real_credit_hrs = hhmm_to_min(real_credit_str) / 60 if real_credit_str else 0
    real_credit_bonus = -(real_credit_hrs * 5)  # bonus: more real credit → lower score

    # LLV threshold: minimum credit for a trip of this length to keep total
    # days worked at floor(LLV / 5:15).  threshold(N) = N × (LLV / d)
    _llv_d = max(1, int(llv_min / MIN_CREDIT_DAY_MIN))
    threshold_min = trip_len * (llv_min / _llv_d)
    meets_llv_threshold = total_credit_min >= threshold_min

    surplus_bonus = 0  # LLV bonus removed; meets_llv_threshold kept for ✓ mark

    # ── 5. TAFB (Time Away From Base) ─────────────────────────
    tafb_min = hhmm_to_min(trip['tafb'])
    tafb_penalty = 0

    # ── 6. FDP analysis ───────────────────────────────────────
    total_fdp_min = hhmm_to_min(trip['total_fdp'])
    fdp_per_day = total_fdp_min / trip_len if trip_len > 0 else 0
    
    # Penalty for high daily FDP
    fdp_penalty = max(0, (fdp_per_day - 8 * 60)) / 60 * 20

    # Penalty for wildly uneven FDPs across days
    fdp_variance_penalty = 0
    if trip_len > 1:
        day_fdps = []
        for lrow in layovers.get(trip_number, []):
            day_fdps.append(hhmm_to_min(lrow['FDP']))
        if len(day_fdps) > 1:
            avg_fdp = sum(day_fdps) / len(day_fdps)
            variance = sum((x - avg_fdp) ** 2 for x in day_fdps) / len(day_fdps)
            fdp_variance_penalty = (variance ** 0.5) / 60 * 2

    check_in_min = hhmm_to_min(trip['check_in_time'])

    # ── 7. Early flying (per day) ──────────────────────────────
    # Penalize each day where flying starts early (before 9:00 AM Mountain Time = 540 min)
    # 5 pts/hr per hour earlier than 9 AM, uncapped
    early_flying_penalty = 0
    for dk, dlegs in day_legs_map.items():
        if not dlegs:
            continue
        first_leg = dlegs[0]
        first_dep_min = clock_to_min(first_leg['dep_time'])
        dep_airport = first_leg.get('dep_city', 'PVU')
        first_dep_mtn = to_mountain_time(first_dep_min, dep_airport) - 60  # subtract 1hr report time
        if first_dep_mtn < 540:
            early_hrs = (540 - first_dep_mtn) / 60  # uncapped
            early_flying_penalty += early_hrs * 5  # 5 pts/hr per day
    late_checkin_penalty = 0

    # Penalize each day where flying ends late (after 8:00 PM Mountain Time = 1200 min)
    # 5 pts/hr past 8 PM, uncapped
    late_flying_penalty = 0
    for dk, dlegs in day_legs_map.items():
        if not dlegs:
            continue
        last_leg = dlegs[-1]
        last_arr_min = clock_to_min(last_leg['arr_time'])
        arr_airport = last_leg.get('arr_city', 'PVU')
        last_arr_mtn = to_mountain_time(last_arr_min, arr_airport)
        if last_arr_mtn > 1200:
            late_hrs = (last_arr_mtn - 1200) / 60  # uncapped
            late_flying_penalty += late_hrs * 5  # 5 pts/hr per day

    # ── 8. Legs per day ───────────────────────────────────────
    num_days = len(day_groups) if day_groups else 1
    free_per_day = 2 if num_days == 1 else 1
    legs_penalty = 0
    for _, _dlegs in day_groups.items():
        dc = len(_dlegs)
        if dc == 1:
            legs_penalty -= 10   # bonus: single-leg day
        else:
            legs_penalty += max(0, dc - free_per_day) * 10

    # ── 9. SIT times ──────────────────────────────────────────
    sit_penalty = 0

    # Parse SIT from total_pay field
    sit_str = ''
    pay_str = trip.get('total_pay', '')
    if 'SIT' in pay_str:
        try:
            sit_part = [p for p in pay_str.split() if 'SIT' in p][0]
            sit_val = sit_part.replace('SIT', '')
            sit_min_parsed = hhmm_to_min(sit_val)
            sit_penalty += sit_min_parsed * 0.5
        except:
            pass

    # ── 10. Weekday priority ──────────────────────────────────
    has_weekday, touches_weekend = days_touch_weekend(trip, layovers.get(trip_number, []), trip_len, month, year, bid_start)
    weekend_penalty = 40 if touches_weekend else 0
    flies_on_weekend = touches_weekend

    # ── Split operates_on dates by weekday vs weekend ─────────
    # For multi-day trips, check ALL days the trip spans, not just the start date.
    # A start date goes in the weekend group if any day in its span is Sat/Sun.
    _raw_op = trip.get('operates_on', '')
    if _raw_op and bid_start:
        _wd_parts, _we_parts, _all_parts = [], [], []
        _cur_month = bid_start
        _last_num = -1
        for _p in _raw_op.split(','):
            _p = _p.strip()
            if not _p:
                continue
            try:
                _day_num = int(_p)
                if _last_num >= 0 and _day_num <= _last_num:
                    _cur_month = _next_month(_cur_month)
                _last_num = _day_num
                _dt = datetime.date(_cur_month.year, _cur_month.month, _day_num)
                _label = f'[{_p}]' if days_off and _dt in days_off else _p
                _touches_we = any(
                    (_dt + datetime.timedelta(days=off)).weekday() >= 5
                    for off in range(trip_len)
                )
                (_we_parts if _touches_we else _wd_parts).append(_label)
                _all_parts.append(_label)
            except ValueError:
                _wd_parts.append(_p)
                _we_parts.append(_p)
                _all_parts.append(_p)
        def _group(parts):
            out, run = [], []
            for p in parts:
                if p.startswith('[') and p.endswith(']'):
                    run.append(p[1:-1])
                else:
                    if run:
                        out.append('[' + ','.join(run) + ']')
                        run = []
                    out.append(p)
            if run:
                out.append('[' + ','.join(run) + ']')
            return ','.join(out)

        _wd_non_off = [p for p in _wd_parts if not (p.startswith('[') and p.endswith(']'))]
        _we_non_off = [p for p in _we_parts if not (p.startswith('[') and p.endswith(']'))]
        operates_on_weekday = _group(_wd_non_off) if _wd_non_off else '—'
        operates_on_weekend = _group(_we_non_off) if _we_non_off else '—'
        operates_on_display = _group(_all_parts) or _raw_op
    else:
        operates_on_weekday = operates_on_weekend = operates_on_display = trip.get('operates_on', trip.get('effective_dates', ''))

    # ── 11. Circadian / layover city analysis ────────────────
    circadian_penalty = 0
    for lrow in layovers.get(trip_number, []):
        airport = lrow['airport']
        fdp_end_min = hhmm_to_min(lrow['FDP'])  # This is FDP duration
        # Estimate arrival time at layover: check_in + FDP
        est_arrival_local = check_in_min + fdp_end_min
        est_arrival_mtn = to_mountain_time(est_arrival_local % 1440, airport)
        # Penalty if arriving at layover after midnight Mountain time
        midnight_mtn = 24 * 60
        if est_arrival_mtn % 1440 > midnight_mtn % 1440 and est_arrival_mtn > midnight_mtn:
            circadian_penalty += 25
        # Extra penalty for early departures from eastern time zones
        if airport in EASTERN_AIRPORTS:
            # If first leg next day would be early, penalize
            circadian_penalty += 5

    # ── 13. Trip length bonus (prefer 1-day > 2-day > 3-day) ──
    length_bonus = {1: -40, 2: -20, 3: 0, 4: 20, 5: 40}.get(trip_len, 40)

    # ── 14. Dead head penalty ─────────────────────────────────
    # 5 pts per hour of dead head block time across the entire trip
    dh_total_min = sum(
        hhmm_to_min(leg.get('block_time', '0'))
        for leg in all_legs_list
        if leg.get('is_deadhead') == 'True'
    )
    deadhead_penalty = (dh_total_min / 60) * 5

    # ── Apply scoring weights ─────────────────────────────────
    w = weights or {}
    def _w(key): return w.get(key, 1.0)

    early_flying_penalty  *= _w('early_flying')
    late_flying_penalty   *= _w('late_flying')
    legs_penalty          *= _w('legs')
    sit_penalty           *= _w('sit')
    weekend_penalty       *= _w('weekend')
    circadian_penalty     *= _w('circadian')
    fdp_penalty           *= _w('fdp')
    fdp_variance_penalty  *= _w('fdp')
    length_bonus          *= _w('length')
    surplus_bonus         *= _w('llv_threshold')
    real_credit_bonus     *= _w('real_credit')
    deadhead_penalty      *= _w('deadhead')

    # ── Composite score ───────────────────────────────────────
    total_penalty = (
        surplus_bonus
        + tafb_penalty
        + fdp_penalty
        + fdp_variance_penalty
        + early_flying_penalty
        + late_checkin_penalty
        + late_flying_penalty
        + legs_penalty
        + sit_penalty
        + circadian_penalty
        + length_bonus
        + real_credit_bonus
        + deadhead_penalty
    )
    # score_weekend adds the weekend penalty only when trip is evaluated in a weekend context
    score_weekend = total_penalty + weekend_penalty

    return {
        'trip_number': trip_number,
        'trip_length': trip_len,
        'score': round(total_penalty, 1),
        'score_weekend': round(score_weekend, 1),
        'check_in': min_to_hhmm(hhmm_to_min(trip['check_in_time'])),
        'check_out': check_out_str,
        'total_credit': trip['total_credit'],
        'meets_llv': meets_llv_threshold,
        'real_credit': real_credit_str or '—',
        'tafb': trip['tafb'],
        'tafb_min': tafb_min,
        'total_fdp': trip['total_fdp'],
        'operates_on': operates_on_display,
        'operates_on_weekday': operates_on_weekday,
        'operates_on_weekend': operates_on_weekend,
        'total_legs': len(all_legs_list),
        'avg_daily_legs': round(len(all_legs_list) / trip_len, 1),
        'flies_on_weekend': flies_on_weekend,
        'has_weekday': has_weekday,
        # Score breakdown
        '_breakdown': {
            'length': round(length_bonus, 1),
            'LLV_threshold': round(surplus_bonus, 1),
            'tafb': round(tafb_penalty, 1),
            'fdp': round(fdp_penalty + fdp_variance_penalty, 1),
            'early_flying': round(early_flying_penalty, 1),
            'late_flying': round(late_flying_penalty, 1),
            'legs': round(legs_penalty, 1),
            'sit': round(sit_penalty, 1),
            'weekend': round(weekend_penalty, 1),
            'circadian': round(circadian_penalty, 1),
            'real_credit_bonus': round(real_credit_bonus, 1),
            'deadhead': round(deadhead_penalty, 1),
        }
    }


# ─────────────────────────────────────────────────────────────
# LLV days-worked calculation
# ─────────────────────────────────────────────────────────────

def llv_analysis(llv_min: int) -> dict:
    """Calculate minimum days needed and required daily credit per trip length."""
    result = {}
    for days in range(1, 6):
        needed_per_trip = llv_min / days
        h, m = divmod(int(needed_per_trip), 60)
        result[days] = {'min_days': days, 'required_credit': f"{h}:{m:02d}",
                        'required_min': needed_per_trip}
    return result


# ─────────────────────────────────────────────────────────────
# Display
# ─────────────────────────────────────────────────────────────

HEADER = "=" * 120
SUBHEADER = "-" * 120

def format_score_bar(score: float, min_score: float, max_score: float, width: int = 20) -> str:
    """Visual bar for score (lower = better = more filled)."""
    if max_score == min_score:
        pct = 0.5
    else:
        pct = 1.0 - (score - min_score) / (max_score - min_score)
    filled = int(pct * width)
    return f"[{'█' * filled}{'░' * (width - filled)}]"


def print_results(scored: list, llv_min: int, days_off_str: str, bid_period_label: str,
                  month: int, year: int, top_n: int = 20):
    """Print ranked trip results grouped by trip length."""
    # llv_info = llv_analysis(llv_min)  # LLV efficiency disabled
    month_name = calendar.month_name[month]

    generated_at = datetime.datetime.now().strftime('%A, %B %d, %Y  %I:%M %p')
    print()
    print(HEADER)
    print(f"  ✈  PILOT TRIP ANALYZER  —  {month_name} {year}  |  Generated: {generated_at}")
    print(HEADER)
    _llv_d = max(1, int(llv_min / MIN_CREDIT_DAY_MIN))
    llv_thresholds = '  '.join(
        f'{n}D>={min_to_hhmm(round(n * llv_min / _llv_d))}'
        for n in range(1, 6)
    )
    print(f"  Bid period: {bid_period_label}  |  Days off: {days_off_str or 'None'}")
    print(f"  LLV: {min_to_hhmm(llv_min)}   {_llv_d} Day Target   {llv_thresholds}")
    #print()
    # # LLV EFFICIENCY TARGETS — disabled
    # print(f"  LLV EFFICIENCY TARGETS (minimum credit to work N trips/month at this length):")
    # for days, info in llv_info.items():
    #     marker = "◀ ideal" if days == 1 else ""
    #     print(f"    {days}-day trips: need ≥ {info['required_credit']} credit/trip  {marker}")
    # print()

    # Group by weekend flag: trips with any weekday operating dates first, then any trip touching a weekend.
    # A trip with mixed dates (some weekday, some weekend) appears in BOTH groups.
    weekday = sorted([s for s in scored if s['operates_on_weekday'] != '—'], key=lambda x: x['score'])
    on_weekend = sorted([s for s in scored if s['flies_on_weekend']], key=lambda x: x['score_weekend'])

    groups = [
        ("WEEKDAY TRIPS  (no weekend flying)", weekday, 'weekday'),
        ("TRIPS WITH WEEKEND FLYING  (including mixed weekday/weekend)", on_weekend, 'weekend'),
    ]

    for group_label, group, group_type in groups:
        if not group:
            continue
        score_key = 'score' if group_type == 'weekday' else 'score_weekend'
        min_s = group[0][score_key]
        max_s = group[-1][score_key]

        print(HEADER)
        print(f"  {group_label}  —  {len(group)} trips")
        print(HEADER)

        # Column header
        print(f"  {'RK':>3}  {'TRIP':>6}{'':1}  {'LEN':>4}  {'SCORE':>7}  {'QUALITY BAR':22}  {'CREDIT':>7}  {'R.CR':>5}  {'CHK-IN':>7}  {'CHK-OUT':>8}  "
              f"{'TAFB':>6}  {'LGS':>4}  {'OPERATES ON'}")
        print(SUBHEADER)

        shown = group[:top_n]
        for rank, s in enumerate(shown, 1):
            display_score = s[score_key]
            bar = format_score_bar(display_score, min_s, max_s)
            avoidance = "⚠" if s['trip_length'] >= 4 else " "
            op_on = s['operates_on_weekday'] if group_type == 'weekday' else s['operates_on_weekend']
            llv_mark = '✓' if s['meets_llv'] else ' '
            print(f"  {rank:>3}  {s['trip_number']:>6}{llv_mark}  {avoidance}{s['trip_length']:>3}  {display_score:>7.1f}  {bar}  "
                  f"{s['total_credit']:>7}  {s['real_credit']:>5}  {s['check_in']:>7}  {s['check_out']:>8}  "
                  f"{s['tafb']:>6}  {s['avg_daily_legs']:>4.1f}  "
                  f"{op_on}")

        if len(group) > top_n:
            print(f"  ... and {len(group) - top_n} more trips not shown (use --top to increase)")

    print(HEADER)
    total_scored = len(scored)
    print(f"  Total trips analyzed: {total_scored}")
    #print()

    print(HEADER)
    #print()
    print("  LEGEND:")
    print("    Score:  Lower = Better")
    print("    CHK-IN: Check-in time (HH:MM)  |  TAFB: Time Away From Base")
    print("    LGS: Avg legs/day  |  R.CR = Free $$$  |  ✓: Meets LLV optimization threshold")
    # print("    Score:  Lower = Better  |  ✓ Credit meets LLV target  |  🔥 High efficiency  |  ✗ Below target")  # LLV efficiency disabled
    # print("    LGS: Avg legs/day  |  EFFIC: Credit efficiency vs LLV target  |  R.CR: Real Credit (over guarantee)")  # LLV efficiency disabled
    print()


def print_score_breakdown(trip_number: str, trips: dict, legs: dict, layovers: dict,
                           llv_min: int, days_off: set, month: int, year: int,
                           bid_start: datetime.date = None, bid_end: datetime.date = None):
    """Verbose score breakdown for a single trip."""
    result = score_trip(trip_number, trips, legs, layovers, llv_min, days_off, month, year, bid_start, bid_end)
    if not result:
        print(f"Trip {trip_number} was eliminated or not found.")
        return
    print(f"\n{'='*60}")
    print(f"  SCORE BREAKDOWN: Trip {trip_number}  ({result['trip_length']}-day)")
    print(f"{'='*60}")
    print(f"  TOTAL SCORE: {result['score']:.1f}  (lower = better)")
    print(f"\n  Component Breakdown:")
    for k, v in result['_breakdown'].items():
        indicator = " 🔴" if v > 50 else (" 🟡" if v > 20 else " 🟢")
        print(f"    {k:20s}: {v:>8.1f}{indicator}")
    print(f"\n  Trip Details:")
    print(f"    Check-in:    {result['check_in']}")
    print(f"    Credit:      {result['total_credit']}")
    print(f"    Real Credit: {result['real_credit']}")
    print(f"    TAFB:        {result['tafb']}")
    print(f"    Avg Legs/Day:{result['avg_daily_legs']}")
    print(f"    Operates On: {result['operates_on']}")
    # Per-day legs breakdown
    day_groups_detail = get_trip_day_legs(trip_number, legs)
    if day_groups_detail:
        print(f"\n  Legs Per Day Breakdown:")
        total_leg_pen = 0
        for dk in sorted(day_groups_detail.keys()):
            dlegs = day_groups_detail[dk]
            non_dh = [l for l in dlegs if l['is_deadhead'] == 'False']
            dh     = [l for l in dlegs if l['is_deadhead'] == 'True']
            n = len(non_dh)
            extra = max(0, n - 2) if len(day_groups_detail) == 1 else max(0, n - 1)
            day_pen = (extra * 10) + len(dh) * 10
            total_leg_pen += day_pen
            dh_note = f"  +{len(dh)} DH" if dh else ""
            pen_note = f"  → {day_pen} pts" if day_pen > 0 else "  → 0 pts"
            print(f"    Day {dk}: {n} non-DH leg{'s' if n != 1 else ''}{dh_note}{pen_note}")


# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Plain-text score detail export
# ─────────────────────────────────────────────────────────────────────────────

SCORE_LABELS = {
    'length':            'Trip length bonus/penalty',
    'LLV_threshold':     'LLV threshold bonus',
    'tafb_relative':     'TAFB position vs bid-period range',

    'early_flying':      'Early flying penalty',
    'late_flying':       'Late flying penalty',
    'legs':              'Legs per day',
    'sit':               'Sit Penalty',
    'weekend':           'Weekend penalty',
    'circadian':         'Circadian / layover timing',
    'real_credit_bonus': 'Real credit bonus (r.cr hrs × 5)',
}

def format_indicator(v):
    if v > 50:   return '[HIGH]'
    if v > 20:   return '[MED] '
    if v < -10:  return '[GOOD]'
    return '[OK]  '


def export_score_details(scored, path, llv_min, days_off_str, bid_period_label,
                         trips, legs, layovers, month, year, excluded=None):
    """Write a detailed per-trip score breakdown to a plain text file."""
    import calendar as cal
    W = 72
    SEP  = '=' * W
    DASH = '-' * W
    THIN = '.' * W

    out = []
    month_name = cal.month_name[month]
    llv_str = min_to_hhmm(llv_min)

    generated_at = datetime.datetime.now().strftime('%A, %B %d, %Y  %I:%M %p')
    _llv_d = max(1, int(llv_min / MIN_CREDIT_DAY_MIN))
    llv_thresholds = '  '.join(
        f'{n}D≥{min_to_hhmm(round(n * llv_min / _llv_d))}'
        for n in range(1, 6)
    )
    out.append(SEP + '\n')
    out.append(f'  PILOT TRIP ANALYZER - SCORE DETAIL REPORT\n')
    out.append(f'  {month_name} {year}   Bid period: {bid_period_label}   Days off: {days_off_str or "None"}\n')
    out.append(f'  LLV: {llv_str}   {_llv_d} Day Target   {llv_thresholds}\n')
    out.append(f'  Generated: {generated_at}\n')
    out.append(SEP + '\n')
    out.append(f'  Trips scored: {len(scored)}\n')
    out.append(f'  Score: lower = better  |  [HIGH] components drive the score up\n')
    out.append('\n')

    weekday = sorted([s for s in scored if s['operates_on_weekday'] != '—'], key=lambda x: x['score'])
    on_weekend = sorted([s for s in scored if s['flies_on_weekend']], key=lambda x: x['score_weekend'])
    groups = [
        ('WEEKDAY TRIPS  (no weekend flying)', weekday, 'weekday'),
        ('TRIPS WITH WEEKEND FLYING  (including mixed weekday/weekend)', on_weekend, 'weekend'),
    ]

    for group_label, group, group_type in groups:
        if not group:
            continue
        score_key = 'score' if group_type == 'weekday' else 'score_weekend'
        out.append(SEP + '\n')
        out.append(f'  {group_label}  ({len(group)} trips)\n')
        out.append(SEP + '\n')

        for rank, s in enumerate(group, 1):
            trip_num  = s['trip_number']
            trip_layovers = layovers.get(trip_num, [])
            day_groups    = get_trip_day_legs(trip_num, legs)

            out.append('\n')
            out.append(DASH + '\n')
            out.append(f'  Rank {rank:>3}  |  Trip {trip_num}  |  {s["trip_length"]}-day'
                       f'  |  TOTAL SCORE: {s[score_key]:>7.1f}\n')
            out.append(DASH + '\n')

            op_on = s['operates_on_weekday'] if group_type == 'weekday' else s['operates_on_weekend']
            out.append(f'  Operates on  : {op_on}\n')
            out.append(f'  Check-in     : {s["check_in"]}\n')
            out.append(f'  Check-out    : {s["check_out"]}\n')
            out.append(f'  Total credit : {s["total_credit"]}  (real: {s["real_credit"]})\n')
            out.append(f'  TAFB         : {s["tafb"]}\n')
            out.append(f'  Total legs   : {s["total_legs"]}  (avg {s["avg_daily_legs"]:.1f}/day)\n')
            # Per-day legs penalty breakdown
            legs_detail_parts = []
            for dk in sorted(day_groups.keys()):
                dlegs = day_groups[dk]
                non_dh = [l for l in dlegs if l['is_deadhead'] == 'False']
                dh     = [l for l in dlegs if l['is_deadhead'] == 'True']
                n = len(non_dh)
                extra = max(0, n - 2) if len(day_groups) == 1 else max(0, n - 1)
                day_pen = (extra * 10) + len(dh) * 10
                dh_note = f'+{len(dh)}DH' if dh else ''
                legs_detail_parts.append(f'D{dk}:{n}{"/" + dh_note if dh_note else ""}={day_pen}pts')
            out.append(f'  Legs penalty : {s["_breakdown"].get("legs", 0.0):.0f} pts  [{", ".join(legs_detail_parts)}]\n')
            # out.append(f'  LLV effic.   : {s["efficiency_ratio"]:.2f}x target\n')  # LLV efficiency disabled

            out.append('\n')
            out.append(f'  SCORE BREAKDOWN:\n')
            out.append(f'  {"Component":<33} {"Points":>8}   Status\n')
            out.append(f'  {THIN[:61]}\n')
            for key, label in SCORE_LABELS.items():
                val = s['_breakdown'].get(key, 0.0)
                if key == 'weekend' and group_type == 'weekday':
                    val = 0.0
                indicator = format_indicator(val)
                out.append(f'  {label:<33} {val:>8.1f}   {indicator}\n')
            out.append(f'  {THIN[:61]}\n')
            out.append(f'  {"TOTAL":<33} {s[score_key]:>8.1f}\n')

            out.append('\n')
            out.append(f'  DAILY ITINERARY:\n')

            all_day_keys = sorted(set(
                list(day_groups.keys()) + [r['trip_day'] for r in trip_layovers]
            ))

            for dk in all_day_keys:
                dlegs = day_groups.get(dk, [])
                out.append(f'    Day {dk}:\n')
                for leg in dlegs:
                    dh  = ' [DHD]' if leg['is_deadhead'] == 'True' else ''
                    blk = leg['block_time'] or '--'
                    trn = f'  turn:{leg["turn_time"]}' if leg['turn_time'] else ''
                    out.append(
                        f'      Flt {leg["flight_num"]:>5}  '
                        f'{leg["dep_city"]} {leg["dep_time"]} -> '
                        f'{leg["arr_city"]} {leg["arr_time"]}  '
                        f'blk:{blk}{trn}{dh}\n'
                    )
                lay_rows = [r for r in trip_layovers if r['trip_day'] == dk]
                for lr in lay_rows:
                    out.append(
                        f'      Layover: {lr["airport"]}  '
                        f'{lr["layover_time"]}h rest  '
                        f'MDP:{lr["MDP"]}\n'
                    )
                if not dlegs and not lay_rows:
                    out.append(f'      (no leg data)\n')

            out.append('\n')

        out.append('\n')

    # Summary table
    out.append(SEP + '\n')
    out.append('  SUMMARY - WEEKDAY-ONLY TRIPS FIRST, THEN WEEKEND, EACH BY SCORE\n')
    out.append(SEP + '\n')
    out.append(f'  {"RK":>3}  {"TRIP":>6}  {"LEN":>4}  {"SCORE":>7}  '
               f'{"CREDIT":>7}  {"R.CR":>5}  {"CHK-IN":>7}  {"CHK-OUT":>8}  {"TAFB":>6}  '
               f'OPERATES ON\n')
    out.append(DASH + '\n')
    rank = 0
    for _, summary_group, _ in groups:
        for s in summary_group:
            rank += 1
            out.append(
                f'  {rank:>3}  {s["trip_number"]:>6}  {s["trip_length"]:>4}  '
                f'{s["score"]:>7.1f}  {s["total_credit"]:>7}  {s["real_credit"]:>5}  '
                f'{s["check_in"]:>7}  {s["check_out"]:>8}  {s["tafb"]:>6}  '
                # f'{s["efficiency_ratio"]:>5.2f}x  '  # LLV efficiency disabled
                f'{s["operates_on"]}\n'
            )
    out.append(SEP + '\n')

    # Excluded trips appendix
    if excluded:
        out.append('\n')
        out.append(SEP + '\n')
        out.append(f'  EXCLUDED TRIPS  ({len(excluded)} trips removed from scoring)\n')
        out.append(SEP + '\n')
        out.append(f'  {"TRIP":>6}  {"REASON":<30}  {"DETAIL":>16}  OPERATES ON\n')
        out.append(DASH + '\n')
        by_reason = {}
        for e in excluded:
            by_reason.setdefault(e['reason'], []).append(e['trip_number'])
        for e in sorted(excluded, key=lambda x: (x['reason'], x['trip_number'])):
            t = trips.get(e['trip_number'], {})
            op_on = t.get('operates_on', t.get('effective_dates', '—'))
            if 'red_eye_arr_city' in e:
                detail = f'{e["red_eye_arr_city"]} arr {e["red_eye_arr_time"]}'
            else:
                credit = t.get('total_credit', '—')
                tafb   = t.get('tafb', '—')
                detail = f'{credit} / {tafb}'
            out.append(f'  {e["trip_number"]:>6}  {e["reason"]:<30}  {detail:>16}  {op_on}\n')
        out.append(DASH + '\n')
        out.append('\n')
        out.append('  SUMMARY BY REASON:\n')
        for reason, nums in sorted(by_reason.items()):
            out.append(f'    {reason:<30}  {len(nums):>3} trips\n')
        out.append(SEP + '\n')

    try:
        from reportlab.lib.pagesizes import landscape, letter
        from reportlab.pdfgen import canvas as rl_canvas
    except ImportError:
        print("  ✗  reportlab not installed. Run: pip install reportlab")
        return

    SUBS = {'█': '#', '░': '.', '✓': '+', '⚠': '!', '✈': '>'}
    lines = [''.join(SUBS.get(ch, ch) for ch in ln.rstrip('\n')) for ln in out]

    page_w, page_h = landscape(letter)
    margin = 30.0
    usable_w = page_w - 2 * margin

    max_len = max((len(ln) for ln in lines if ln), default=80)
    font_size = usable_w / (max_len * 0.522)
    font_size = min(font_size, 12.65)

    line_h = font_size * 1.3

    c = rl_canvas.Canvas(path, pagesize=landscape(letter))
    c.setFont('Courier', font_size)

    x = margin
    y = page_h - margin

    for line in lines:
        if y < margin + line_h:
            c.showPage()
            c.setFont('Courier', font_size)
            y = page_h - margin
        c.drawString(x, y, line)
        y -= line_h

    c.save()

# PDF Export
# ─────────────────────────────────────────────────────────────

def export_to_pdf(scored: list, llv_min: int, days_off_str: str, bid_period_label: str,
                  month: int, year: int, top_n: int, filename: str):
    """Export print_results() output to a landscape PDF, maximising font size."""
    try:
        from reportlab.lib.pagesizes import landscape, letter
        from reportlab.pdfgen import canvas as rl_canvas
    except ImportError:
        print("  ✗  reportlab not installed. Run: pip install reportlab")
        return

    import io

    # Capture the standard output
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        print_results(scored, llv_min, days_off_str, bid_period_label, month, year, top_n=top_n)
    finally:
        sys.stdout = old_stdout

    raw_lines = buf.getvalue().splitlines()

    # Substitute Unicode chars that Courier cannot render
    SUBS = {'█': '#', '░': '.', '✓': '+', '⚠': '!', '✈': '>'}
    lines = [''.join(SUBS.get(ch, ch) for ch in ln) for ln in raw_lines]

    # Landscape US Letter: 792 × 612 pt
    page_w, page_h = landscape(letter)
    margin = 30.0
    usable_w = page_w - 2 * margin

    # Courier character width = 0.6 × font_size  (fixed-pitch)
    max_len = max((len(ln) for ln in lines if ln), default=80)
    font_size = usable_w / (max_len * 0.522)
    font_size = min(font_size, 16.1)   # sensible ceiling

    line_h = font_size * 1.3

    c = rl_canvas.Canvas(filename, pagesize=landscape(letter))
    c.setFont('Courier', font_size)

    x = margin
    y = page_h - margin

    for line in lines:
        if y < margin + line_h:
            c.showPage()
            c.setFont('Courier', font_size)
            y = page_h - margin
        c.drawString(x, y, line)
        y -= line_h

    c.save()
    print(f"  PDF exported → {filename}  ({font_size:.1f}pt Courier, landscape Letter)")


def export_trip_list_pdf(scored: list, trips_order: list, llv_min: int, days_off_str: str,
                         bid_period_label: str, month: int, year: int, filename: str):
    """Export all scored trips in input CSV order to a landscape PDF with rank column."""
    try:
        from reportlab.lib.pagesizes import landscape, letter
        from reportlab.pdfgen import canvas as rl_canvas
    except ImportError:
        print("  ✗  reportlab not installed. Run: pip install reportlab")
        return

    scored_map = {s['trip_number']: s for s in scored}
    ordered = [scored_map[tn] for tn in trips_order if tn in scored_map]

    month_name = calendar.month_name[month]
    generated_at = datetime.datetime.now().strftime('%A, %B %d, %Y  %I:%M %p')

    lines = []
    lines.append(HEADER)
    lines.append(f"  PILOT TRIP LIST  —  {month_name} {year}  |  Generated: {generated_at}")
    lines.append(f"  LLV: {min_to_hhmm(llv_min)}  |  Bid period: {bid_period_label}  |  Days off: {days_off_str or 'None'}")
    lines.append(f"  {len(ordered)} trips listed in bid package order  |  Rank: lower score = better")
    lines.append(HEADER)
    lines.append(f"  {'TRIP':>6}{'':1}  {'RK':>4}  {'LEN':>3}  {'SCORE':>6}  {'CREDIT':>6}  {'R.CR':>5}  "
                 f"{'CHK-IN':>6}  {'CHK-OUT':>7}  {'TAFB':>6}  {'LGS':>4}  OPERATES ON")
    lines.append(SUBHEADER)
    for s in ordered:
        llv_mark = '+' if s['meets_llv'] else ' '
        lines.append(f"  {s['trip_number']:>6}{llv_mark}  {s['rank']:>4}  {s['trip_length']:>3}  "
                     f"{s['score']:>6.1f}  {s['total_credit']:>6}  {s['real_credit']:>5}  "
                     f"{s['check_in']:>6}  {s['check_out']:>7}  {s['tafb']:>6}  "
                     f"{s['avg_daily_legs']:>4.1f}  {s['operates_on']}")
    lines.append(HEADER)
    lines.append("  LEGEND:  +: Meets LLV threshold  |  LGS: Avg legs/day  |  R.CR: Real Credit (over guarantee)")
    lines.append("")

    SUBS = {'█': '#', '░': '.', '+': '+', '✓': '+', '⚠': '!', '✈': '>'}
    lines = [''.join(SUBS.get(ch, ch) for ch in ln) for ln in lines]

    page_w, page_h = landscape(letter)
    margin = 30.0
    usable_w = page_w - 2 * margin
    max_len = max((len(ln) for ln in lines if ln), default=80)
    font_size = usable_w / (max_len * 0.522)
    font_size = min(font_size, 16.1)
    line_h = font_size * 1.3

    c = rl_canvas.Canvas(filename, pagesize=landscape(letter))
    c.setFont('Courier', font_size)
    x = margin
    y = page_h - margin
    for line in lines:
        if y < margin + line_h:
            c.showPage()
            c.setFont('Courier', font_size)
            y = page_h - margin
        c.drawString(x, y, line)
        y -= line_h
    c.save()
    print(f"  Trip list PDF exported -> {filename}  ({font_size:.1f}pt Courier, landscape Letter)")


def export_combined_trips_pdf(trips: dict, scored: list, trips_order: list, llv_min: int,
                               days_off_str: str, bid_period_label: str, month: int, year: int,
                               filename: str):
    """Export raw trips followed by scored trip list into a single landscape PDF."""
    try:
        from reportlab.lib.pagesizes import landscape, letter
        from reportlab.pdfgen import canvas as rl_canvas
    except ImportError:
        print("  ✗  reportlab not installed. Run: pip install reportlab")
        return

    generated_at = datetime.datetime.now().strftime('%A, %B %d, %Y  %I:%M %p')
    SUBS = {'█': '#', '░': '.', '+': '+', '✓': '+', '⚠': '!', '✈': '>'}

    # ── Section 1: Raw trips ──────────────────────────────────
    COLS = [
        ('TRIP',       'trip_number',   6),
        ('OPER ON',    'operates_on',  10),
        ('CHK-IN',     'check_in_time', 6),
        ('TOT CR',     'total_credit',  7),
        ('TOT BLK',    'total_block',   8),
        ('CREDIT',     'credit',        7),
        ('TOT FDP',    'total_fdp',     7),
        ('TAFB',       'tafb',          7),
        ('REAL CR',    'real_credit',   7),
        ('TOTAL PAY',  'total_pay',    28),
    ]

    raw_lines = []
    raw_lines.append(HEADER)
    raw_lines.append(f"  RAW TRIPS  —  Generated: {generated_at}  |  {len(trips)} trips")
    raw_lines.append(HEADER)
    raw_lines.append('  ' + '  '.join(label.ljust(width) for label, _, width in COLS))
    raw_lines.append(SUBHEADER)
    for row in trips.values():
        cells = [row.get(key, '').strip().ljust(width)[:width] for _, key, width in COLS]
        raw_lines.append('  ' + '  '.join(cells))
    raw_lines.append(HEADER)
    raw_lines = [''.join(SUBS.get(ch, ch) for ch in ln) for ln in raw_lines]

    # ── Section 2: Trip list ──────────────────────────────────
    scored_map = {s['trip_number']: s for s in scored}
    ordered = [scored_map[tn] for tn in trips_order if tn in scored_map]
    month_name = calendar.month_name[month]

    list_lines = []
    list_lines.append(HEADER)
    list_lines.append(f"  PILOT TRIP LIST  —  {month_name} {year}  |  Generated: {generated_at}")
    list_lines.append(f"  LLV: {min_to_hhmm(llv_min)}  |  Bid period: {bid_period_label}  |  Days off: {days_off_str or 'None'}")
    list_lines.append(f"  {len(ordered)} trips listed in bid package order  |  Rank: lower score = better")
    list_lines.append(HEADER)
    list_lines.append(f"  {'TRIP':>6}{'':1}  {'RK':>4}  {'LEN':>3}  {'SCORE':>6}  {'CREDIT':>6}  {'R.CR':>5}  "
                      f"{'CHK-IN':>6}  {'CHK-OUT':>7}  {'TAFB':>6}  {'LGS':>4}  OPERATES ON")
    list_lines.append(SUBHEADER)
    for s in ordered:
        llv_mark = '+' if s['meets_llv'] else ' '
        list_lines.append(f"  {s['trip_number']:>6}{llv_mark}  {s['rank']:>4}  {s['trip_length']:>3}  "
                          f"{s['score']:>6.1f}  {s['total_credit']:>6}  {s['real_credit']:>5}  "
                          f"{s['check_in']:>6}  {s['check_out']:>7}  {s['tafb']:>6}  "
                          f"{s['avg_daily_legs']:>4.1f}  {s['operates_on']}")
    list_lines.append(HEADER)
    list_lines.append("  LEGEND:  +: Meets LLV threshold  |  LGS: Avg legs/day  |  R.CR: Real Credit (over guarantee)")
    list_lines.append("")
    list_lines = [''.join(SUBS.get(ch, ch) for ch in ln) for ln in list_lines]

    # ── Render both sections into one canvas ──────────────────
    page_w, page_h = landscape(letter)
    margin = 30.0
    usable_w = page_w - 2 * margin
    max_len = max(
        max((len(ln) for ln in raw_lines if ln), default=80),
        max((len(ln) for ln in list_lines if ln), default=80),
    )
    font_size = usable_w / (max_len * 0.522)
    font_size = min(font_size, 16.1)
    line_h = font_size * 1.3

    c = rl_canvas.Canvas(filename, pagesize=landscape(letter))
    c.setFont('Courier', font_size)
    x = margin

    def draw_section(lines, start_new_page=False):
        y = page_h - margin
        if start_new_page:
            c.showPage()
            c.setFont('Courier', font_size)
        for line in lines:
            if y < margin + line_h:
                c.showPage()
                c.setFont('Courier', font_size)
                y = page_h - margin
            c.drawString(x, y, line)
            y -= line_h

    draw_section(raw_lines, start_new_page=False)
    draw_section(list_lines, start_new_page=True)
    c.save()
    print(f"  Combined trips PDF exported -> {filename}  ({font_size:.1f}pt Courier, landscape Letter)")


# Main
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Pilot Trip Analyzer — rank and score bid trips',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('--llv', default='70:53',
                        help='Low Line Value in HH:MM format (default: 70:53)')
    parser.add_argument('--days-off', default='',
                        help='Days off as comma-separated days/ranges, e.g. 1,2,6,7,22-31')
    parser.add_argument('--trips-file', default='26APR-obs_trips.csv')
    parser.add_argument('--legs-file', default='26APR-obs_legs.csv')
    parser.add_argument('--lay-file', default='26APR-obs_layovers.csv')
    parser.add_argument('--top', type=int, default=78,
                        help='Number of top trips per length to display (default: 78)')
    parser.add_argument('--month', type=int, default=4,
                        help='Month number (default: 4=April)')
    parser.add_argument('--year', type=int, default=2026,
                        help='Year (default: 2026)')
    parser.add_argument('--detail', default='',
                        help='Trip number for detailed score breakdown (e.g. --detail 4582)')
    parser.add_argument('--no-export-csv', action='store_true',
                        help='Skip CSV export (default: export to <YYmon>-bid-analysis.csv)')
    parser.add_argument('--no-export-txt', action='store_true',
                        help='Skip reasons PDF export (default: export to <YYmon>-reasons.pdf)')
    parser.add_argument('--no-export-pdf', action='store_true',
                        help='Skip PDF export (default: export to <YYmon>-bid-analysis.pdf)')
    parser.add_argument('--no-export-raw-trips', action='store_true',
                        help='Skip raw trips PDF export (default: export to <YYmon>_trips.pdf)')
    parser.add_argument('--bid-period', default='',
                        help='Bid period as YYmon (e.g. 26apr). Sets year, month, and output file prefix. '
                             'Also accepts plain month name (e.g. april or apr).')
    args = parser.parse_args()

    # Parse LLV
    llv_min = parse_llv(args.llv)
    print(f"\n  Loading data...")

    # Find CSV files by glob pattern *_trips.csv, *_legs.csv, *_layovers.csv
    import glob

    search_dirs = ['.', os.path.dirname(os.path.abspath(__file__)),
                   '/mnt/user-data/uploads']

    def find_file_by_suffix(suffix, explicit_path):
        if explicit_path and os.path.exists(explicit_path):
            return explicit_path
        for d in search_dirs:
            matches = glob.glob(os.path.join(d, f'*_{suffix}.csv'))
            if matches:
                if len(matches) > 1:
                    print(f"  ⚠  Multiple *_{suffix}.csv found, using: {os.path.basename(matches[0])}")
                    print(f"     Others ignored: {[os.path.basename(m) for m in matches[1:]]}")
                else:
                    print(f"  ✓  Found: {os.path.basename(matches[0])}")
                return matches[0]
        flag = '--lay-file' if suffix == 'layovers' else f'--{suffix}-file'
        raise FileNotFoundError(
            f"Cannot find *_{suffix}.csv in any of {search_dirs}. "
            f"Specify it explicitly with {flag} /path/to/file.csv"
        )

    trips_path = find_file_by_suffix('trips',    args.trips_file)
    legs_path  = find_file_by_suffix('legs',     args.legs_file)
    lay_path   = find_file_by_suffix('layovers', args.lay_file)

    trips = load_trips(trips_path)
    legs = load_legs(legs_path)
    layovers = load_layovers(lay_path)

    # Resolve bid period — accept "26apr" (YYmon) or plain month name
    import re as _re
    bp_raw = args.bid_period.strip()
    bp_prefix = ''
    if bp_raw:
        _m = _re.match(r'^(\d{2})([a-zA-Z]{3,})$', bp_raw)
        if _m:
            args.year = 2000 + int(_m.group(1))
            bp_prefix = bp_raw.lower()
            bp_name = _m.group(2).lower()
            args.month = next((i for i in range(1, 13)
                               if calendar.month_abbr[i].lower() == bp_name[:3]), args.month)
        else:
            bp_name = bp_raw.lower()
    else:
        bp_name = calendar.month_name[args.month].lower()
    if bp_name not in BID_PERIOD_DATES:
        bp_name = next((k for k in BID_PERIOD_DATES if k.startswith(bp_name)), bp_name)
    if bp_name not in BID_PERIOD_DATES:
        print(f"  ⚠  Unknown bid period '{args.bid_period}'. Valid: {', '.join(BID_PERIOD_DATES)}")
        sys.exit(1)
    bid_start, bid_end, bid_period_label = get_bid_period(bp_name, args.year)

    days_off = parse_days_off(args.days_off, bid_start)

    print(f"  Loaded {len(trips)} trips, {sum(len(v) for v in legs.values())} legs, "
          f"{sum(len(v) for v in layovers.values())} layovers")
    print(f"  LLV: {min_to_hhmm(llv_min)}  |  Bid period: {bid_period_label}  |  Days off: {args.days_off or 'None'}")

    # Detailed breakdown mode
    if args.detail:
        print_score_breakdown(args.detail, trips, legs, layovers,
                               llv_min, days_off, args.month, args.year, bid_start, bid_end)
        return

    # Launch weight GUI (skipped in --detail mode above)
    weights = launch_weight_gui(WEIGHT_DEFAULTS)
    if weights is None:
        print("  Cancelled.")
        return

    # Score all trips
    print(f"  Scoring trips...")
    scored = []
    excluded = []
    eliminated = 0
    for trip_number in trips:
        result = score_trip(trip_number, trips, legs, layovers,
                            llv_min, days_off, args.month, args.year, bid_start, bid_end,
                            weights=weights)
        if result is None:
            eliminated += 1
        elif result.get('excluded'):
            excluded.append(result)
            eliminated += 1
        else:
            scored.append(result)

    print(f"  {len(scored)} trips scored, {eliminated} eliminated (outside bid period, red-eye, days-off conflicts)")

    # Compute TAFB min/max/midpoint per trip length and apply relative bonus/penalty (2-day+ only)
    TAFB_RELATIVE_MAX_PTS = 20  # ±20 pts across the full TAFB range within each length group
    tafb_weight = weights.get('tafb', 1.0)
    for trip_len in range(2, 6):
        group = [s for s in scored if s['trip_length'] == trip_len]
        if not group:
            continue
        min_tafb = min(s['tafb_min'] for s in group)
        max_tafb = max(s['tafb_min'] for s in group)
        mid_tafb = (min_tafb + max_tafb) / 2
        tafb_range = max_tafb - min_tafb
        for s in group:
            if tafb_range > 0:
                tafb_relative = (s['tafb_min'] - mid_tafb) / tafb_range * (TAFB_RELATIVE_MAX_PTS * 2)
            else:
                tafb_relative = 0.0
            tafb_relative = round(tafb_relative * tafb_weight, 1)
            s['score'] = round(s['score'] + tafb_relative, 1)
            s['_breakdown']['tafb_relative'] = tafb_relative

    # Assign global rank (lower score = rank 1 = best)
    for rank_i, s in enumerate(sorted(scored, key=lambda x: x['score']), 1):
        s['rank'] = rank_i

    # Print results
    print_results(scored, llv_min, args.days_off, bid_period_label, args.month, args.year,
                  top_n=args.top)

    # Derive default export filenames from bid period prefix
    combined_trips_pdf_path = f'{bp_prefix}_trips.pdf' if bp_prefix else 'trips.pdf'
    txt_path = f'{bp_prefix}-reasons.pdf'       if bp_prefix else ''
    pdf_path = f'{bp_prefix}-bid-analysis.pdf'  if bp_prefix else ''

    # Combined trips PDF export (raw trips + trip list in one file)
    if not args.no_export_raw_trips:
        export_combined_trips_pdf(trips, scored, list(trips.keys()), llv_min, args.days_off,
                                  bid_period_label, args.month, args.year, combined_trips_pdf_path)

    # PDF export (on by default when bid period given)
    if pdf_path and not args.no_export_pdf:
        export_to_pdf(scored, llv_min, args.days_off, bid_period_label,
                      args.month, args.year, args.top, pdf_path)

    # Reasons PDF export (on by default when bid period given)
    if txt_path and not args.no_export_txt:
        export_score_details(scored, txt_path, llv_min, args.days_off, bid_period_label,
                             trips, legs, layovers, args.month, args.year, excluded=excluded)
        print(f"  Score details exported to {txt_path}")


if __name__ == '__main__':
    main()
