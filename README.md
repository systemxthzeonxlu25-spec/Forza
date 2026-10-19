# Forza 遥测分析工具

给 **Forza Motorsport（FM7 / FM 2023）** 和 **Forza Horizon（FH4 / FH5 / FH6）** 用的练车、调车小工具。游戏在电脑上全屏跑，手机浏览器当副屏看实时数据；跑完的每一圈自动存档，可以两圈叠加对比、看调车诊断。

- **实时面板**（手机友好）：车速、转速、档位、油门/刹车/方向、四轮胎温、悬挂行程、滑移率、G 值、实时圈速差
- **自动切圈 + 存档**：用游戏给的圈数 / 圈时字段自动切圈，每圈存进本地 SQLite
- **两圈对比**：按赛道距离叠加速度、油门、刹车曲线，自动切出弯道，告诉你在哪个弯慢了、慢在哪
- **调车诊断**：转向不足/过度、悬挂触底、胎温不均、出弯打滑、刹车抱死、齿比，每条都写明判断依据
- **录制 / 回放 / 假数据**：把原始 UDP 包录成文件，不开游戏也能开发调试

## 页面一览

| 地址 | 用途 |
|---|---|
| `/`（实时面板） | 手机副屏：车速、档位、转速、输入、四轮状态、G 球、圈速和实时差距 |
| `/laps.html`（圈速对比） | 圈列表 + 两圈按距离叠加对比 + 逐弯分析 + 赛道图 |
| `/tuning.html`（调车诊断） | 单圈 / 多圈合并诊断，每条都附数据和判断依据 |
| `/connect.html`（连接手机） | 手机访问地址的二维码、游戏里该填什么 |


---

## 1. 安装（Windows）

