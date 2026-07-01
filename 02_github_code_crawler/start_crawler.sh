#!/bin/bash
# GitHub 代码爬虫启动脚本

echo "=========================================="
echo "GitHub 代码爬虫"
echo "=========================================="
echo ""

# 检查Python版本
python3 --version
echo ""

# 检查依赖
echo "检查依赖..."
pip3 install -r requirements.txt
echo ""

# 创建输出目录
mkdir -p crawled_data
echo "输出目录: crawled_data/"
echo ""

# 启动爬虫
echo "启动爬虫..."
echo "按 Ctrl+C 可以安全中断（会保存进度）"
echo ""

python3 github_crawler_v3_enhanced.py

echo ""
echo "爬虫已停止"
