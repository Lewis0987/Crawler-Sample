from datetime import datetime, time as dt_time
import struct
import time
import threading
import collections
import queue
import modbus_tk.modbus_tcp as modbus_tcp
from bess_request import request_kw, request_mode

# Modbus TCP 參數
METER_HOST = "192.168.128.111"  # 電表 IP
METER_PORT = 502
METER_START = 0x0  # 讀取電表功率地址
METER_COUNT = 2  # 讀取 2 個 16-bit 寄存器

# 🔹 設備 2: 儲能設備 (BESS)
BESS_HOST = "192.168.70.201"
BESS_PORT = 502
BESS_PW_START = 4  # BESS 電池放電資料地址
BESS_PW_COUNT = 2  # 讀取 2 個 16-bit 寄存器
BESS_MODE_START = 11  # BESS PCS運作模式資料地址
BESS_MODE_COUNT = 1  # 讀取 1 個 16-bit 寄存器
BESS_SOC_START = 6  # BESS SOC資料地址
BESS_SOC_COUNT = 1  # 讀取 1 個 16-bit 寄存器
BESS_STATUS_START = 7  # BESS PCS STATUS資料地址
BESS_STATUS_COUNT = 1  # 讀取 1 個 16-bit 寄存器

# ver TW:0 US:1
BESS = ["TW", "US"]
LastVer = []
LastPd = [0, 0]
Step = [0, 0]
is_request = [False]
req_fail = [False, False]
# 離峰用電時段定義
# offpeak_time_range = [["11:00", "14:00"], ["00:00", "06:00"]]
# offpeak_time_range = [["00:00", "09:00"]]
# 台電公告離峰日
OFFPEAK_DATES = [
    # 2026
    datetime(2026, 9, 25).date(),
    datetime(2026, 9, 28).date(),
    datetime(2026, 10, 10).date(),
    datetime(2026, 12, 25).date(),

    # 2027
    datetime(2027, 1, 1).date(),
    datetime(2027, 2, 4).date(),
    datetime(2027, 2, 5).date(),
    datetime(2027, 2, 6).date(),
    datetime(2027, 2, 8).date(),
    datetime(2027, 2, 9).date(),
    datetime(2027, 2, 10).date(),
    datetime(2027, 4, 5).date(),
    datetime(2027, 5, 1).date(),
    datetime(2027, 6, 9).date(),
    datetime(2027, 9, 15).date(),
    datetime(2027, 9, 28).date(),
    datetime(2027, 10, 25).date(),
    datetime(2027, 12, 25).date(),
]
# offpeak_time_range = []

# 儲存 5 次功率讀數
power_readings = collections.deque(maxlen=5)
pl_readings = collections.deque(maxlen=5)

# Pd 參數
Pd = 0  # 獨立櫃放電功率
MAX_kW = 249
MAX_PD_CH = 80  # Charge Pd 最大值
MAX_PD_CH_UNDER = 80  # Under Demand Charge Pd 最大值
MAX_PD_DC = 80  # Discharge Pd 最大值
MAX_PD_DC_OVER = 0  # Over Demand Discharge Pd 最大值
MIN_SOC = 2  # SOC允許的最小值
MAX_SOC = 98  # SOC允許的最大值
OVER_flag = True  # 是否判斷超過契約容量
UNDER_flag = False  # 是否判斷過度放電

# IEEE 754 轉換函數
def modbus_to_ieee754(int_low, int_high):
    combined = (int_high << 16) | int_low
    return struct.unpack("!f", struct.pack("!I", combined))[0]


def modbus_client_init():
    global meter_client, bess_client, registers
    # 初始化 Modbus TCP 連線
    meter_client = modbus_tcp.TcpMaster(host=METER_HOST, port=METER_PORT)
    bess_client = modbus_tcp.TcpMaster(host=BESS_HOST, port=BESS_PORT)
    meter_client.set_timeout(2.0)
    bess_client.set_timeout(2.0)
    registers = {}


modbus_client_init()

# 控制執行緒狀態
running = False
command_queue = queue.Queue()


def PL_read():
    try:
        meter_registers = meter_client.execute(1, 0x3, METER_START, METER_COUNT)
        int_low, int_high = meter_registers[0], meter_registers[1]
        PL = modbus_to_ieee754(int_low, int_high) / 1000  # 轉 kW
        return PL
    except Exception as e:
        print(f"Meter Modbus 讀取失敗: {e}")
        modbus_client_init()


