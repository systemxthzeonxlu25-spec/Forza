# 内置参考数据

| 文件 | 内容 | 来源 / 许可证 |
|---|---|---|
| `fm2023_cars.csv` | FM (2023) 车辆编号 → 年份、品牌、型号（`编号,年份,品牌,型号`） | [bluemanos/forza-motorsport-car-track-ordinal](https://github.com/bluemanos/forza-motorsport-car-track-ordinal) `fm8/cars.csv`，MIT（见 `LICENSE-forza-motorsport-car-track-ordinal`） |
| `fm2023_tracks.csv` | FM (2023) 赛道编号 → 赛道、地点、国家、布局、长度 km（`编号,赛道,地点,国家代码,布局,长度km`） | 同上，`fm8/tracks.csv` |

导入时的上游版本：`7989547cb5ac61dc1a7b6c1d5bc14a75beb6df90`。原样复制，没有改动。

用途：`forza/names.py` 读取，给 FM (2023) 的车和赛道起默认名字（网页上显示真实名字而不是“车 #2240”“赛道 #860”）。
用户在网页上改过的名字不会被覆盖。别的游戏（FM7 / Horizon）的编号不保证和这份表一致，不使用。

更新：把上游的两个 csv 重新复制过来、改掉上面的版本号即可。
