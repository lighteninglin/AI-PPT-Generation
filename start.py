#!/usr/bin/env python3
"""PPT-Web 一键启动"""

import sys
import os

# 确保在 ppt-web 目录下运行
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# 安装依赖
print("📦 检查依赖...")
import subprocess
subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "-r", "backend/requirements.txt"])
subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "-r", "skills/ppt-master/requirements.txt"])

print("🚀 启动 PPT-Web: http://localhost:8000")
subprocess.call([sys.executable, "backend/run.py", "--no-reload"])