def bess_client_read():
    try:
        for i in range(2):
            regs = bess_client.execute(i + 1, 0x4, 0, 12)
            if regs[0] == 0:
                registers.pop(i, None)
                continue
            registers[i] = regs
    except Exception as e:
        registers.clear()
        print(f"BESS Modbus 讀取失敗: {e}")
        modbus_client_init()


def check_demand():
    PL = PL_read()
    bess_client_read()
    if PL is None:
        print("PL 讀取失敗，無法進行需量控制判斷。")
        return
    if len(registers) == 0:
        print("當前無有效的 BESS 設備。")
        return
    Pd = []
    # 從BESS獲取當下Pd(信號回授校正)
    for i in registers.keys():
        bess_pw = registers[i][BESS_PW_START : BESS_PW_START + BESS_PW_COUNT]
        int_low, int_high = bess_pw[0], bess_pw[1]
        bess_power = modbus_to_ieee754(int_low, int_high) * -1
        Pd.append(
            bess_power * 0.97 if bess_power > 0 else bess_power / 0.98
        )  # 修正電池與PCS功率轉換誤差
    return sum(Pd) + PL


def check_soc(ver, offpeak=False, sat_half_peak=False):
    power_check = registers[ver][1]
    if power_check == 0:
        print(f"{BESS[ver]} 高壓箱處於斷開狀態，請檢查設備...")
        return
    # 確認SOC大於允許的%數才可作需量放電
    soc = registers[ver][BESS_SOC_START]
    if sat_half_peak:
        print(f"{BESS[ver]} SOC:{soc} 週六半尖峰不允許充放電...")
    elif not offpeak and soc <= MIN_SOC:
        print(f"{BESS[ver]} SOC:{soc} 由於SOC已達設置下限，不允許放電...")
        # print(f"{BESS[ver]} SOC:{soc} SOC低於需量模式最低要求...")
    elif offpeak and soc >= MAX_SOC:
        print(f"{BESS[ver]} SOC:{soc} 由於SOC已達設置上限，不允許充電...")
        # print(f"{BESS[ver]} SOC:{soc} SOC高於離峰模式最高上限...")
    else:
        return soc if soc > 0 else 1
    bess_pw = registers[ver][BESS_PW_START : BESS_PW_START + BESS_PW_COUNT]
    int_low, int_high = bess_pw[0], bess_pw[1]
    power = modbus_to_ieee754(int_low, int_high)
    if (power > 0 and round(power) != 0) or (power < 0 and int(power) != 0):
        print(f"{BESS[ver]} 強制操作輸出功率為0")
        request_kw(ver, 0)
    return


def check_bess_mode(ver):
    # 確認PCS運作模式在手動操作 若否則修改
    work_mode = registers[ver][BESS_MODE_START]
    if work_mode != 1:
        print(f"{BESS[ver]} 切換PCS為手動操控模式...")
        request_mode(ver, 1)
        return
    return True


def check_bess_status(ver):
    # 確認PCS運行狀態 若停機則啟機
    bess_status = registers[ver][BESS_STATUS_START]
    if bess_status == 0:
        print(f"{BESS[ver]} PCS處於停機狀態，執行啟機動作...")
        request_kw(ver, 0, 0)
        return
    return True


def check_pd(soc):
    Pd = []
    # 從BESS獲取當下Pd(信號回授校正)
    for i in soc.keys():
        bess_pw = registers[i][BESS_PW_START : BESS_PW_START + BESS_PW_COUNT]
        int_low, int_high = bess_pw[0], bess_pw[1]
        bess_power = modbus_to_ieee754(int_low, int_high) * -1
        Pd.append(
            bess_power * 0.97 if bess_power > 0 else bess_power / 0.98
        )  # 修正電池與PCS功率轉換誤差
    return round(sum(Pd))


