import os
from pathlib import Path

# BASE_DIR 指向项目根目录
base_dir = Path(__file__).parent.resolve()
BASE_DIR = base_dir.parent.resolve()

XHS_SERVER = "http://0.0.0.0:11901"
LOCAL_CHROME_PATH_DEFAULT = (
    "/home/zkvih/playwright_browsers/chromium-1194/chrome-linux/chrome"
)
LOCAL_CHROME_PATH = os.getenv("LOCAL_CHROME_PATH", LOCAL_CHROME_PATH_DEFAULT).strip()
LOCAL_CHROME_HEADLESS = True
