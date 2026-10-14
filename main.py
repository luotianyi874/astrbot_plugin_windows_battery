"""
astrbot_plugin_windows_battery

功能：
    查询运行 AstrBot 的 Windows 计算机当前电池剩余电量，
    并支持自定义回复模板与用户名。

指令：
    唤醒词 + 设备电量                 -> /设备电量
    唤醒词 + 设置电量模板 <模板>       -> /设置电量模板 {username}，你的设备电量还有{percent}。
    唤醒词 + 设置电量用户名 <名字>     -> /设置电量用户名 用户
    唤醒词 + 电量配置                  -> /电量配置    查看当前用户名与模板

模板占位符：
    {username}  用户名（配置项 user_name，留空时可用发送者昵称兜底）
    {percent}   电量百分比，如 87
    {state}     充电状态：电源已接通 / 使用电池
    {sender}    发送消息的人的昵称

数据持久化：
    运行时设置只写入 StarTools.get_data_dir() 返回的插件数据目录，
    绝不使用插件自身目录，避免更新 / 重装插件时数据被覆盖。
"""

import asyncio
import json
import platform
import re
from pathlib import Path
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    import psutil
except ImportError:  # 依赖缺失时仅记录日志，不让插件加载失败
    psutil = None


PLUGIN_NAME = "astrbot_plugin_windows_battery"
DEFAULT_TEMPLATE = "{username}，你的设备电量还有{percent}。"
DEFAULT_USER_NAME = "当前计算机"

# 指令名与别名，用于从原始消息中剥离指令名取参数
CMD_SET_TEMPLATE = ("设置电量模板", "电量模板")
CMD_SET_NAME = ("设置电量用户名", "电量用户名")


