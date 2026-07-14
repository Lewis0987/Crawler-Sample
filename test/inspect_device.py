# -*- coding: utf-8 -*-
"""
[DIAGNOSTIC 一次性診斷工具]（run_all.py 不會呼叫本檔）
唯讀：讀取 output/device_control_readonly.json，摘要各唯讀端點是否有資料。
"""
import os
import json

# 從專案根目錄的 output/ 讀取（由 device_control_scraper.py 產生）
_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
_DEVICE_JSON = os.path.join(_OUTPUT_DIR, "device_control_readonly.json")

d = json.load(open(_DEVICE_JSON, encoding='utf-8'))
print('logged_in:', d.get('logged_in'))
print('唯讀查詢結果：')
for name, block in d.get('readonly_data', {}).items():
    if block is None:
        status = 'None（無資料/需登入）'
    elif isinstance(block, dict) and block.get('_error'):
        status = f"錯誤 code={block.get('_error')}"
    elif isinstance(block, dict) and block.get('_blocked'):
        status = '被安全阻擋'
    else:
        status = '有資料'
    print(f'  {name}: {status}')
print('控制 API（僅記錄）數量:', len(d.get('control_apis_documented_not_called', [])))