def procedure_demand_response(MAX_PD_DC_OVER=0, over=False, offpeak=False, sat_half_peak=False):
    global Pd, running
    bess_client_read()
    if len(registers) == 0:
        print("當前無有效的 BESS 設備。")
        return
    soc = {}
    for i in registers.keys():
        check = check_soc(i, sat_half_peak=sat_half_peak)
        if check:
            soc[i] = check
    if not soc:
        time.sleep(1)
        return
    soc = dict(
        sorted(
            soc.items(),
            key=lambda item: (
                item[0] not in LastVer,
                LastVer.index(item[0]) if item[0] in LastVer else 0,
            ),
        )
    )
    for i in list(soc.keys()):
        if not check_bess_mode(i):
            del soc[i]
    if not soc:
        time.sleep(1)
        return

    for i in list(soc.keys()):
        if not check_bess_status(i):
            del soc[i]
    if not soc:
        time.sleep(3)
        return

    print("讀取 PL 數據...")
    for _ in range(5):
        if not running:
            break
        try:
            meter_registers = meter_client.execute(1, 0x3, METER_START, METER_COUNT)
            int_low, int_high = meter_registers[0], meter_registers[1]
            PL = modbus_to_ieee754(int_low, int_high) / 1000  # 轉 kW
            pl_readings.append(PL)
        except Exception as e:
            print(f"Meter Modbus 讀取失敗: {e}")
            modbus_client_init()
            return
        time.sleep(0.2)
    if not running:
        return
    # 計算 PL 平均值
    PL_avg = sum(pl_readings) / len(pl_readings)
    print(f"計算 PL 平均值 = {PL_avg:.2f} kW")

    bess_client_read()
    if len(registers) == 0:
        print("當前無有效的 BESS 設備。")
        return
    Pd = check_pd(soc)
    for i in list(soc.keys()):
        req_fail[i] = True if abs(Pd - sum(LastPd)) > 1 else False
    LastVer.clear()
    for i in soc.keys():
        LastVer.append(i)

    # 根據 PL 平均值調整 Pd
    if not over:
        if PL_avg > 10:
            Pd = min(Pd + PL_avg * 0.6, MAX_PD_DC)
            for i in soc.keys():
                Step[i] = 0
                Pd_part = Pd * (soc[i] / sum(soc.values()))
                if (Pd_part < 0 and soc[i] >= MAX_SOC):
                    Pd_part = 0
                    print(f"{BESS[i]} SOC:{soc[i]} ，Pd < 0但SOC達設定上限，調整 {BESS[i]} Pd = 0 kW")
                print(f"PL > 10kW，調整 {BESS[i]} Pd = {round(Pd_part)} kW")
                if round(Pd_part) != LastPd[i] or req_fail[i]:
                    LastPd[i] = round(Pd_part)
                    request_kw(i, round(Pd_part))
                    is_request[0] = True
        elif 0 < PL_avg < 10:
            for i in soc.keys():
                Step[i] += 1
                Pd_part = Pd * (soc[i] / sum(soc.values()))
                if Step[i] >= 5:
                    if (Pd_part < 0 and soc[i] >= MAX_SOC):
                        Pd_part = 0
                        print(f"{BESS[i]} SOC:{soc[i]} ，Pd < 0但SOC達設定上限，調整 {BESS[i]} Pd = 0 kW")

                    print(
                        f"PL 介於 0 到 10kW，間段性調整 {BESS[i]} Pd = {round(Pd_part)} kW"
                    )
                    Step[i] = 0
                    if round(Pd_part) != LastPd[i] or req_fail[i]:
                        LastPd[i] = round(Pd_part)
                        request_kw(i, round(Pd_part))
                        is_request[0] = True
                else:
                    print(f"PL 介於 0 到 10kW，不調整Pd")

        elif PL_avg < 0:
            #Pd = Pd + PL_avg - 6
            Pd = max(Pd + PL_avg - 6, -MAX_PD_CH)
            for i in soc.keys():
                Step[i] = 0
                Pd_part = Pd * (soc[i] / sum(soc.values()))
                if (Pd_part < 0 and soc[i] >= MAX_SOC):
                    Pd_part = 0
                    print(f"{BESS[i]} SOC:{soc[i]} ，Pd < 0但SOC達設定上限，調整 {BESS[i]} Pd = 0 kW")
                print(f"PL < 10kW，調整 {BESS[i]} Pd = {round(Pd_part)} kW")
                if round(Pd_part) != LastPd[i] or req_fail[i]:
                    LastPd[i] = round(Pd_part)
                    request_kw(i, round(Pd_part))
                    is_request[0] = True
    else:
        Pd = max(
            min(Pd + (PL_avg - MAX_kW) * 0.6, MAX_PD_DC_OVER), 0 if offpeak else MAX_PD_DC
        )
        for i in soc.keys():
            Step[i] = 0
            Pd_part = Pd * (soc[i] / sum(soc.values()))
            print(f"PL {round(PL_avg)}kW，調整 {BESS[i]} Pd = {round(Pd_part)} kW")
            if round(Pd_part) != LastPd[i] or req_fail[i]:
                LastPd[i] = round(Pd_part)
                request_kw(i, round(Pd_part))
                is_request[0] = True

    if is_request[0]:
        is_request[0] = False
        time.sleep(2)
    print("進行下一個 5次/秒平均取樣...\n")


