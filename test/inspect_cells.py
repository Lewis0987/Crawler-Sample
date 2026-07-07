import csv
from collections import Counter

cells = list(csv.DictReader(open('battery_cells.csv', encoding='utf-8-sig')))
print('cell 總數:', len(cells))
print('CSV 欄位:', list(cells[0].keys()))

# 抽查 pack2：序號範圍應 21~40
p2 = [c for c in cells if c['Pack'] == '2']
print('pack2 cell 數:', len(p2), '| 序號範圍:', p2[0]['序號'], '~', p2[-1]['序號'])

# 以明細算 pack2 電壓 max/min
vs = [(float(c['電壓']), c['序號']) for c in p2]
print('明細算 max:', max(vs), ', min:', min(vs))

# 均衡狀態值分布
bs = set(c['均衡狀態'] for c in cells)
print('均衡狀態相異值:', bs)

# 每 pack cell 數檢查
cnt = Counter(c['Pack'] for c in cells)
print('每 pack cell 數是否皆 20:', all(v == 20 for v in cnt.values()), dict(cnt))