1. 装 **Python 3.11 或更新版本**（[python.org](https://www.python.org/downloads/)，安装时勾选 *Add python.exe to PATH*）。
2. 在项目文件夹里打开 PowerShell：

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

3. 启动：

```powershell
python -m forza serve
```

启动后终端会打印类似：

```
 Forza 遥测已启动
  UDP 监听：0.0.0.0:5555
  游戏 Data Out 填：IP = 127.0.0.1 或 192.168.1.23，端口 = 5555
  电脑浏览器：http://localhost:8000
  手机浏览器：http://192.168.1.23:8000
```

以后每次用只要 `.venv\Scripts\activate` 然后 `python -m forza serve`。

**更省事的办法**：直接双击项目里的 `start.bat`。第一次运行会自动创建虚拟环境、安装依赖，以后双击就直接启动。额外参数也能传进去，例如在命令行里运行 `start.bat --game fh6 --record`。

## 2. 游戏里打开 Data Out

| 游戏 | 菜单位置 | 要改的设置 |
|---|---|---|
| **FH4 / FH5 / FH6** | 设置 → **HUD and Gameplay**（HUD 和游戏玩法）→ 拉到最底 | Data Out = **On**，Data Out IP Address = 见下文，Data Out IP Port = **5555**。Horizon 不用选包格式 |
| **FM (2023)** | 设置 → **Gameplay & HUD** → *UDP Race Telemetry* 一栏 | Data Out = **On**，IP、端口同上，**Data Out Packet Format = Car Dash** |
| **FM7** | 设置 → **HUD** 选项页 | Data Out = **On**，IP 填**本机局域网 IP**（FM7 官方说明不支持 127.0.0.1），端口 5555，**Packet Format = Car Dash** |

- 中文界面下菜单名可能略有不同，找 *Data Out / 数据输出* 相关的几项。
- FM 一定要选 **Car Dash**：Sled 格式没有车速、胎温、圈速、油门刹车，面板只能显示一小部分，也切不了圈。
- 游戏只在**真正开车时**发数据（FH6 在菜单里会完全停发），所以菜单里看到"没有收到数据"是正常的。

### IP 填什么

- **游戏和本工具在同一台电脑**：先填 `127.0.0.1`。
- 面板一直显示"没有收到数据"，就改填**本机局域网 IP**。启动时终端会打印这个 IP，网页的"连接手机"页也有，一般是 `192.168.x.x`。
- 还是收不到，而你的游戏是 **Microsoft Store / Xbox App（Game Pass）版**，大概率是 Windows 对商店应用的"回环隔离"挡住了，需要给游戏加回环豁免。用**管理员** PowerShell 运行：

```powershell
# 1. 找到游戏的 PackageFamilyName（FH5 的包名不含 forza，所以多匹配几个关键词）
Get-AppxPackage | Where-Object { $_.Name -match "forza|624F8B84B80|SunriseBaseGame|ApolloBaseGame" } | Select-Object Name, PackageFamilyName

# 2. 用上一步查到的 PackageFamilyName 加豁免
CheckNetIsolation LoopbackExempt -a -n="<PackageFamilyName>"
```

  社区报告过的包名：FH4 是 `Microsoft.SunriseBaseGame_8wekyb3d8bbwe`，FH6 是 `Microsoft.ForzaHorizon6_8wekyb3d8bbwe`。其他版本以你自己查到的为准。社区说 Steam 版不需要这一步。
- 在 **Xbox 主机**上玩也可以用：IP 填运行本工具的电脑的局域网 IP。

### 端口

默认 **5555**，游戏里填的端口必须和本工具一致。想改的话用 `python -m forza serve --udp-port 端口号`，或者写进 `config.toml`。

FH6 官方文档要求**避开 5200–5300**，因为游戏自己会占用这一段的端口。很多教程里写的 5300 正好落在这个范围里。

## 3. 用手机打开实时面板

1. 手机和电脑连**同一个 Wi-Fi / 局域网**。
2. 手机浏览器打开 `http://电脑局域网IP:8000`。启动时终端打印的"手机浏览器"那一行就是这个地址。也可以在电脑上打开 `http://localhost:8000/connect.html`，用手机扫二维码。
3. 打不开的话，多半是 **Windows 防火墙**拦了：
   - 第一次运行时 Windows 会弹窗询问，要勾选"专用网络"并允许。
   - 没弹窗或者点错了，用**管理员** PowerShell 加一条规则：

     ```powershell
     New-NetFirewallRule -DisplayName "Forza Telemetry Web" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private
     ```

   - 确认当前网络是"专用网络"：设置 → 网络和 Internet → WLAN / 以太网 → 网络配置文件类型 → 专用。
4. 面板右上角有全屏按钮。局域网 http 页面一般没法阻止手机息屏，建议把手机的自动锁屏时间调长。iPhone 的 Safari 不支持网页全屏，可以用"添加到主屏幕"后从桌面图标打开。

### 实时面板上都有什么

- **顶部转速条**：到最高转速的 92% 变黄，97% 闪红，该换挡了；右边是转速数字。
- **挡位 + 车速**：点一下车速可以在 km/h 和 mph 之间切换。左边竖条是刹车，右边是油门，底下横条是方向。
- **四个轮胎**：按车上的位置摆放（左前、右前 / 左后、右后）。
  - 大字是胎温（°C），颜色从冷到热依次是蓝、绿、黄、红。颜色阈值是 `web/live.js` 顶部的常量，只是通用参考值，不是 Forza 官方数字。
  - 旁边的竖条是悬挂行程，≥ 95% 变红，表示快压到底了。
  - "滑移 / 侧偏"是归一化滑移率和侧偏角，绝对值 > 1 标红，表示这个轮已经失去抓地。
  - FM2023 还会显示轮胎磨损。
- **圈速面板**：大号数字是**实时差距**，拿当前这一圈和本场最快完整圈在同一距离上比用时，绿色表示比最快圈快，红色表示慢。下面依次是本圈用时、上圈、最快、预计圈时、第几圈、名次。
- **G 球**：横向 / 纵向 G，带最近 1.5 秒的轨迹。
- **底部状态栏**：识别出的游戏、包格式、每秒包数。
- 右上角 ☰ 菜单可以切换到其他页面，也能设置刷新频率（10 / 20 / 30 Hz）。
- 状态提示：没连上服务、没收到数据（会提示 UDP 端口和本机 IP）、暂停 / 菜单、FM 选了 Sled 格式（提示改成 Car Dash）、收到未知包长等。

## 4. 录制、回放、假数据

```powershell
python -m forza serve --record                 # 边用边录原始包，存到 recordings\
python -m forza record -o mylap.frec           # 只录不开网页
python -m forza info mylap.frec                # 看录像：包长分布、识别出的游戏、能切出几圈
python -m forza serve --replay mylap.frec --loop   # 不开游戏，用录像驱动整个网页
python -m forza replay mylap.frec --to 127.0.0.1:5555   # 用 UDP 重发，测整条网络链路
python -m forza import mylap.frec              # 离线导入：切圈后存进数据库
python -m forza fake -o samples\demo.frec      # 生成假数据（5 圈，带几个故意埋的调车问题）
python -m forza fake --format fh --scenario sprint   # Horizon 冲刺赛假数据
```

`.frec` 存的是**原始包**。以后解析逻辑改了，老录像照样能重新解析。文件名以 `.gz` 结尾会自动压缩。

假数据的赛道叫 "Fake Ring"（3862 m，10 个弯），车是一台后驱车。默认会埋几个问题，用来验证圈对比和诊断功能：第 2 圈 T3 刹车早、T7 推头、T5 出弯甩尾打滑、直道颠簸触底、T1 前轮抱死、直道末端撞断油、左前胎偏热。加 `--clean` 生成没有问题的干净数据。

## 5. 支持的游戏和包格式

程序按收到的**包长度**自动识别格式。所有字段都是小端序。

| 包长 | 识别为 | 结构 |
|---|---|---|
| 232 | FM7 / FM2023 的 **Sled** 格式 | 只有物理量（G 值、悬挂、滑移、转速、车辆信息） |
| 311 | **FM7** Car Dash | Sled + Dash（位置、车速、胎温、圈速、油门刹车方向……） |
| 323 / 324 | **Forza Horizon 4 / 5 / 6** | Sled + 12 字节 Horizon 专有字段 + Dash（+1 字节尾巴） |
| 331 | **Forza Motorsport (2023)** Car Dash | Sled + Dash + 四轮磨损 + 赛道 ID |

- 324 字节的包分不出是 FH4、FH5 还是 FH6，三代布局完全一样。想在记录里标清楚的话，启动时加 `--game fh6`（或 fh4 / fh5）。
- FH3 及更早、FM6 及更早**没有 Data Out**，用不了这个工具。
- **没见过的包长**（比如以后游戏加了字段）：程序会用"Speed 字段 ≈ 速度向量长度"去试 FM 和 FH 两种布局，自动选对得上的那个，并在页面上提示。已知包长也会持续做这个校验，万一游戏改了布局能及时发现。
- 胎温：官方文档原文没能确认单位，社区共识是华氏度，程序默认按 °F 转成 °C。如果发现明显不对，可以用 `--tire-unit C` 或 `config.toml` 改。
- 格式依据：官方 [FM Data Out 文档](https://support.forza.net/hc/en-us/articles/21742934024211-Forza-Motorsport-Data-Out-Documentation)、[FH6 Data Out 文档](https://support.forza.net/hc/en-us/articles/51744149102611-Forza-Horizon-6-Data-Out-Documentation)，以及 [geeooff/forza-data-web](https://github.com/geeooff/forza-data-web)、[richstokes/Forza-data-tools](https://github.com/richstokes/Forza-data-tools)、[TheBanHammer/fh6-tel](https://github.com/TheBanHammer/fh6-tel) 等开源实现交叉核对。

## 6. 自动切圈规则

全部基于游戏自己给的字段：

- `IsRaceOn = 0`（菜单、暂停）：跳过这些帧，不打断当前这一圈。
- `LapNumber` + 1：上一圈结束。圈时直接取游戏给的 `LastLap`。
- 比赛时间小幅回退 = **倒带**：删掉被倒掉的那段数据，接着记。倒过了起终点线，就把上一圈拿回来接着记。
- 比赛时间回到 0 = **重开**：没跑完的那一圈丢弃。
- **FH 冲刺赛**（全程 LapNumber = 0 且有名次）：整段存成一条"冲刺"记录。是否跑完的判断方法是：和这条路线跑过的最长距离比，达到 95% 以上算跑完。第一次跑某条路线时没有参照，只能先当作跑完；以后跑了更长的距离，会自动把之前那条改标成"放弃"。
- **FH 自由漫游**（没有比赛时钟）：只能看实时面板，不切圈。
- 比赛最后一圈冲线时游戏如果没让 LapNumber + 1，就按距离判断：达到本场整圈的 95% 算完整圈，圈时自己量。
- 赛道识别：FM2023 用赛道 ID；其他游戏的包里没有赛道 ID，用"起终点位置 + 圈长"识别。名字可以在网页上改。

## 7. 圈速对比怎么看

打开 `/laps.html`，先选赛道和车。列表里每圈前面有 **A / B** 两个选择钮：A 是参考圈，B 是要分析的圈。默认是"最新一圈 对比 最快圈"。

- **顶部总结**：B 比 A 慢（快）多少，主要丢在哪几个弯。
- **曲线**：横轴是赛道距离，从上到下依次是：
  - 累计时间差 Δt：往上走表示 B 在这段丢时间，往下走表示赚时间
  - 车速
  - 油门（向上画）/ 刹车（向下画）
  - 方向
  - 档位
  
  各图的光标是同步的。在图上拖一段可以放大，双击还原。
- **逐弯分析**：按丢时多少排序。每个弯一句话说明慢在哪，比如"刹车点早 24 m，刹车更轻，弯心最低速低 7 km/h"，下面附 A/B 两圈的原始数字。点一个弯，曲线会直接放大到那里。
- **赛道图**：颜色表示 B 在哪里丢时间（红）、在哪里赚时间（青）。鼠标在曲线上移动时，赛道图上会有个点跟着走。

对齐方式：两圈长度不可能完全一样，所以先把 B 的距离按比例缩放到 A 的圈长，再逐米对比。局部几米的走线差异会留在结果里。

解释句只根据数字生成，差别小于阈值的项不提。阈值是 `forza/analysis.py` 顶部的常量。

## 8. 调车诊断怎么看

在圈列表点"诊断"，或者直接打开 `/tuning.html?lap=圈号`。勾选"合并最近 N 圈"可以多圈一起诊断：每圈都出现的问题置信度会提高，偶尔出现一次的会降级。

| 检查项 | 依据（Data Out 通道） |
|---|---|
| 转向不足 / 过度（分入弯、弯中、出弯） | 前后轴归一化侧偏角：哪根轴先超过 1.0（抓地极限）且明显比另一根大 |
| 悬挂触底 / 伸到底 | 归一化悬挂行程 ≥ 0.98 / ≤ 0.02，连续多帧；同时压路肩的单独标为"路肩" |
| 胎温左右 / 前后不均 | 热胎后的平均胎温；会算出这条赛道左右弯的比例，扣掉赛道方向本身造成的温差 |
| 出弯打滑 | 给油时驱动轮（按驱动形式）滑移率 > 1.0 |
| 刹车抱死 | 刹车时滑移率 < −1.0，看前轴和后轴谁先抱死 |
| 齿比 | 最高挡顶着断油转速跑了多久 |

- 每条结论都包含：实测数字、比较用的阈值、判断依据（展开"判断依据"可见）、常见调整方向、置信度。
- 数据不够时，结论就是"数据不足"，不会硬下结论。
- **Data Out 判断不了的东西会单独列出来**。最重要的一条：**每个轮胎只给一个温度，没有胎面内/中/外三点温度，所以没法判断外倾角合不合适**，这个要看游戏自带的遥测轮胎页面。胎压、当前调校数值、挡位数量等也都不在包里。
- 所有阈值都在 `forza/tuning_rules.toml`，每项都有中文注释，改完保存后下一次诊断就会生效。目前这些阈值只用假数据校准过，真实游戏数据如果偏敏感或偏迟钝，按实际情况调。

## 9. 开发

```powershell
pip install -r requirements-dev.txt
python -m pytest -q
```

目录结构：

```
forza/
  frame.py        统一数据结构 Frame（单位、四轮顺序、各字段含义）
  packet.py       包格式注册表 + 解析 / 编码 + 未知包长自动探测
  profiles.py     各代游戏差异（有没有赛道 ID / 冲刺赛 / 倒带……）
  recorder.py     .frec 录制 / 读取        replay.py   回放 / 导入
  udp.py hub.py   收包 → 解析 → 分发给订阅者
  laps.py         自动切圈 + 存档           lapdata.py  单圈列式数据
  storage.py      SQLite                    corners.py  弯道识别
  live.py         实时推送（WebSocket）     analysis.py 两圈对比
  tuning.py       调车诊断（阈值在 tuning_rules.toml）
  api/            各模块的 HTTP 路由         fake/       假数据生成器
web/              前端（原生 JS，无需构建；uPlot 等依赖已放在 web/vendor）
tests/            pytest，全部基于假数据 / 手搓帧，不需要游戏
```

常见扩展：

- **游戏出了新包格式**：在 `forza/packet.py` 里照着现有字段表加一个 `PacketFormat`，再在 `profiles.py` 里加游戏配置。
- **切圈要多记一个通道**：在 `lapdata.py` 的 `CHANNELS` 和 `frame_sample()` 各加一项。
- **加一条诊断规则**：在 `tuning.py` 里照着现有检查写一个函数，阈值放进 `tuning_rules.toml`。
- **拿真实数据校准**：用 `python -m forza serve --record` 跑几圈，录像放进 `samples/`。之后 `python -m forza info` 看切圈结果，`python -m forza import` 导入后在网页上看对比和诊断。

## 10. 已知限制

- 本工具是在没有游戏的环境下开发的，所有功能都只用假数据和公开的包格式验证过。第一次接真实游戏时最可能需要微调的是：FH 冲刺赛 / 比赛结束的判定、诊断阈值、胎温单位。
- 324 字节的包分不出 FH4、FH5 还是 FH6，要手动用 `--game` 标注。
- FH6 那 12 字节专有字段（CarGroup 等），类型没有核实，只存原始值，没有参与分析。
- 冲刺赛第一次跑某条路线时，没法知道是跑完了还是中途放弃，要等跑过一次更长的才能纠正。