def procedure_offpeak_response(MAX_PD_CH_UNDER=0, under=False, offpeak=True):
    global Pd, running
    bess_client_read()
    if len(registers) == 0:
        print("當前無有效的 BESS 設備。")
        return
    soc = {}
    for i in registers.keys():
        check = check_soc(i, True)
        if check:
            soc[i] = check
    if not soc:
        time.sleep(1)
        return
    soc = dict(
        sorted(
            soc.items(),
            key=lambda item: (
                item[0] not in LastVer,
                LastVer.index(item[0]) if item[0] in LastVer else 0,
            ),
        )
    )
    for i in list(soc.keys()):
        if not check_bess_mode(i):
            del soc[i]
    if not soc:
        time.sleep(1)
        return

    for i in list(soc.keys()):
        if not check_bess_status(i):
            del soc[i]
    if not soc:
        time.sleep(3)
        return

    print("讀取 PL 數據...")
    for _ in range(5):
        if not running:
            break
        try:
            meter_registers = meter_client.execute(1, 0x3, METER_START, METER_COUNT)
            int_low, int_high = meter_registers[0], meter_registers[1]
            PL = modbus_to_ieee754(int_low, int_high) / 1000  # 轉 kW
            pl_readings.append(PL)
        except Exception as e:
            print(f"Meter Modbus 讀取失敗: {e}")
            modbus_client_init()
            return
        time.sleep(0.2)
    if not running:
        return
    # 計算 PL 平均值
    PL_avg = sum(pl_readings) / len(pl_readings)
    print(f"計算 PL 平均值 = {PL_avg:.2f} kW")

    bess_client_read()
    if len(registers) == 0:
        print("當前無有效的 BESS 設備。")
        return
    Pd = check_pd(soc)
    for i in list(soc.keys()):
        req_fail[i] = True if abs(Pd - sum(LastPd)) > 1 else False
    LastVer.clear()
    for i in soc.keys():
        LastVer.append(i)
    
    if not under:
        # 根據 PL 平均值調整 Pd
        if PL_avg < MAX_kW - 10:
            Pd = max(Pd + (PL_avg - MAX_kW) * 0.6, -MAX_PD_CH)
            for i in soc.keys():
                Step[i] = 0
                Pd_part = (
                    Pd * (1 - (soc[i] / sum(soc.values()))) if len(soc.keys()) > 1 else Pd
                )
                print(f"PL < {MAX_kW - 10}kW，調整 {BESS[i]} Pd = {round(Pd_part)} kW")
                if round(Pd_part) != LastPd[i] or req_fail[i]:
                    LastPd[i] = round(Pd_part)
                    request_kw(i, round(Pd_part))
                    is_request[0] = True
        elif PL_avg < MAX_kW:
            for i in soc.keys():
                Step[i] += 1
                Pd_part = (
                    Pd * (1 - (soc[i] / sum(soc.values()))) if len(soc.keys()) > 1 else Pd
                )
                if Step[i] >= 5:
                    print(
                        f"PL 介於 {MAX_kW - 10} 到 {MAX_kW}kW，間段性調整 {BESS[i]} Pd = {round(Pd_part)} kW"
                    )
                    Step[i] = 0
                    if round(Pd_part) != LastPd[i] or req_fail[i]:
                        LastPd[i] = round(Pd_part)
                        request_kw(i, round(Pd_part))
                        is_request[0] = True
                else:
                    print(f"PL 介於 {MAX_kW - 10} 到 {MAX_kW}kW，不調整Pd")
        else:
            Pd = min(Pd + (PL_avg - MAX_kW) + 2, 0)
            for i in soc.keys():
                Step[i] = 0
                Pd_part = (
                    Pd * (1 - (soc[i] / sum(soc.values()))) if len(soc.keys()) > 1 else Pd
                )
                print(f"PL > {MAX_kW}kW，調整 {BESS[i]} Pd = {round(Pd_part)} kW")
                if round(Pd_part) != LastPd[i] or req_fail[i]:
                    LastPd[i] = round(Pd_part)
                    request_kw(i, round(Pd_part))
                    is_request[0] = True
    else:
        if 0 > PL_avg > -5:
            for i in soc.keys():
                Step[i] += 1
                Pd_part = (
                    Pd * (1 - (soc[i] / sum(soc.values()))) if len(soc.keys()) > 1 else Pd
                )
                if Step[i] >= 5:
                    print(
                        f"PL 介於 -5 到 0kW，間段性調整 {BESS[i]} Pd = {round(Pd_part)} kW"
                    )
                    Step[i] = 0
                    if round(Pd_part) != LastPd[i] or req_fail[i]:
                        LastPd[i] = round(Pd_part)
                        request_kw(i, round(Pd_part))
                        is_request[0] = True
                else:
                    print(f"PL 介於 -5 到 0kW，不調整Pd")
        else:
            Pd = min(max(Pd + PL_avg, -MAX_PD_CH_UNDER), 0)
            for i in soc.keys():
                Step[i] = 0
                Pd_part = (
                    Pd * (1 - (soc[i] / sum(soc.values()))) if len(soc.keys()) > 1 else Pd
                )
                print(f"PL {round(PL_avg)}kW，調整 {BESS[i]} Pd = {round(Pd_part)} kW")
                if round(Pd_part) != LastPd[i] or req_fail[i]:
                    LastPd[i] = round(Pd_part)
                    request_kw(i, round(Pd_part))
                    is_request[0] = True

    if is_request[0]:
        is_request[0] = False
        time.sleep(2)
    print("進行下一個 5次/秒平均取樣...\n")


