# 云主机上跑 S3 搜索（128 vCPU）

## 1. 要拷贝的文件（不含令牌）
```
mj/                                  # 代码（纯标准库）
tools/                               # 含 tools/train/ tools/cloud/ tools/overlays/；不要带 tools/.cache/ 和 tools/models/（体积大且可重建）
tests/                               # 批处理第一步的单元测试
models/events/                       # 对局事件流（~650MB，搜索用它抽状态）
models/weights.json                  # 生产权重（搜索里"我方=生产"读它）
models/dealer_value.json  models/mc_opp_params.json  models/nn_discard.json  models/opp_profile.json  models/hazard_table.json
models/route_cal.json  models/rollout_cal.json  models/fit_weights.json  models/discard_fit_weights.json   # 生产代码按需读取
models/nn_policy.json                # 仅 diag 步骤用（S2 网络）
reports/sim_calibrate.json           # 对手校准的目标（mc_opp_params.json 已经是产物，这个只是复核用）
tools/.cache/train/masters.json      # 可选：build_resid / diag 用（没有就先跑 python3 tools/train/player_stats.py）
```
**不要拷贝**：`portal_cookie.txt`、任何 token 文件、`logs/`（含线上决策日志，S3 不需要）、`tools/.cache/`。
可以用：`rsync -av --exclude='.cache' --exclude='portal_cookie.txt' --exclude='logs' --exclude='tools/models' --exclude='__pycache__' ./ cloud:~/mahjong/`

## 2. 环境
Python 3.9+ 即可跑搜索（纯标准库）。训练（train 步）需要 `pip install torch`（CPU 版就行）。
脚本在 Linux 上自动：用 `nproc` 取核数（`MJ_JOBS` 没设时默认 6——**云上请显式 `MJ_JOBS=$(nproc)`**）、没有 caffeinate/taskpolicy 就不加前缀、
实战测速（性能核/能效核）只在 macOS 跑（verify 只验精度）。

## 3. 一条命令
```
cd ~/mahjong
MJ_JOBS=$(nproc) S3_STATES=100000 S3_SEARCH_MIN=240 bash tools/cloud/run_detached.sh s3 bash tools/train/s3_batch.sh
bash tools/cloud/run_detached.sh status        # 进度
cat reports/s3_summary.txt                     # 汇总
```
第一步 `bench` 用 1 分钟实测单次推演耗时并算出"每 1000 个状态要几小时"，**先看这一步再决定 S3_STATES 和 S3_SEARCH_MIN**。
断线/被杀/重启后重新执行同一条命令即可续跑（搜索结果 `tools/.cache/search/results.jsonl` 按状态 id 去重、追加）。

## 4. 带回本机
`tools/.cache/search/results.jsonl`（搜索结果，训练目标）和 `models/nn_resid.json`（如果在云上训练了）。
本机再 `python3 tools/train/train_resid.py --source both` / `bash tools/train/s3_batch.sh train verify arena` 即可。
