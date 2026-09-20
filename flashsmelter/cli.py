"""命令行入口。

CLI 与控制台共用同一份动作注册表：``call`` 子命令就是不带 HTTP 的动作调用，
方便值班人员在服务器上直接下发指令或做点检。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .application import Application
from .config import Settings
from .console import ConsoleApp, ConsoleServer
from .errors import FlashSmelterError, ValidationError
from .params import Params


def _coerce(text: str) -> Any:
    lowered = text.strip().lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _collect_params(raw_pairs: Sequence[str], raw_json: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if raw_json:
        try:
            decoded = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise ValidationError("--params-json 不是合法 JSON") from exc
        if not isinstance(decoded, dict):
            raise ValidationError("--params-json 必须是 JSON 对象")
        params.update({str(key): value for key, value in decoded.items()})
    for pair in raw_pairs:
        if "=" not in pair:
            raise ValidationError("--param 需要 key=value 形式", details={"value": pair})
        key, value = pair.split("=", 1)
        params[key.strip()] = _coerce(value)
    return params


def _build_settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env(None)
    root = getattr(args, "root", None)
    if root:
        settings = settings.with_root(Path(root))
    namespace = getattr(args, "namespace", None)
    if namespace:
        settings = replace(settings, namespace=namespace)
        settings.validate()
    return settings


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _run(action: str, params: Mapping[str, Any], args: argparse.Namespace) -> int:
    application = Application(_build_settings(args))
    try:
        result = application.invoke(action, params, source=f"cli:{action}")
    except FlashSmelterError as exc:
        _print(exc.to_dict())
        return 1
    _print({"action": action, "result": dict(result)})
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    settings = replace(_build_settings(args), host=args.host, port=args.port)
    settings.validate()
    application = Application(settings)
    console = ConsoleApp(application)
    server = ConsoleServer(console, host=settings.host, port=settings.port)
    host, port = server.start()
    print(f"FlashSmelter 控制台已启动：http://{host}:{port}/api/state（Ctrl+C 停止）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    application = Application(_build_settings(args))
    state = application.state()
    if getattr(args, "component", None):
        component = application.component(args.component)
        _print({"name": component.name, "status": dict(component.status()), "snapshot": dict(component.snapshot())})
        return 0
    _print(state)
    return 0


def _cmd_actions(args: argparse.Namespace) -> int:
    application = Application(_build_settings(args))
    _print({"actions": application.describe_actions()})
    return 0


def _cmd_call(args: argparse.Namespace) -> int:
    params = _collect_params(args.param or [], args.params_json)
    return _run(args.action, params, args)


def _cmd_audit(args: argparse.Namespace) -> int:
    application = Application(_build_settings(args))
    events = application.audit_events(
        limit=args.limit,
        since_seq=args.since,
        action=args.action,
        target=args.target,
        outcome=args.outcome,
        actor=args.actor,
    )
    _print({"count": len(events), "events": events})
    return 0


def _cmd_heat(args: argparse.Namespace) -> int:
    application = Application(_build_settings(args))
    _print(
        {
            "current": dict(application.furnace.heat_report()),
            "heats": [dict(item) for item in application.furnace.heats(limit=args.limit)],
            "converter": dict(application.conv.status()),
        }
    )
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    application = Application(_build_settings(args))
    report = application.verify()
    _print(report)
    return 0 if report.get("ok") else 2


def _cmd_replay_steps(args: argparse.Namespace) -> int:
    application = Application(_build_settings(args))
    session = application.replay()
    events = session.steps(limit=args.limit, critical_only=args.critical_only)
    _print({"count": len(events), "length": session.length(), "events": events})
    return 0


def _cmd_replay_show(args: argparse.Namespace) -> int:
    application = Application(_build_settings(args))
    session = application.replay()
    if args.at:
        _print(session.state_at_time(args.at))
        return 0
    step = args.step if args.step is not None else session.length()
    _print(session.state_at(step))
    return 0


def _cmd_replay_mark(args: argparse.Namespace) -> int:
    application = Application(_build_settings(args))
    mark = application.mark_replay_step(step=args.step, note=args.note, actor=args.actor or "anonymous")
    _print({"mark": mark})
    return 0


def _cmd_replay_marks(args: argparse.Namespace) -> int:
    application = Application(_build_settings(args))
    session = application.replay()
    marks = session.marks()
    _print({"count": len(marks), "marks": marks})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flashsmelter",
        description="FlashSmelter 铜闪速熔炼炉精矿喷吹与放铜控制平台",
    )
    parser.add_argument("--root", help="状态根目录（默认 var/）")
    parser.add_argument("--namespace", help="冶炼命名空间 site/unit")
    parser.add_argument("--version", action="store_true", help="打印版本后退出")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="启动 JSON 控制台")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.set_defaults(func=_cmd_serve)

    status = subparsers.add_parser("status", help="打印平台状态")
    status.add_argument("--component", help="只打印指定组件")
    status.set_defaults(func=_cmd_status)

    actions = subparsers.add_parser("actions", help="列出可用动作")
    actions.set_defaults(func=_cmd_actions)

    call = subparsers.add_parser("call", help="下发一条控制指令")
    call.add_argument("action", help="动作名，如 furnace.start")
    call.add_argument("--param", action="append", help="key=value，可重复")
    call.add_argument("--params-json", help="以 JSON 对象形式给出参数")
    call.set_defaults(func=_cmd_call)

    audit = subparsers.add_parser("audit", help="查询审计流水")
    audit.add_argument("--limit", type=int, default=20)
    audit.add_argument("--since", type=int, default=0)
    audit.add_argument("--action")
    audit.add_argument("--target")
    audit.add_argument("--outcome")
    audit.add_argument("--actor")
    audit.set_defaults(func=_cmd_audit)

    heat = subparsers.add_parser("heat", help="打印炉次与转炉批次")
    heat.add_argument("--limit", type=int, default=5)
    heat.set_defaults(func=_cmd_heat)

    verify = subparsers.add_parser("verify", help="校验落盘数据完整性")
    verify.set_defaults(func=_cmd_verify)

    replay = subparsers.add_parser("replay", help="只读回放历史工况（不改现场状态）")
    replay.set_defaults(replay_parser=replay)
    replay_sub = replay.add_subparsers(dest="replay_command")

    replay_steps = replay_sub.add_parser("steps", help="列出时间线步骤（含关键标记）")
    replay_steps.add_argument("--limit", type=int, default=50)
    replay_steps.add_argument("--critical-only", action="store_true", help="只看关键步骤")
    replay_steps.set_defaults(func=_cmd_replay_steps)

    replay_show = replay_sub.add_parser("show", help="物化某一步（或某时刻）的全厂工况")
    replay_show.add_argument("--step", type=int, default=None, help="步号（缺省为最后一步）")
    replay_show.add_argument("--at", default=None, help="按 ISO 时刻定位，优先于 --step")
    replay_show.set_defaults(func=_cmd_replay_show)

    replay_mark = replay_sub.add_parser("mark", help="给某一步挂复盘标记")
    replay_mark.add_argument("--step", type=int, required=True)
    replay_mark.add_argument("--note", required=True, help="标记说明")
    replay_mark.add_argument("--actor", default=None)
    replay_mark.set_defaults(func=_cmd_replay_mark)

    replay_marks = replay_sub.add_parser("marks", help="列出全部复盘标记")
    replay_marks.set_defaults(func=_cmd_replay_marks)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else sys.argv[1:])
    if getattr(args, "version", False):
        from . import __version__

        print(__version__)
        return 0
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    func = getattr(args, "func", None)
    if func is None:
        # 只给了 ``replay`` 而没给子命令
        sub = getattr(args, "replay_parser", None)
        (sub or parser).print_help()
        return 1
    try:
        return int(func(args))
    except FlashSmelterError as exc:
        _print(exc.to_dict())
        return 1


__all__ = ["main", "build_parser"]