# def is_offpeak(now: datetime, offpeak_time_range: list):
#     """
#     檢查當前時間是否落在指定的離峰時間區間內。
#     """
#     if now.weekday() == 6:
#         return True
#     for start, end in offpeak_time_range:
#         start_time = dt_time(*map(int, start.split(":")))
#         end_time = dt_time(*map(int, end.split(":")))

#         if start_time <= end_time:
#             # 正常時間區間 (同一天內)
#             if start_time <= now.time() <= end_time:
#                 return True
#         else:
#             # 跨午夜時間區間 (例如 23:00 - 02:00)
#             if now.time() >= start_time or now.time() <= end_time:
#                 return True
#     return False

def is_offpeak(now: datetime) -> bool:
    """
    判斷目前是否為台電高壓／特高壓二段式時間電價的離峰時間。

    夏月（5/16 ~ 10/15）：
        週一～週六：00:00 ~ 09:00
        週日／離峰日：全天

    非夏月：
        週一～週六：00:00 ~ 06:00、11:00 ~ 14:00
        週日／離峰日：全天
    """

    # 週日或台電公告離峰日：全天離峰
    if now.weekday() == 6 or now.date() in OFFPEAK_DATES:
        return True

    current_time = now.time()

    # 夏月：5/16 ~ 10/15
    is_summer = (
        (now.month == 5 and now.day >= 16)
        or now.month in (6, 7, 8, 9)
        or (now.month == 10 and now.day <= 15)
    )

    if is_summer:
        # 夏月：00:00 ~ 09:00
        return current_time < dt_time(9, 0)

    # 非夏月：00:00 ~ 06:00、11:00 ~ 14:00
    return (
        current_time < dt_time(6, 0)
        or dt_time(11, 0) <= current_time < dt_time(14, 0)
    )


