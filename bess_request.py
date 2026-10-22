import requests


def request_kw(ver, value, status=1):
    auth = ""
    # 設定必要的 headers
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json",
        "accept-language": "en-US",
    }
    setting = [
        # {
        #     "param": 1,
        #     "workingMode": 4,  # 交流恒功率
        #     "setValue": value,  # +值放電-值充電
        #     "reactivePower": None,
        #     "frequency": None,
        # },
        {
            "param": 1,
            "gridInterconnectionMode": 0,
            "energyDispatchingMode": 0,
            "activePowerControlMode": 0,
            "activePowerSetPoint": value,
            "dcControlMode": None,
            "dcCurrentSetPoint": None,
            "dcPowerSetPoint": None,
            "offGridAcVoltRegulation": None,
        },
        {
            "param": 1,
            "gridInterconnectionMode": 0,
            "energyDispatchingMode": 0,
            "activePowerControlMode": 0,
            "activePowerSetPoint": value,
            "dcControlMode": None,
            "dcCurrentSetPoint": None,
            "dcPowerSetPoint": None,
            "offGridAcVoltRegulation": None,
        },
    ]
    base_url = ["http://192.168.128.109:8080/", "http://192.168.128.110:8080/"]
    added_url = [
        # "admin-api/client/dynamic/dataOrControl/manualControl",
        "admin-api/client/dynamic/sinexcel/dataOrControl/manualControl",
        "admin-api/client/dynamic/sinexcel/dataOrControl/manualControl",
    ]
    # {param: 2} #PCS停機
    try:
        # 發送登入請求
        response_login = requests.post(
            base_url[ver] + "admin-api/system/auth/login",
            json={"password": "044725adb8d5c421a8d40dc1496c4ea64589d5996b38b69760737ffefa55d63771cc1f0795b49026026cabe5af960a8c9692ad588bb8b5e9a5f67588d58bd38d826ac0f3b2f85c9fbdf30866439c9cf7ed29e9f702fd967e4de676804b568531267066e8d57f2c4e43", "username": "admin"},
            headers=headers,
            timeout=1,
        )

        # 檢查是否登入成功
        if response_login.status_code == 200:
            auth = response_login.json()["data"]["accessToken"]
            headers.update({"Authorization": f"Bearer {auth}"})
            try:
                requests.post(
                    base_url[ver] + added_url[ver],
                    json=setting[ver],
                    headers=headers,
                    timeout=1,
                )
            except:
                pass
#            if ver == 1 and status == 0:
#                try:
#                    requests.post(
#                        base_url[ver] + added_url[ver],
#                        json={"param": 1},
#                        headers=headers,
#                        timeout=1,
#                    )
#                except:
#                    pass
        else:
            pass
    except:
        pass


def request_mode(ver, code):
    # 0是智慧模式 1是手動模式
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json",
        "accept-language": "en-US",
    }
    base_url = ["http://192.168.128.109:8080/", "http://192.168.128.110:8080/"]
    setting = [
        {"schedulePlanSwitchId": 1, "schedulePlanSwitch": 1, "manualModeSwitch": 0},
        {"schedulePlanSwitchId": 1, "schedulePlanSwitch": 0, "manualModeSwitch": 1},
    ]
    url = [
        "admin-api/schedule/config/editScheduleSwitch",
        "admin-api/schedule/config/editManualSwitch",
    ]
    try:
        response_login = requests.post(
            base_url[ver] + "admin-api/system/auth/login",
            json={"password": "044725adb8d5c421a8d40dc1496c4ea64589d5996b38b69760737ffefa55d63771cc1f0795b49026026cabe5af960a8c9692ad588bb8b5e9a5f67588d58bd38d826ac0f3b2f85c9fbdf30866439c9cf7ed29e9f702fd967e4de676804b568531267066e8d57f2c4e43", "username": "admin"},
            headers=headers,
            timeout=1,
        )
        if response_login.status_code == 200:
            auth = response_login.json()["data"]["accessToken"]
            headers.update({"Authorization": f"Bearer {auth}"})
        requests.put(
            base_url[ver] + url[code], json=setting[code], headers=headers, timeout=1
        )
    except:
        pass
