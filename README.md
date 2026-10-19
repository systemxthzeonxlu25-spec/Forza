# Forza 遥测分析工具

给 **Forza Motorsport（FM7 / FM 2023）** 和 **Forza Horizon（FH4 / FH5 / FH6）** 用的练车、调车小工具。游戏在电脑上全屏跑，手机浏览器当副屏看实时数据；跑完的每一圈自动存档，可以两圈叠加对比、看调车诊断。

- **实时面板**（手机友好）：车速、转速、档位、油门/刹车/方向、四轮胎温、悬挂行程、滑移率、G 值、实时圈速差
- **自动切圈 + 存档**：用游戏给的圈数 / 圈时字段自动切圈，每圈存进本地 SQLite
- **两圈对比**：按赛道距离叠加速度、油门、刹车曲线，自动切出弯道，告诉你在哪个弯慢了、慢在哪
- **调车诊断**：转向不足/过度、悬挂触底、胎温不均、出弯打滑、刹车抱死、齿比，每条都写明判断依据
- **录制 / 回放 / 假数据**：把原始 UDP 包录成文件，不开游戏也能开发调试

<!-- PAGES -->

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
4. 面板右上角有全屏按钮。局域网 http 页面一般没法阻止手机息屏，建议把手机的自动锁屏时间调长。

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

<!-- DEV -->