@register(
    PLUGIN_NAME,
    "luotianyi874",
    "查询 Windows 计算机电池剩余电量，回复内容与用户名可自定义",
    "v1.0.0",
)
class WindowsBatteryPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.runtime = self._load_runtime()

    # ---------------- 指令 ----------------

    @filter.command("设备电量", alias=["电量", "电池电量", "battery"])
    async def device_battery(self, event: AstrMessageEvent):
        """查询运行 AstrBot 的 Windows 计算机当前电池剩余电量"""
        percent, state = await asyncio.to_thread(self._read_battery)

        if percent is None:
            yield event.plain_result(f"无法获取电量：{state}")
            return

        username = self._resolve_username(event)
        text = self._render(
            self._template(),
            username=username,
            percent=percent,
            state=state,
            sender=self._sender_name(event),
        )
        yield event.plain_result(text)

    @filter.command("设置电量模板")
    async def set_template(self, event: AstrMessageEvent):
        """设置电量回复模板，如：/设置电量模板 {username}，你的设备电量还有{percent}。"""
        if not self._get("allow_runtime_edit", True):
            yield event.plain_result("当前未开启通过指令修改，请到插件配置页修改。")
            return

        template = self._extract_arg(event, CMD_SET_TEMPLATE)
        if not template:
            yield event.plain_result(
                "请输入模板，可用占位符：{username} 用户名、{percent} 电量、"
                "{state} 充电状态、{sender} 发送者昵称。\n"
                "示例：/设置电量模板 {username}，你的设备电量还有{percent}。\n"
                "恢复默认请输入：/设置电量模板 默认"
            )
            return

        if template == "默认":
            self.runtime.pop("reply_template", None)
        else:
            self.runtime["reply_template"] = template

        if not self._save_runtime():
            yield event.plain_result("保存失败，请查看 AstrBot 日志了解详情。")
            return

        preview = self._render(
            self._template(),
            username=self._resolve_username(event),
            percent=87,
            state="使用电池",
            sender=self._sender_name(event),
        )
        tip = "模板已恢复默认" if template == "默认" else "模板已保存"
        yield event.plain_result(f"{tip}，效果预览：\n{preview}")

    @filter.command("设置电量用户名")
    async def set_username(self, event: AstrMessageEvent):
        """设置回复中的用户名，如：/设置电量用户名 用户"""
        if not self._get("allow_runtime_edit", True):
            yield event.plain_result("当前未开启通过指令修改，请到插件配置页修改。")
            return

        name = self._extract_arg(event, CMD_SET_NAME)
        if not name:
            yield event.plain_result(
                "请输入用户名。示例：/设置电量用户名 用户\n"
                "恢复默认请输入：/设置电量用户名 默认"
            )
            return

        if name == "默认":
            self.runtime.pop("user_name", None)
            if not self._save_runtime():
                yield event.plain_result("保存失败，请查看 AstrBot 日志了解详情。")
                return
            yield event.plain_result("用户名已恢复为插件配置中的设置。")
            return

        self.runtime["user_name"] = name
        if not self._save_runtime():
            yield event.plain_result("保存失败，请查看 AstrBot 日志了解详情。")
            return

        preview = self._render(
            self._template(),
            username=name,
            percent=87,
            state="使用电池",
            sender=self._sender_name(event),
        )
        yield event.plain_result(f"用户名已保存为「{name}」，效果预览：\n{preview}")

    @filter.command("电量配置")
    async def show_config(self, event: AstrMessageEvent):
        """查看当前电量插件的用户名与回复模板"""
        username = self._resolve_username(event)
        template = self._template()
        preview = self._render(
            template,
            username=username,
            percent=87,
            state="使用电池",
            sender=self._sender_name(event),
        )
        yield event.plain_result(
            f"当前用户名：{username}\n"
            f"当前模板：{template}\n"
            f"效果预览：{preview}"
        )

    # ---------------- 模板与用户名 ----------------

    def _template(self) -> str:
        """运行时设置的模板优先，其次用插件配置项"""
        template = self.runtime.get("reply_template")
        if not template:
            template = self._get("reply_template", DEFAULT_TEMPLATE)
        return template or DEFAULT_TEMPLATE

    def _render(self, template: str, username: str, percent, state: str, sender: str) -> str:
        """用占位符替换生成回复文本，用 replace 而非 format，模板里写别的花括号也不会报错"""
        if not template:
            template = DEFAULT_TEMPLATE
        text = str(template)
        for key, value in (
            ("{username}", username),
            ("{percent}", str(percent)),
            ("{state}", state),
            ("{sender}", sender),
            ("{name}", username),  # 兼容写法
            ("{电量}", str(percent)),  # 兼容中文占位符
        ):
            text = text.replace(key, value)
        return text

    def _resolve_username(self, event: AstrMessageEvent) -> str:
        """运行时设置 > 配置项 > 发送者昵称 > 默认称呼"""
        name = self.runtime.get("user_name")
        if name:
            return str(name)
        name = self._get("user_name", "")
        if name and str(name).strip():
            return str(name).strip()
        if self._get("use_sender_name", True):
            sender = self._sender_name(event)
            if sender:
                return sender
        return DEFAULT_USER_NAME

    @staticmethod
    def _sender_name(event: AstrMessageEvent) -> str:
        try:
            return event.get_sender_name() or ""
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 获取发送者昵称失败: {e}")
            return ""

    @staticmethod
    def _extract_arg(event: AstrMessageEvent, names) -> str:
        """从原始消息里剥掉唤醒词和指令名，取剩下的参数"""
        try:
            raw = (event.message_str or "").strip()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 读取消息内容失败: {e}")
            return ""
        # 去掉开头的唤醒词（/、!、# 等非文字字符）
        raw = re.sub(r"^[^\w\u4e00-\u9fff]+", "", raw)
        # 长指令名优先，避免「电量模板」被「设置电量模板」抢先匹配
        for name in sorted(names, key=len, reverse=True):
            if raw.startswith(name):
                return raw[len(name):].strip()
        # 未匹配到已知指令名：按第一个空白分割，取后半部分
        parts = raw.split(None, 1)
        return parts[1].strip() if len(parts) > 1 else ""

    # ---------------- 运行时设置的持久化 ----------------

    def _data_file(self) -> Optional[Path]:
        """获取插件数据目录下的设置文件路径，失败返回 None（不回退到插件目录）"""
        try:
            from astrbot.api.star import StarTools

            try:
                data_dir = StarTools.get_data_dir()
            except TypeError:
                data_dir = StarTools.get_data_dir(PLUGIN_NAME)
            data_dir = Path(data_dir)
            data_dir.mkdir(parents=True, exist_ok=True)
            return data_dir / "settings.json"
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{PLUGIN_NAME}] 无法获取插件数据目录，本次不持久化设置: {e}")
            return None

    def _load_runtime(self) -> dict:
        try:
            path = self._data_file()
            if path is not None and path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except (OSError, ValueError) as e:
            logger.warning(f"[{PLUGIN_NAME}] 读取运行时设置失败: {e}")
        return {}

    def _save_runtime(self) -> bool:
        """保存运行时设置，返回是否成功"""
        try:
            path = self._data_file()
            if path is None:
                return False
            path.write_text(
                json.dumps(self.runtime, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return True
        except (OSError, TypeError, ValueError) as e:
            logger.error(f"[{PLUGIN_NAME}] 保存运行时设置失败: {e}")
            return False

    def _get(self, key, default):
        """读插件配置项，配置缺失时返回默认值"""
        try:
            value = self.config.get(key, default)
        except (AttributeError, TypeError) as e:
            logger.debug(f"[{PLUGIN_NAME}] 读取配置项 {key} 失败: {e}")
            return default
        return default if value is None else value

    # ---------------- 电池读取 ----------------

    def _read_battery(self):
        """返回 (电量百分比 int | None, 状态说明 str)。百分比为 None 表示未取到。"""

        if platform.system() != "Windows":
            return None, f"当前系统为 {platform.system()}，该插件仅支持 Windows 计算机。"

        # 1) 优先用 psutil（内部同样是调用 Windows 的 GetSystemPowerStatus）
        if psutil is not None:
            try:
                battery = psutil.sensors_battery()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] psutil 读取电池失败: {e}")
                battery = None
            if battery is not None and battery.percent is not None:
                pct = int(round(battery.percent))
                if 0 <= pct <= 100:
                    return pct, self._power_state(battery.power_plugged)

        # 2) 兜底：直接用 ctypes 调 Windows API，不依赖任何第三方库
        pct, plugged = self._read_battery_winapi()
        if pct is not None:
            return pct, self._power_state(plugged)

        return None, "未检测到电池信息，这台计算机可能是台式机，或系统未提供电池状态。"

    @staticmethod
    def _read_battery_winapi():
        """通过 GetSystemPowerStatus 读取电量，返回 (百分比 | None, 是否插电 | None)"""
        if platform.system() != "Windows":
            return None, None
        try:
            import ctypes

            class SYSTEM_POWER_STATUS(ctypes.Structure):
                _fields_ = [
                    ("ACLineStatus", ctypes.c_ubyte),
                    ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte),
                    ("Reserved1", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_ulong),
                    ("BatteryFullLifeTime", ctypes.c_ulong),
                ]

            status = SYSTEM_POWER_STATUS()
            if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
                return None, None

            if status.BatteryLifePercent == 255:  # 255 代表未知
                return None, None
            return int(status.BatteryLifePercent), status.ACLineStatus == 1
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] Windows API 读取电池失败: {e}")
            return None, None

    @staticmethod
    def _power_state(plugged: Optional[bool]) -> str:
        if plugged is None:
            return "未知"
        return "电源已接通" if plugged else "使用电池"
