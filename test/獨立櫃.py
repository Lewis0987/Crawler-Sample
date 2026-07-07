from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
import time

# 獨立櫃url
url = "http://192.168.128.110:8853/data-overview"

options = Options()
options.add_argument("--start-maximized")

driver = webdriver.Chrome(options=options)
driver.get(url)

time.sleep(3)  # 等頁面資料載入
text = driver.find_element(By.TAG_NAME, "body").text  # 抓取整個網頁的文字內容   
print(text)



input('Press Enter to exit...')