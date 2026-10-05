"""mj.sim：阶段一模拟决策地基（docs/SIM_FOUNDATION_REPORT.md）。

纯标准库；import 时不做任何 I/O、不写文件、不读令牌。子模块：
  engine.py    规则状态机（G1），与服务端逐事件核对，见 tools/sim_replay_check.py。
  snapshot.py  引擎状态 -> 生产快照（G2.1），供 mj.bot.choose_action 直接消费。
  fastcheck.py 查表版判胡/距离（G4），阶段二蒙特卡洛推演用。
"""