def read_and_control():
    global Pd, running, over, under, reset
    over = 0
    under = 0
    reset = 0
    while True:
        try:
            command = command_queue.get_nowait()
            if command == "stop":
                running = False
                while True:
                    time.sleep(5)
                    is_stop = True
                    bess_client_read()
                    if len(registers) == 0:
                        continue
                    for i in LastVer:
                        LastPd[i] = 0
                        Step[i] = 0
                        bess_pw = registers[i][
                            BESS_PW_START : BESS_PW_START + BESS_PW_COUNT
                        ]
                        int_low, int_high = bess_pw[0], bess_pw[1]
                        power = modbus_to_ieee754(int_low, int_high)
                        if (power > 0 and round(power) != 0) or (
                            power < 0 and int(power) != 0
                        ):
                            print(f"{BESS[i]} 強制操作輸出功率為0")
                            request_kw(i, 0)
                            is_stop = False
                    if is_stop:
                        print(f"已停止運行")
                        Pd = 0
                        LastVer.clear()
                        break
            elif command == "quit":
                print("關閉程式...")
                return
        except queue.Empty:
            pass

        if not running:
            time.sleep(1)
            continue
        demand = check_demand()
        if demand is None:
            time.sleep(0.2)
            continue
        now = datetime.now()
        print(now.strftime("%Y-%m-%d %H:%M:%S"), "當前廠區需求:", demand)
        if not OVER_flag:
            pass
        elif demand > MAX_kW:
            over += 1
            reset = 0
        elif over >= 3:
            if demand < MAX_kW:
                reset += 1
                if reset >= 3:
                    print("判斷風險已降低，恢復正常運作...")
                    over = 0
                    reset = 0
            else:
                reset = 0
        if not UNDER_flag:
            pass
        elif demand < 0:
            under += 1
            reset = 0
        elif under >= 3:
            if demand > 0:
                reset += 1
                if reset >= 3:
                    print("判斷其他放電測試已結束或降載，恢復正常運作...")
                    under = 0
                    reset = 0
            else:
                reset = 0
        # offpeak = is_offpeak(now, offpeak_time_range)
        offpeak = is_offpeak(now)
        sat_half_peak = now.weekday() == 5 and not offpeak
        if over >= 3:
            print("判斷當前廠區用電量有超過契約容量風險，實施控管...")
            procedure_demand_response(
                MAX_PD_DC_OVER=MAX_PD_DC_OVER, over=True, offpeak=offpeak
            )
        elif under >= 3:
            print("判斷當前廠區有過度放電，實施節能充電操作...")
            procedure_offpeak_response(MAX_PD_CH_UNDER=MAX_PD_CH_UNDER, under=True, offpeak=offpeak)
        elif offpeak:
            print("執行離峰用電程序...")
            procedure_offpeak_response()
        elif sat_half_peak:
            print("執行週六半尖峰用電程序...")
            procedure_demand_response(sat_half_peak=True)
        else:
            print("執行尖峰用電程序...")
            procedure_demand_response()


# 創建並啟動執行緒
thread = threading.Thread(target=read_and_control, daemon=True)
thread.start()

