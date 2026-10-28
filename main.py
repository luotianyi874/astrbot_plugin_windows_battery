"""
astrbot_plugin_windows_battery

在 AstrBot 中查询「运行 AstrBot 的那台 Windows 电脑」还剩多少电，
并在电量低于阈值时自动提醒；查询回复与提醒文案都可以自定义。

指令（前面的唤醒词以 AstrBot 配置为准，默认是 /）：
    /设备电量                 查询本机当前电量
    /设置电量模板 <模板>       临时修改查询回复模板（发「默认」可还原）
    /设置电量用户名 <名字>     临时修改文案里的称呼（发「默认」可还原）
    /电量配置                 查看当前模板、称呼与提醒设置
    /电量提醒绑定             把当前会话加入低电量提醒
    /电量提醒解绑             把当前会话移出低电量提醒
    /电量提醒状态             查看提醒开关、阈值与订阅情况
    /电量提醒测试             立刻发一条测试提醒（管理员）

查询回复可用的占位符：
    {username}  称呼（配置项 user_name，留空时用发送者昵称兜底）
    {percent}   电量百分比，如 87
    {state}     供电状态：电源已接通 / 使用电池
    {sender}    发送消息的人的昵称

低电量提醒可用的占位符：
    {username}  称呼（主动提醒没有发送者，只会用配置项 user_name）
    {percent}   当前电量百分比
    {threshold} 触发提醒的电量阈值
    {state}     供电状态
    {time}      当前时间，如 2026-10-28 08:30:00

数据持久化：
    运行时设置与提醒订阅只写入 StarTools.get_data_dir() 返回的插件数据目录
    （data/plugin_data/astrbot_plugin_windows_battery），
    不使用插件自身目录，避免更新 / 重装插件时数据被覆盖。
"""

import asyncio
import json
import platform
import re
import time
from pathlib import Path
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

try:  # AstrBot v4
    from astrbot.api.event import MessageChain
except ImportError:  # 兼容早期版本的导入路径
    from astrbot.core.message.message_event_result import MessageChain

try:
    import psutil
except ImportError:  # 依赖缺失时降级为调用 Windows API，不让插件加载失败
    psutil = None


PLUGIN_NAME = "astrbot_plugin_windows_battery"
PLUGIN_VERSION = "v1.2.0"
DATA_FILE_NAME = "settings.json"

DEFAULT_USER_NAME = "当前计算机"
DEFAULT_REPLY_TEMPLATE = "{username}，你的设备电量还有{percent}%。"
DEFAULT_ALERT_TEMPLATE = "{username}，你的设备电量还有{percent}%，请及时充电。"

# 供电状态文案
STATE_PLUGGED = "电源已接通"
STATE_BATTERY = "使用电池"
STATE_UNKNOWN = "未知"

# 低电量提醒的默认参数
DEFAULT_ALERT_PERCENT = 20
DEFAULT_CHECK_INTERVAL = 60
DEFAULT_REPEAT_MINUTES = 30
MIN_CHECK_INTERVAL = 10
MAX_CHECK_INTERVAL = 3600
MAX_ALERT_PERCENT = 99
MAX_SUBSCRIBERS = 50
STARTUP_DELAY = 5
# 电量回升到「阈值 + 该值」以上，才重新允许提醒，避免在阈值附近反复打扰
RECOVER_HYSTERESIS = 5

# 自适应检测节奏：只在「真的可能触发提醒」时才按 check_interval 读电量。
# 每次读取只是一次 GetSystemPowerStatus 系统调用，不读磁盘、不发网络请求，
# 但能少唤醒一次 CPU 就少一次，所以电量充足 / 插着电 / 没人订阅时都会自动放慢。
IDLE_INTERVAL = 60  # 没开提醒或还没有订阅会话：不读电量，只定期回来看配置
MID_INTERVAL = 120  # 接近阈值、或已经提醒过：2 分钟看一次
SLOW_INTERVAL = 300  # 电量还很足、插着电、读不到电池：5 分钟看一次
QUIET_MARGIN_FAR = 20  # 电量比阈值高 20% 以上算「很足」

# 指令名与别名。同一组内的名字等价，长名字优先匹配。
CMD_BATTERY = ("设备电量", "电池电量", "电量", "battery")
CMD_SET_TEMPLATE = ("设置电量模板", "电量模板")
CMD_SET_NAME = ("设置电量用户名", "电量用户名")
CMD_CONFIG = ("电量配置",)
CMD_BIND = ("电量提醒绑定",)
CMD_UNBIND = ("电量提醒解绑",)
CMD_STATUS = ("电量提醒状态", "电量提醒")
CMD_TEST = ("电量提醒测试",)

