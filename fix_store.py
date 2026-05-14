"""
fix_store.py — runs before booking bot each time.
Marks all past bookings as closed to prevent spam.
"""
import json
from datetime import datetime

with open('booking_thread_store.json') as f:
    s = json.load(f)

today = datetime.now().strftime('%Y-%m-%d')
fixed = 0

for k, v in s['bookings'].items():
    if not isinstance(v, dict):
        continue
    e = v.get('end_date', '')
    if 'delivery_ts' not in v:        v['delivery_ts'] = None
    if 'pickup_ts' not in v:          v['pickup_ts'] = None
    if 'closed_ts' not in v:          v['closed_ts'] = None
    if 'extension_ts_list' not in v:  v['extension_ts_list'] = []
    if e and e < today:
        if not v.get('closed'):
            v['closed'] = True
            fixed += 1
        v['pickup_alerted'] = True

with open('booking_thread_store.json', 'w') as f:
    json.dump(s, f, indent=2)

print(f'Store fix complete — {fixed} bookings marked closed')
print(f'Total store entries: {len(s["bookings"])}')

# Debug — show last 5 contract IDs in store
sorted_keys = sorted(s['bookings'].keys(), reverse=True)[:5]
print('Latest 5 in store:')
for k in sorted_keys:
    print(f'  {k}')
