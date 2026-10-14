# Windows 设备电量 / Windows Battery

一个 AstrBot 插件：用一条指令查看运行 AstrBot 的 Windows 计算机当前剩余电量，
**回复内容和用户名都可以自定义**。

> 仅对 **Windows 本机部署** 的 AstrBot 有效。若 AstrBot 跑在 Linux 服务器或 Docker 里，
> 读到的是宿主机状态，指令会提示系统不支持。

## 使用

唤醒词 + `设备电量`，例如唤醒词是 `/` 时发送：

```
/设备电量
```

机器人回复（用户名填「用户」时）：

```
用户，你的设备电量还有87。
```

### 指令一览

| 指令 | 作用 | 示例 |
|---|---|---|
| `设备电量` | 查询电量 | `/设备电量` |
| `设置电量模板` | 自定义回复内容 | `/设置电量模板 {username}，你的设备电量还有{percent}。` |
| `设置电量用户名` | 设置用户名 | `/设置电量用户名 用户` |
| `电量配置` | 查看当前用户名与模板 | `/电量配置` |

指令别名：`设备电量` = `电量` = `电池电量` = `battery`。

设置用户名填 `默认` 可恢复到插件配置页里的值；模板填 `默认` 可恢复默认模板。

### 模板占位符

| 占位符 | 含义 | 示例值 |
|---|---|---|
| `{username}` | 用户名 | 用户 |
| `{percent}` | 电量百分比（数字） | 87 |
| `{state}` | 充电状态 | 电源已接通 / 使用电池 |
| `{sender}` | 发送消息的人的昵称 | 小明 |

默认模板：`{username}，你的设备电量还有{percent}。`，输出「用户，你的设备电量还有87。」；
想带百分号就写 `{percent}%`，输出「用户，你的设备电量还有87%。」。

### 用户名取值规则（优先级从高到低）

1. 用 `设置电量用户名` 指令设置的名字；
2. 插件配置页里的「用户名 / 称呼」；
3. 开启「使用发送者昵称」时用对方昵称；
4. 以上都没有则显示「当前计算机」。

## 两种自定义方式

1. **改配置（推荐，长期生效）**：WebUI → 插件 → Windows 设备电量 → 配置，
   填 `用户名 / 称呼`、`回复模板`；
2. **用指令改（临时，立即生效）**：`设置电量模板` / `设置电量用户名`，
   设置保存在插件数据目录（`data/plugin_data/astrbot_plugin_windows_battery/`），
   卸载插件前一直有效。

可在配置里把 `允许通过指令修改` 关掉，防止被随意改动。

## 安装

1. 把整个 `astrbot_plugin_windows_battery` 文件夹放进 AstrBot 的 `data/plugins/` 目录；
2. 在 WebUI「插件管理」里找到该插件，点击**重载 / 启用**；
3. 若提示缺少依赖，AstrBot 会按 `requirements.txt` 安装 `psutil`，
   也可手动 `pip install psutil` 后重启。

目录结构：

```
astrbot_plugin_windows_battery/
metadata.yaml        # 插件元数据（必填）
main.py              # 插件主逻辑
_conf_schema.json    # 插件配置项（WebUI 配置页）
requirements.txt     # 依赖：psutil
README.md
```

## 实现说明

- 电量优先用 `psutil.sensors_battery()`；
- psutil 不可用或异常时，自动回退到 `ctypes` 直接调用 Windows 的 `GetSystemPowerStatus`，不依赖第三方库；
- 两者都失败（台式机、无电池、`BatteryLifePercent` 为 255 未知）时回复「未检测到电池信息」；
- 非 Windows 系统（Linux / macOS / Docker）会提示该插件仅支持 Windows；
- 读取放在独立线程执行，不阻塞 AstrBot 事件循环；
- 模板渲染用占位符替换而非 `str.format`，模板里写其它花括号不会报错；
- 运行时设置只写入插件数据目录，不写入插件自身目录，更新 / 重装插件不会丢失。
