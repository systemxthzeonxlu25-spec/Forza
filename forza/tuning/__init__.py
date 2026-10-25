"""调车诊断：给一圈（或几圈）数据，找出常见的调校问题，每条结论都写清楚证据和判断依据。

原则（用户的要求：别瞎猜）：
- 每条 finding 都带 evidence（实测数字 + 比较的阈值）和 basis（用了哪些通道、哪条规则、为什么能说明问题）。
- 数据不够就说“数据不足”；Data Out 本身给不了的东西列在 not_available 里，不硬下结论。
- 只看数值大小（绝对值），不依赖正负号：真实游戏各通道的正负号约定没有逐个核实。
  （左右弯方向来自方向盘输入 steer，见 corners.py。）
- 阈值全部在 tuning_rules.toml（带中文说明），文件缺失时用这里的 DEFAULT_RULES。

对外接口：
    diagnose(lap, rules=None, corners=None, *, abs_on=None, aero=None) -> dict
        单圈诊断。corners = 外部给的弯道表（例如 sectors.layout_corners 映射好的固定弯道表），
        不给就用 corners.detect_corners 自己识别；abs_on / aero = 用户在页面上勾选的“我开了 ABS”/“有可调空力”
        （None = 没说），只影响刹车 / 平衡结论的解读和措辞。
    diagnose_many(laps, rules=None, corners=None, *, abs_on=None, aero=None) -> dict
        多圈合并（同车同赛道），每圈都出现的问题置信度更高；corners 是和 laps 一一对应的列表（元素可为 None）
    rules_info() / get_rules()                 当前阈值（tuning_rules.toml 改了会自动重新读取）
    track_outline(lap, max_points)             画赛道小地图用的降采样坐标

检查项（都在下面各自的 _check_xxx 函数里，好找好改）：
    转向平衡（按弯道阶段的转向不足 / 转向过度）、悬挂触底 / 伸到底、胎温不平衡、
    出弯空转（含内侧 / 外侧轮、四驱前后轴）、刹车抱死、刹车平衡（直线重刹前后轴滑移）、
    刹车压力（踏板-减速度饱和）、差速器（入弯松刹内侧轮 / 四驱前后分配）、齿比（最高挡撞断油 / 转速余量）、
    降挡超转（降挡太早）。

分析块（不是 finding，是给页面画图的量化数据，见 _balance_block / _brake_block / _body_block）：
    balance_map    平衡指数 B = 前轴 |侧偏角| − 后轴 |侧偏角|，按车速分档 + 弯 × 入弯/弯中/出弯 热力表 + 解读句
    brake_analysis 直线重刹段前后轴滑移对比、踏板-减速度曲线（按车速分档）
    body           车身与悬挂：四轮行程分位数、悬挂速度直方图、侧倾 / 点头梯度

代码结构（以前是一个 2600 多行的 tuning.py，按章节拆成了这个包，读某一项检查只用看一个文件）：
    rules.py                 阈值：默认值 + 读 forza/tuning_rules.toml
    common.py                共用常量、小工具、单圈上下文 _Ctx（所有检查函数的输入）
    quality.py               数据质量检查、弯道识别
    check_balance.py         ① 转向平衡
    check_suspension.py      ② 悬挂触底 / 伸到底
    check_tires.py           ③ 胎温
    check_traction.py        ④ 出弯空转
    check_brakes.py          ⑤ 刹车抱死、⑤b 刹车平衡
    check_brake_pressure.py  ⑤c 刹车压力（以及按车速分档的工具函数）
    check_differential.py    ⑤d 差速器
    check_gearing.py         ⑥ 齿比
    check_downshift.py       ⑦ 降挡超转（降挡太早）
    blocks.py                三个分析块（平衡指数分布 / 刹车分析 / 车身与悬挂）
    diagnosis.py             主流程：跑检查 → 交叉印证 → 拼结果；diagnose / diagnose_many / track_outline

加一项新检查：新建 check_xxx.py 写 _check_xxx(ctx)，加进 diagnosis.CHECKS，
在 common.CHECK_LIST 里登记 (id, 名字)，阈值写进 rules.DEFAULT_RULES 和 forza/tuning_rules.toml。
依赖方向只有一个：common / rules ← check_* ← blocks ← diagnosis（检查项之间尽量不互相引用）。
"""
from __future__ import annotations

# ---- 对外接口（网页路由、调校版本对比、固定弯道表都只用这些）
from .common import CHECK_LIST, CHECK_NAME, CONF_LEVELS, NOT_AVAILABLE
from .diagnosis import diagnose, diagnose_many, track_outline
from .rules import DEFAULT_RULES, RULES_PATH, get_rules, load_rules, rules_info

# ---- 内部函数：测试里直接调来验证细节，所以在包顶层留个引用（业务代码别用）
from .check_brake_pressure import _band_defs, _band_mask, _band_shares, _speed_edges  # noqa: F401
from .diagnosis import _rules_info, _run  # noqa: F401

__all__ = [
    "CHECK_LIST", "CHECK_NAME", "CONF_LEVELS", "NOT_AVAILABLE", "DEFAULT_RULES", "RULES_PATH",
    "diagnose", "diagnose_many", "track_outline", "get_rules", "load_rules", "rules_info",
]
