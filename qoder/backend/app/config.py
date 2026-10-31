import os

# 笔记源目录（含中文路径，仅用于读取，不受 seekdb 限制）
NOTES_ROOT = os.environ.get(
    "MMKB_NOTES_ROOT",
    r"D:/新建文件夹/Obsidian/吉米",
)

# seekdb 数据库目录
# ──────────────────────────────────────────────────────────────
# 必须使用纯 ASCII 路径：seekdb 底层引擎无法打开含非 ASCII 字符的目录。
# 本项目位于 D:/OceanBase大赛/qoder-lab/qoder（含中文"大赛"），
# 因此数据库不能放在项目内，固定到 C:/Temp/mmkb_db。
# 首次运行时由 database.init_db() 自动创建。
# ──────────────────────────────────────────────────────────────
DB_DIR = os.environ.get("MMKB_DB_DIR", r"C:/Temp/mmkb_db")

# 扫描时排除的目录名
EXCLUDED_DIRS = {".obsidian", ".workbuddy", ".git", ".trash"}

# 分块用的 H2 正则
H2_PATTERN = r"^## .+"