# 指令處理
while True:
    command = input("請輸入指令 (start/stop/quit/help): \n").strip().lower()
    if command == "start":
        if not running:
            print("開始運行...")
            running = True
        else:
            print("已經在運行中")
    elif command == "status":
        print(f"MAX_kW: {MAX_kW} kW")
        print(f"MAX_PD_CH: {MAX_PD_CH} kW")
        print(f"MAX_PD_CH_UNDER: {MAX_PD_CH_UNDER} kW")
        print(f"MAX_PD_DC: {MAX_PD_DC} kW")
        print(f"MAX_PD_DC_OVER: {MAX_PD_DC_OVER} kW")
        print(f"MIN_SOC: {MIN_SOC}%")
        print(f"MAX_SOC: {MAX_SOC}%")
        print(f"OVER_flag: {OVER_flag}")
        print(f"UNDER_flag: {UNDER_flag}")
    elif command == "help":
        print("指令列表:")
        print("  start - 開始運行")
        print("  stop - 停止運行")
        print("  quit - 關閉程式")
        print("  status - 顯示當前設定值")
        print("  max_kw <value> - 設定 MAX_kW (大於0 kW)")
        print("  ch <value> - 設定 MAX_PD_CH (0-80 kW)")
        print("  ch_under <value> - 設定 MAX_PD_CH_UNDER (0-80 kW)")
        print("  dc <value> - 設定 MAX_PD_DC (0-80 kW)")
        print("  dc_over <value> - 設定 MAX_PD_DC_OVER (0-80 kW)")
        print("  min_soc <value> - 設定 MIN_SOC (0-100%)")
        print("  max_soc <value> - 設定 MAX_SOC (0-100%)")
        print("  over <true/false> - 設定 OVER_flag")
        print("  under <true/false> - 設定 UNDER_flag")
    elif command.startswith("ch "):
        try:
            new_max_pd_ch = int(command.split()[1])
            if 0 <= new_max_pd_ch <= 80:
                MAX_PD_CH = new_max_pd_ch
                print(f"已更新 MAX_PD_CH 為 {MAX_PD_CH} kW")
            else:
                print("請輸入介於 0 到 80 的整數值")
        except (IndexError, ValueError):
            print("請輸入有效的整數值，例如: ch 50")
    elif command.startswith("ch_under "):
        try:
            new_max_pd_ch_under = int(command.split()[1])
            if 0 <= new_max_pd_ch_under <= 80:
                MAX_PD_CH_UNDER = new_max_pd_ch_under
                print(f"已更新 MAX_PD_CH_UNDER 為 {MAX_PD_CH_UNDER} kW")
            else:
                print("請輸入介於 0 到 80 的整數值")
        except (IndexError, ValueError):
            print("請輸入有效的整數值，例如: ch_under 50")
    elif command.startswith("dc "):
        try:
            new_max_pd_dc = int(command.split()[1])
            if 0 <= new_max_pd_dc <= 80:
                MAX_PD_DC = new_max_pd_dc
                print(f"已更新 MAX_PD_DC 為 {MAX_PD_DC} kW")
            else:
                print("請輸入介於 0 到 80 的整數值")
        except (IndexError, ValueError):
            print("請輸入有效的整數值，例如: dc 50")
    elif command.startswith("dc_over "):
        try:
            new_max_pd_dc_over = int(command.split()[1])
            if 0 <= new_max_pd_dc_over <= 80:
                MAX_PD_DC_OVER = new_max_pd_dc_over
                print(f"已更新 MAX_PD_DC_OVER 為 {MAX_PD_DC_OVER} kW")
            else:
                print("請輸入介於 0 到 80 的整數值")
        except (IndexError, ValueError):
            print("請輸入有效的整數值，例如: dc_over 50")
    elif command.startswith("min_soc "):
        try:
            new_min_soc = int(command.split()[1])
            if 0 <= new_min_soc <= 100:
                MIN_SOC = new_min_soc
                print(f"已更新 MIN_SOC 為 {MIN_SOC}%")
            else:
                print("請輸入介於 0 到 100 的整數值")
        except (IndexError, ValueError):
            print("請輸入有效的整數值，例如: min_soc 20")
    elif command.startswith("max_soc "):
        try:
            new_max_soc = int(command.split()[1])
            if 0 <= new_max_soc <= 100:
                MAX_SOC = new_max_soc
                print(f"已更新 MAX_SOC 為 {MAX_SOC}%")
            else:
                print("請輸入介於 0 到 100 的整數值")
        except (IndexError, ValueError):
            print("請輸入有效的整數值，例如: max_soc 80")
    elif command.startswith("over "):
        try:
            new_over_flag = command.split()[1].lower()
            if new_over_flag in ["true", "false"]:
                OVER_flag = new_over_flag == "true"
                print(f"已更新 OVER_flag 為 {OVER_flag}")
                over = 0
                reset = 0
            else:
                print("請輸入 true 或 false")
        except IndexError:
            print("請輸入有效的值，例如: over true")
    elif command.startswith("under "):
        try:
            new_under_flag = command.split()[1].lower()
            if new_under_flag in ["true", "false"]:
                UNDER_flag = new_under_flag == "true"
                print(f"已更新 UNDER_flag 為 {UNDER_flag}")
                under = 0
                reset = 0
            else:
                print("請輸入 true 或 false")
        except IndexError:
            print("請輸入有效的值，例如: under true")
    elif command.startswith("max_kw "):
        try:
            new_max_kW = int(command.split()[1])
            if new_max_kW > 0:
                MAX_kW = new_max_kW
                print(f"已更新 MAX_kW 為 {MAX_kW} kW")
            else:
                print("請輸入大於 0 的整數值")
        except (IndexError, ValueError):
            print("請輸入有效的整數值，例如: max_kw 250")
    elif command == "stop":
        if running:
            command_queue.put(command)
        else:
            print("目前沒有在運行中")
    elif command == "quit":
        command_queue.put(command)
        thread.join()
        break
