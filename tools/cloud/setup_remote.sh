#!/bin/sh
# 在云主机上执行一次（Ubuntu 22.04/24.04）：装 python3 与 CPU 版 torch。
# 用法（本机）：ssh user@host 'bash -s' < tools/cloud/setup_remote.sh
set -eu
sudo apt-get update -y && sudo apt-get install -y python3 python3-pip rsync
python3 -m pip install --user --upgrade pip
# 依赖走阿里云内网 PyPI 镜像（快）；torch 本体只有官方 CPU 源有，--no-deps 避免依赖也走海外慢线路。
python3 -m pip install --user -i https://mirrors.cloud.aliyuncs.com/pypi/simple/ filelock typing-extensions sympy networkx fsspec "setuptools>=77"
python3 -m pip install --user --no-deps torch --index-url https://download.pytorch.org/whl/cpu
python3 -c "import torch, os; print('torch', torch.__version__, 'cores', os.cpu_count())"