# 兜底指令路由：指令文本 -> 处理方法名
_COMMAND_ROUTES = (
    ("set_reply_template", CMD_SET_TEMPLATE),
    ("set_username", CMD_SET_NAME),
    ("show_config", CMD_CONFIG),
    ("bind_alert", CMD_BIND),
    ("unbind_alert", CMD_UNBIND),
    ("alert_status", CMD_STATUS),
    ("test_alert", CMD_TEST),
    ("battery", CMD_BATTERY),
)
# 长指令优先，避免「电量提醒状态」被别名「电量」抢先命中
_SORTED_COMMANDS = tuple(
    sorted(
        ((name, attr) for attr, names in _COMMAND_ROUTES for name in names),
        key=lambda item: len(item[0]),
        reverse=True,
    )
)

# 消息开头可能残留的 @ 标记：QQ 官方机器人 / 频道 / At 文本形式
_MENTION_NOISE = (
    r"(?:<@!?[^>\s]{1,64}>"
    r"|<qqbot-at-user[^>]{0,200}/?>"
    r"|\[At:[^\]]{0,64}\])"
)
# 唤醒前缀：/ ／ ! ！ # ＃
_WAKE_PREFIX_CHARS = "/／!！#＃"
_LEADING_NOISE_RE = re.compile(
    r"^\s*(?:" + _MENTION_NOISE + r"\s*|[" + _WAKE_PREFIX_CHARS + r"]\s*)+",
)

# 兜底匹配：仅当消息仍然带着唤醒前缀或 @ 标记时才命中，
# 因此不会和正常的 @filter.command 重复回复，也不会让群里的普通聊天误触发。
_FALLBACK_CMD_ALT = "|".join(re.escape(name) for name, _ in _SORTED_COMMANDS)
_FALLBACK_REGEX = (
    r"(?i)(?:"
    r"^\s*(?:" + _MENTION_NOISE + r")?\s*[" + _WAKE_PREFIX_CHARS + r"]\s*(?:"
    + _FALLBACK_CMD_ALT
    + r")(?=\s|$)"
    r"|^\s*(?:" + _MENTION_NOISE + r")\s*[" + _WAKE_PREFIX_CHARS + r"]?\s*(?:"
    + _FALLBACK_CMD_ALT
    + r")(?=\s|$)"
    r")"
)


def _strip_leading_noise(text: str) -> str:
    """去掉消息开头残留的 @ 标记、唤醒前缀与空白，得到干净的命令文本。"""
    text = str(text or "")
    for _ in range(4):
        stripped = _LEADING_NOISE_RE.sub("", text, count=1).strip()
        if stripped == text:
            break
        text = stripped
    return text


@register(
    PLUGIN_NAME,
    "luotianyi874",
    "查询运行 AstrBot 的 Windows 计算机剩余电量，回复与低电量提醒文案可自定义",
    PLUGIN_VERSION,
)
class WindowsBatteryPlugin(Star):
    """Windows 设备电量插件"""

    def __init__(self, context: Context, config: Optional[dict] = None):
        super().__init__(context)
        self.config = config or {}
        self.runtime = self._load_runtime()
        self._alert_task: Optional[asyncio.Task] = None
        # 有新会话订阅时用它立刻唤醒后台任务，不用等到下一个检测周期
        self._wake = asyncio.Event()
        # 只打一次的场景化告警，避免后台任务刷屏
        self._warned: set = set()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self):
        """插件启用 / 重载后开始后台电量检测（AstrBot 生命周期钩子）"""
        if self._alert_task is not None and not self._alert_task.done():
            return
        self._alert_task = asyncio.create_task(self._alert_loop())
        logger.info(
            f"[{PLUGIN_NAME}] 低电量提醒任务已启动：阈值 {self._threshold()}%，"
            f"检测间隔 {self._check_interval()} 秒（电量充足时自动放慢），"
            f"已订阅 {len(self._subscribers())} 个会话。"
        )

    async def terminate(self):
        """插件禁用 / 重载时停止后台任务，避免任务泄漏（AstrBot 生命周期钩子）"""
        task, self._alert_task = self._alert_task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001 - 停任务失败不能影响插件卸载
            logger.warning(f"[{PLUGIN_NAME}] 停止低电量检测任务时出错: {e}")

    # ------------------------------------------------------------------ #
    # 指令
    # ------------------------------------------------------------------ #

    @filter.command("设备电量", alias=["电池电量", "电量", "battery"])
    async def battery(self, event: AstrMessageEvent):
        """查询运行 AstrBot 的这台 Windows 电脑还剩多少电。"""
        self._auto_bind(event)
        yield self._build_battery_reply(event)

    # 兜底指令入口：唤醒前缀没被剥离、或消息里残留 @ 标记时仍能识别指令。
    #
    # AstrBot 只有在「消息已被唤醒词剥离」或「@ 到机器人」时，标准指令
    # （@filter.command）才会命中。QQ 官方机器人等平台的群聊 / 频道消息
    # 有时会带着未剥离的 `/设备电量` 或残留的 `<@!openid>` 标记，
    # 这时标准指令会静默失配，看起来就是「插件不回复」。
    # 这里用一条严格锚定的正则补上这些形态，命中后复用同一套指令实现。
    @filter.regex(_FALLBACK_REGEX)
    async def command_fallback(self, event: AstrMessageEvent):
        """指令兜底入口（唤醒前缀 / @ 标记未被框架处理时生效）。"""
        handler = self._resolve_command(_strip_leading_noise(event.get_message_str()))
        if handler is None:
            return
        async for result in handler(event):
            yield result

    @filter.command("设置电量模板")
    async def set_reply_template(self, event: AstrMessageEvent):
        """临时修改电量查询的回复模板，例如：/设置电量模板 {username}，还有{percent}% 电。"""
        if not self._bool("allow_runtime_edit", True):
            yield self._plain(event, "已在插件配置里关闭「允许通过指令修改」，请到配置页修改模板。")
            return

        template = self._extract_arg(event, CMD_SET_TEMPLATE)
        if not template:
            yield self._plain(
                event,
                "请把模板写在指令后面，可用占位符：\n"
                "{username} 称呼 / {percent} 电量 / {state} 供电状态 / {sender} 发送者昵称\n"
                "示例：/设置电量模板 {username}，你的设备电量还有{percent}%。\n"
                "回复「/设置电量模板 默认」可还原成插件配置里的模板。",
            )
            return

        if template in ("默认", "default"):
            self.runtime.pop("reply_template", None)
            hint = "回复模板已还原为插件配置里的内容。"
        else:
            self.runtime["reply_template"] = template
            hint = "回复模板已保存。"

        if not self._save_runtime():
            yield self._plain(event, "保存失败，请查看 AstrBot 日志了解详情。")
            return

        preview = self._render(
            self._reply_template(),
            username=self._username(event),
            percent=87,
            state=STATE_BATTERY,
            sender=self._sender_name(event),
        )
        yield self._plain(event, f"{hint}\n效果预览：{preview}")

    @filter.command("设置电量用户名")
    async def set_username(self, event: AstrMessageEvent):
        """临时修改文案里的称呼，例如：/设置电量用户名 小明。"""
        if not self._bool("allow_runtime_edit", True):
            yield self._plain(event, "已在插件配置里关闭「允许通过指令修改」，请到配置页修改称呼。")
            return

        name = self._extract_arg(event, CMD_SET_NAME)
        if not name:
            yield self._plain(
                event,
                "请把称呼写在指令后面，示例：/设置电量用户名 小明\n"
                "回复「/设置电量用户名 默认」可还原成插件配置里的称呼。",
            )
            return

        if name in ("默认", "default"):
            self.runtime.pop("user_name", None)
            if not self._save_runtime():
                yield self._plain(event, "保存失败，请查看 AstrBot 日志了解详情。")
                return
            yield self._plain(event, "称呼已还原为插件配置里的内容。")
            return

        self.runtime["user_name"] = name
        if not self._save_runtime():
            yield self._plain(event, "保存失败，请查看 AstrBot 日志了解详情。")
            return

        preview = self._render(
            self._reply_template(),
            username=name,
            percent=87,
            state=STATE_BATTERY,
            sender=self._sender_name(event),
        )
        yield self._plain(event, f"称呼已保存为「{name}」。\n效果预览：{preview}")

    @filter.command("电量配置")
    async def show_config(self, event: AstrMessageEvent):
        """查看当前称呼、回复模板和低电量提醒设置。"""
        yield self._plain(
            event,
            f"称呼：{self._username(event)}\n"
            f"查询模板：{self._reply_template()}\n"
            f"提醒模板：{self._alert_template()}\n"
            f"提醒开关：{'开启' if self._alert_enabled() else '关闭'}\n"
            f"提醒阈值：电量 ≤ {self._threshold()}%\n"
            f"检测间隔：{self._check_interval()} 秒\n"
            "以上内容都可以在 AstrBot 的插件配置页修改。",
        )

    @filter.command("电量提醒绑定")
    async def bind_alert(self, event: AstrMessageEvent):
        """把当前会话加入低电量提醒，低于阈值时自动收到提醒。"""
        umo = self._umo(event)
        if not umo:
            yield self._plain(event, "无法获取当前会话信息，绑定失败。")
            return

        if umo in self._subscribers():
            yield self._plain(
                event,
                f"当前会话已经订阅了低电量提醒。\n"
                f"电量 ≤ {self._threshold()}% 时会自动提醒，"
                f"检测间隔 {self._check_interval()} 秒。",
            )
            return

        if not self._add_subscriber(umo):
            yield self._plain(event, "绑定失败，请查看 AstrBot 日志了解详情。")
            return

        extra = "" if self._alert_enabled() else "\n注意：低电量提醒当前是关闭状态，可在插件配置里开启。"
        yield self._plain(
            event,
            f"已把当前会话加入低电量提醒。\n"
            f"电量 ≤ {self._threshold()}% 时会自动提醒，"
            f"检测间隔 {self._check_interval()} 秒。{extra}",
        )

    @filter.command("电量提醒解绑")
    async def unbind_alert(self, event: AstrMessageEvent):
        """把当前会话移出低电量提醒。"""
        if self._remove_subscriber(self._umo(event)):
            yield self._plain(event, "已取消当前会话的低电量提醒。")
        else:
            yield self._plain(event, "当前会话本来就没有订阅低电量提醒。")

    @filter.command("电量提醒状态", alias=["电量提醒"])
    async def alert_status(self, event: AstrMessageEvent):
        """查看低电量提醒的开关、阈值、订阅会话和上次提醒时间。"""
        percent, state = self._read_battery()
        umo = self._umo(event)
        subscribers = self._subscribers()

        if percent is None:
            battery_line = f"当前电量：读取失败（{state}）"
        else:
            battery_line = f"当前电量：{percent}%（{state}）"

        repeat = self._repeat_minutes()
        repeat_line = (
            f"重复提醒：每 {repeat} 分钟一次，直到电量回升"
            if repeat > 0
            else "重复提醒：只提醒一次，直到电量回升"
        )
        last_ts = self._last_alert_ts()
        last_line = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_ts))
            if last_ts > 0
            else "还没有提醒过"
        )
        lines = [
            f"低电量提醒：{'已开启' if self._alert_enabled() else '已关闭'}",
            battery_line,
            f"提醒阈值：电量 ≤ {self._threshold()}%",
            f"检测间隔：{self._check_interval()} 秒（电量充足时自动放慢到 5 分钟）",
            f"仅在用电池时提醒：{'是' if self._only_on_battery() else '否'}",
            repeat_line,
            f"已订阅会话：{len(subscribers)} 个",
        ]
        for item in subscribers[:10]:
            lines.append(f"  - {item}")
        if not subscribers:
            lines.append("  （暂无，发送「电量提醒绑定」即可订阅）")
        elif len(subscribers) > 10:
            lines.append(f"  …另有 {len(subscribers) - 10} 个")
        lines.append(f"当前会话：{'已订阅' if umo and umo in subscribers else '未订阅'}")
        lines.append(f"上次提醒：{last_line}")
        yield self._plain(event, "\n".join(lines))

    @filter.command("电量提醒测试")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def test_alert(self, event: AstrMessageEvent):
        """立刻给当前会话发一条测试提醒，用来检查文案和发送链路（仅管理员）。"""
        if not event.is_admin():
            yield self._plain(event, "该指令仅限管理员使用。")
            return

        percent, state = self._read_battery()
        threshold = self._threshold()
        if percent is None:
            # 读不到电量（例如台式机）时用模拟值，仍然可以测试文案效果
            shown, note = threshold, f"\n（本机读不到电量：{state}，以上为模拟效果）"
        elif percent <= threshold:
            shown, note = percent, ""
        else:
            shown = max(0, threshold)
            note = f"\n（当前实际电量 {percent}%，高于阈值，以上为模拟效果）"

        text = "【测试】" + self._render_alert(shown, state, threshold)
        ok = await self._broadcast(text + note, [self._umo(event)])
        if ok:
            yield self._plain(event, "测试提醒已发送。")
        else:
            yield self._plain(event, "测试提醒发送失败，请查看 AstrBot 日志了解详情。")

    # ------------------------------------------------------------------ #
    # 低电量自动提醒
    # ------------------------------------------------------------------ #

    async def _alert_loop(self):
        """后台检测循环：按当前电量自适应调整读取频率；异常不会让任务退出。"""
        await asyncio.sleep(STARTUP_DELAY)
        while True:
            delay = IDLE_INTERVAL
            try:
                delay = await self._check_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 单次失败不能打断循环
                logger.error(f"[{PLUGIN_NAME}] 低电量检测出错: {e}", exc_info=True)
            await self._wait_interval(delay)

    async def _wait_interval(self, seconds: float):
        """等到下一次检测；期间如果有新会话订阅，会被立刻唤醒。"""
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        finally:
            self._wake.clear()

    async def _check_once(self) -> float:
        """执行一次检测，返回建议的下次检测间隔（秒）。

        只有「开着提醒 + 有订阅会话」时才真的去读电量；其余情况直接返回一个
        较长的间隔，不做任何系统调用，避免空转。
        """
        if not self._alert_enabled():
            return IDLE_INTERVAL

        if platform.system() != "Windows":
            self._warn_once(
                "os",
                f"当前系统为 {platform.system()}，低电量提醒只在 Windows 上工作。",
            )
            return SLOW_INTERVAL

        targets = self._push_targets()
        if not targets:
            # 新装插件时会话还没订阅，这是正常状态，用 info 提示一次就行
            self._info_once(
                "no_target",
                "低电量提醒已开启，但还没有订阅会话；"
                "发送一次「设备电量」或「电量提醒绑定」就会开始监测。",
            )
            return IDLE_INTERVAL
        self._warned.discard("no_target")

        percent, state = self._read_battery()
        if percent is None:
            self._warn_once("battery", f"读不到电量，本次跳过：{state}")
            return SLOW_INTERVAL
        self._warned.discard("battery")

        threshold = self._threshold()
        recover = min(100, threshold + RECOVER_HYSTERESIS)

        # 电量回升：重新武装，允许下一次提醒
        if percent > recover:
            self._set_armed(True)
            return self._next_delay(percent, state, threshold)

        if percent > threshold:
            return self._next_delay(percent, state, threshold)

        # 插着电源时通常不需要催充电（可在配置里关掉这个行为）
        if self._only_on_battery() and state == STATE_PLUGGED:
            return self._next_delay(percent, state, threshold)

        # 已经提醒过：只有到了重复提醒间隔才再提醒
        if not self._armed():
            repeat = self._repeat_minutes() * 60
            last_ts = self._last_alert_ts()
            if repeat <= 0 or (last_ts > 0 and time.time() - last_ts < repeat):
                return self._next_delay(percent, state, threshold)

        text = self._render_alert(percent, state, threshold)
        sent = await self._broadcast(text, targets)
        if sent > 0:
            self._mark_alerted()
            logger.info(
                f"[{PLUGIN_NAME}] 电量 {percent}% 已低于阈值 {threshold}%，"
                f"向 {sent} 个会话发送了提醒。"
            )
        else:
            logger.warning(
                f"[{PLUGIN_NAME}] 电量 {percent}% 已低于阈值 {threshold}%，"
                "但提醒没有发出去，下次检测会重试。"
            )
        return self._next_delay(percent, state, threshold)

    def _next_delay(self, percent, state: str, threshold: int) -> int:
        """按当前状态决定下次读电量的间隔，能在不降低提醒及时性的前提下少唤醒。"""
        base = self._check_interval()
        # 插着电源且只在用电池时提醒：只为发现「拔电」，慢一点没关系
        if self._only_on_battery() and state == STATE_PLUGGED:
            return max(base, SLOW_INTERVAL)
        if percent > threshold + QUIET_MARGIN_FAR:
            return max(base, SLOW_INTERVAL)  # 电量还很足
        if percent > threshold:
            return max(base, MID_INTERVAL)  # 快到阈值了
        if not self._armed():
            return max(base, MID_INTERVAL)  # 已提醒过，等重复间隔或回血
        return base  # 刚好掉到阈值附近，按用户设置的间隔跟进

    async def _broadcast(self, text: str, targets) -> int:
        """向多个会话发送同一条文本，返回成功的数量。"""
        sent = 0
        for umo in targets:
            try:
                ok = await self.context.send_message(umo, self._build_chain(text))
            except Exception as e:  # noqa: BLE001 - 单个会话失败不影响其他会话
                logger.warning(f"[{PLUGIN_NAME}] 向会话 {umo} 发送提醒失败: {e}")
                continue
            if ok:
                sent += 1
            else:
                logger.warning(f"[{PLUGIN_NAME}] 未找到会话 {umo} 对应的平台，提醒未发送。")
        return sent

    def _render_alert(self, percent, state: str, threshold: int) -> str:
        """渲染低电量提醒文案。"""
        return self._render(
            self._alert_template(),
            username=self._alert_username(),
            percent=percent,
            state=state,
            sender="",
            threshold=threshold,
            now=time.strftime("%Y-%m-%d %H:%M:%S"),
        )

    def _alert_username(self) -> str:
        """主动提醒没有发送者，只能用运行时设置的称呼、配置项或默认称呼。"""
        name = self.runtime.get("user_name")
        if name:
            return str(name)
        name = self._str("user_name")
        return name or DEFAULT_USER_NAME

    # ------------------------------------------------------------------ #
    # 提醒开关、订阅与状态
    # ------------------------------------------------------------------ #

    def _alert_enabled(self) -> bool:
        return self._bool("alert_enabled", True)

    def _only_on_battery(self) -> bool:
        return self._bool("only_on_battery", True)

    def _threshold(self) -> int:
        return self._int("alert_percent", DEFAULT_ALERT_PERCENT, 1, MAX_ALERT_PERCENT)

    def _check_interval(self) -> int:
        return self._int(
            "check_interval",
            DEFAULT_CHECK_INTERVAL,
            MIN_CHECK_INTERVAL,
            MAX_CHECK_INTERVAL,
        )

    def _repeat_minutes(self) -> int:
        return self._int("repeat_interval", DEFAULT_REPEAT_MINUTES, 0, 1440)

    def _armed(self) -> bool:
        return bool(self.runtime.get("alert_armed", True))

    def _set_armed(self, armed: bool):
        if self._armed() == armed:
            return
        self.runtime["alert_armed"] = armed
        self._save_runtime()

    def _last_alert_ts(self) -> float:
        try:
            return float(self.runtime.get("alert_last_ts") or 0)
        except (TypeError, ValueError):
            return 0.0

    def _mark_alerted(self):
        self.runtime["alert_armed"] = False
        self.runtime["alert_last_ts"] = time.time()
        self._save_runtime()

    def _subscribers(self) -> list:
        """读取订阅会话（去重、忽略非法项）。"""
        data = self.runtime.get("alert_subscribers")
        if not isinstance(data, list):
            return []
        result = []
        for item in data:
            target = self._normalize_target(item)
            if target and target not in result:
                result.append(target)
        return result

    def _add_subscriber(self, umo) -> bool:
        target = self._normalize_target(umo)
        if not target:
            return False
        subscribers = self._subscribers()
        if target in subscribers:
            return True
        # 超出上限时丢弃最早的订阅，避免设置文件无限膨胀
        if len(subscribers) >= MAX_SUBSCRIBERS:
            subscribers = subscribers[-(MAX_SUBSCRIBERS - 1):]
        subscribers.append(target)
        previous = self.runtime.get("alert_subscribers")
        self.runtime["alert_subscribers"] = subscribers
        if self._save_runtime():
            # 有新会话开始订阅：立刻唤醒后台任务，不用等下一个检测周期
            self._wake.set()
            return True
        # 保存失败则回滚，避免提示成功但重启后丢失
        if previous is None:
            self.runtime.pop("alert_subscribers", None)
        else:
            self.runtime["alert_subscribers"] = previous
        return False

    def _remove_subscriber(self, umo) -> bool:
        target = self._normalize_target(umo)
        if not target:
            return False
        subscribers = self._subscribers()
        if target not in subscribers:
            return False
        previous = self.runtime.get("alert_subscribers")
        subscribers.remove(target)
        self.runtime["alert_subscribers"] = subscribers
        if self._save_runtime():
            return True
        if previous is None:
            self.runtime.pop("alert_subscribers", None)
        else:
            self.runtime["alert_subscribers"] = previous
        return False

    def _push_targets(self) -> list:
        """配置里的固定推送目标 + 运行时订阅的会话，去重后返回。"""
        targets = []
        raw = self._get("push_targets", []) or []
        if isinstance(raw, str):
            raw = re.split(r"[\s,，;；]+", raw)
        if isinstance(raw, (list, tuple, set)):
            for item in raw:
                target = self._normalize_target(item)
                if target and target not in targets:
                    targets.append(target)
        for item in self._subscribers():
            if item not in targets:
                targets.append(item)
        return targets

    def _auto_bind(self, event: AstrMessageEvent):
        """查询电量时顺手记住这个会话，省去手动绑定。"""
        if not self._bool("auto_bind_session", True):
            return
        self._add_subscriber(self._umo(event))

    @staticmethod
    def _umo(event: AstrMessageEvent) -> str:
        try:
            return str(getattr(event, "unified_msg_origin", "") or "").strip()
        except Exception as e:  # noqa: BLE001 - 取不到会话标识时按未绑定处理
            logger.debug(f"[{PLUGIN_NAME}] 获取会话标识失败: {e}")
            return ""

    @staticmethod
    def _normalize_target(value) -> str:
        """校验 unified_msg_origin 的形状：platform_id:message_type:session_id。"""
        text = str(value or "").strip().strip("`'\"")
        if not text or text.count(":") < 2:
            return ""
        return text

    # ------------------------------------------------------------------ #
    # 文案与称呼
    # ------------------------------------------------------------------ #

    def _build_battery_reply(self, event: AstrMessageEvent):
        """生成电量查询的回复。"""
        percent, state = self._read_battery()
        if percent is None:
            return self._plain(event, f"暂时读不到电量：{state}")
        text = self._render(
            self._reply_template(),
            username=self._username(event),
            percent=percent,
            state=state,
            sender=self._sender_name(event),
        )
        return self._plain(event, text)

    def _reply_template(self) -> str:
        """运行时设置的模板优先，其次用插件配置项。"""
        template = self.runtime.get("reply_template")
        if not template:
            template = self._get("reply_template", DEFAULT_REPLY_TEMPLATE)
        template = str(template or "").strip()
        return template or DEFAULT_REPLY_TEMPLATE

    def _alert_template(self) -> str:
        template = self._get("alert_template", DEFAULT_ALERT_TEMPLATE)
        template = str(template or "").strip()
        return template or DEFAULT_ALERT_TEMPLATE

    @staticmethod
    def _render(
        template: str,
        *,
        username: str,
        percent,
        state: str,
        sender: str = "",
        threshold=None,
        now: str = "",
    ) -> str:
        """用占位符生成文案。

        用 replace 而不是 format，这样模板里写别的花括号也不会抛异常。
        """
        text = str(template or DEFAULT_REPLY_TEMPLATE)
        values = {
            "{username}": username,
            "{percent}": str(percent),
            "{state}": state,
            "{sender}": sender,
            "{name}": username,  # 兼容写法
            "{电量}": str(percent),  # 兼容中文占位符
        }
        if threshold is not None:
            values["{threshold}"] = str(threshold)
        if now:
            values["{time}"] = now
        for key, value in values.items():
            text = text.replace(key, str(value))
        return text

    def _username(self, event: AstrMessageEvent) -> str:
        """运行时设置 > 配置项 > 发送者昵称 > 默认称呼。"""
        name = self.runtime.get("user_name")
        if name:
            return str(name)
        name = self._str("user_name")
        if name:
            return name
        if self._bool("use_sender_name", True):
            sender = self._sender_name(event)
            if sender:
                return sender
        return DEFAULT_USER_NAME

    @staticmethod
    def _sender_name(event: AstrMessageEvent) -> str:
        try:
            return event.get_sender_name() or ""
        except Exception as e:  # noqa: BLE001 - 部分平台拿不到昵称
            logger.debug(f"[{PLUGIN_NAME}] 获取发送者昵称失败: {e}")
            return ""

    @staticmethod
    def _plain(event: AstrMessageEvent, text: str):
        """构造纯文本回复。

        部分平台（例如 QQ 官方机器人）对 markdown 有额外权限要求，
        这里显式要求纯文本，避免回复因平台限制发不出去。
        """
        result = event.plain_result(text)
        try:
            result.use_markdown(False)
        except AttributeError:  # 旧版本 AstrBot 没有该字段
            pass
        return result

    @staticmethod
    def _build_chain(text: str) -> MessageChain:
        chain = MessageChain().message(text)
        try:
            chain.use_markdown(False)
        except AttributeError:
            pass
        return chain

    @staticmethod
    def _extract_arg(event: AstrMessageEvent, names) -> str:
        """从消息里剥掉 @ 标记、唤醒词和指令名，取剩下的参数。"""
        raw = _strip_leading_noise(event.get_message_str())
        for name in sorted(names, key=len, reverse=True):
            if raw.startswith(name):
                return raw[len(name):].lstrip(" ：:，,-").strip()
        # 没匹配到指令名：按第一个空白切分，取后半部分
        parts = raw.split(None, 1)
        return parts[1].strip() if len(parts) > 1 else ""

    def _resolve_command(self, text: str):
        """按指令文本找到对应的处理方法，用于兜底入口分发。

        只接受「和指令名完全一致」或「指令名 + 空格 + 参数」，
        与 AstrBot 标准指令的匹配规则保持一致，避免把「电量低啊」这类
        普通聊天误当成指令。
        """
        if not text:
            return None
        for name, attr in _SORTED_COMMANDS:
            if text == name or text.startswith(name + " "):
                return getattr(self, attr)
        return None

    # ------------------------------------------------------------------ #
    # 运行时设置的持久化
    # ------------------------------------------------------------------ #

    def _data_file(self) -> Optional[Path]:
        """插件数据目录下的设置文件；拿不到目录时返回 None（不回退到插件目录）。"""
        try:
            return Path(StarTools.get_data_dir(PLUGIN_NAME)) / DATA_FILE_NAME
        except Exception as e:  # noqa: BLE001 - 数据目录不可用时不影响指令本身
            logger.error(f"[{PLUGIN_NAME}] 无法获取插件数据目录，本次不持久化设置: {e}")
            return None

    def _load_runtime(self) -> dict:
        path = self._data_file()
        if path is None or not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning(f"[{PLUGIN_NAME}] 读取运行时设置失败: {e}")
            return {}
        return data if isinstance(data, dict) else {}

    def _save_runtime(self) -> bool:
        """原子写入设置文件，返回是否成功。"""
        path = self._data_file()
        if path is None:
            return False
        try:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                json.dumps(self.runtime, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
            return True
        except (OSError, TypeError, ValueError) as e:
            logger.error(f"[{PLUGIN_NAME}] 保存运行时设置失败: {e}")
            return False

    # ------------------------------------------------------------------ #
    # 配置读取
    # ------------------------------------------------------------------ #

    def _get(self, key, default):
        """读插件配置项，缺失或类型不对时返回默认值。"""
        try:
            value = self.config.get(key, default)
        except (AttributeError, TypeError) as e:
            logger.debug(f"[{PLUGIN_NAME}] 读取配置项 {key} 失败: {e}")
            return default
        return default if value is None else value

    def _int(self, key, default: int, minimum=None, maximum=None) -> int:
        try:
            value = int(self._get(key, default))
        except (TypeError, ValueError):
            value = default
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def _bool(self, key, default: bool) -> bool:
        value = self._get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是", "开启")
        return bool(value)

    def _str(self, key, default: str = "") -> str:
        value = self._get(key, default)
        return str(value).strip() if value is not None else default

    # ------------------------------------------------------------------ #
    # 电池读取
    # ------------------------------------------------------------------ #

    def _read_battery(self):
        """返回 (电量百分比 int | None, 状态说明 str)；百分比为 None 表示读不到。"""
        if platform.system() != "Windows":
            return None, f"当前系统为 {platform.system()}，该插件仅支持 Windows 计算机"

        # 1) 优先用 psutil（内部同样调用 Windows 的 GetSystemPowerStatus）
        if psutil is not None:
            try:
                battery = psutil.sensors_battery()
            except Exception as e:  # noqa: BLE001 - 读失败就走下面的兜底
                logger.warning(f"[{PLUGIN_NAME}] psutil 读取电池失败: {e}")
                battery = None
            if battery is not None and battery.percent is not None:
                percent = int(round(battery.percent))
                if 0 <= percent <= 100:
                    return percent, self._power_state(battery.power_plugged)

        # 2) 兜底：直接调用 Windows API，不依赖第三方库
        percent, plugged, battery_flag = self._read_battery_winapi()
        if percent is not None:
            return percent, self._power_state(plugged)

        # BatteryFlag = 128 表示系统没有电池（台式机 / 虚拟机）
        if battery_flag is not None and battery_flag & 128:
            return None, "系统报告这台设备没有电池（可能是台式机或虚拟机）"
        return None, "没检测到电池，有可能是台式机，或系统没有提供电池状态"

    @staticmethod
    def _read_battery_winapi():
        """通过 GetSystemPowerStatus 读取电量。

        返回 (电量百分比 | None, 是否插电 | None, BatteryFlag | None)。
        """
        if platform.system() != "Windows":
            return None, None, None
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
                return None, None, None
            flag = int(status.BatteryFlag)
            if status.BatteryLifePercent == 255:  # 255 表示未知
                return None, None, flag
            return int(status.BatteryLifePercent), status.ACLineStatus == 1, flag
        except Exception as e:  # noqa: BLE001 - 兜底失败按读不到处理
            logger.warning(f"[{PLUGIN_NAME}] 调用 Windows API 读取电池失败: {e}")
            return None, None, None

    @staticmethod
    def _power_state(plugged: Optional[bool]) -> str:
        if plugged is None:
            return STATE_UNKNOWN
        return STATE_PLUGGED if plugged else STATE_BATTERY

    # ------------------------------------------------------------------ #
    # 杂项
    # ------------------------------------------------------------------ #

    def _warn_once(self, key: str, message: str):
        """同一类问题只记一次日志，避免后台任务刷屏。"""
        if key in self._warned:
            return
        self._warned.add(key)
        logger.warning(f"[{PLUGIN_NAME}] {message}")

    def _info_once(self, key: str, message: str):
        """同上，但用于正常状态下的提示。"""
        if key in self._warned:
            return
        self._warned.add(key)
        logger.info(f"[{PLUGIN_NAME}] {message}")
