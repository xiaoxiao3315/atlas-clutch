from __future__ import annotations

import bridge


def assert_contains(text: str, needle: str) -> None:
    if needle not in text:
        raise AssertionError(f"missing expected text: {needle}")


def assert_not_contains(text: str, needle: str) -> None:
    if needle in text:
        raise AssertionError(f"unexpected text: {needle}")


def context() -> dict:
    return {
        "registered": True,
        "robot_id": "robot-1234567890",
        "owner_channel_id": "owner-1234567890",
        "last_seq": 42,
        "state": {
            bridge.PROCESSED_STATE_KEY: ["1:a", "2:b"],
        },
        "runtime_info": {
            "run_id": "workflow-smoke",
            "pid": 1234,
            "startup_method": "manual",
            "lock_status": "held",
        },
        "heartbeat": {
            "updated_at": "2026-06-06T18:00:00+08:00",
        },
    }


def main() -> int:
    ctx = context()

    workflow, route = bridge.prepare_reply("/workflow", ctx)
    assert route == "local_command"
    assert_contains(workflow, "Atlas 工作流闭环")
    assert_contains(workflow, "工作单")
    assert_contains(workflow, "Codex/Kiro 回传报告")
    assert_contains(workflow, "下一步")

    wo, route = bridge.prepare_reply("/template wo", ctx)
    assert route == "local_command"
    for needle in ("目标", "范围", "执行边界", "验收标准", "风险点", "回传证据"):
        assert_contains(wo, needle)

    report, route = bridge.prepare_reply("/template report", ctx)
    assert route == "local_command"
    for needle in ("修改文件", "执行命令", "测试结果", "未解决风险"):
        assert_contains(report, needle)

    review, route = bridge.prepare_reply("/template review", ctx)
    assert route == "local_command"
    for needle in ("通过", "不通过", "待补证据"):
        assert_contains(review, needle)

    help_text, route = bridge.prepare_reply("/help", ctx)
    assert route == "local_command"
    for needle in (
        "生成工作单",
        "审查这份 Codex 返回报告",
        "判断下一步优先级",
        "把这个报错整理成 Kiro/Codex 可执行任务",
        "根据以下证据判断是否完成",
    ):
        assert_contains(help_text, needle)

    original_call_hermes = bridge.call_hermes

    def fail_call_hermes(_: str) -> str:
        raise AssertionError("workflow and execution requests must not call Hermes")

    bridge.call_hermes = fail_call_hermes
    try:
        generated_wo, route = bridge.prepare_reply("生成工作单：检查 Kiro 反代当前状态", ctx)
        assert route == "work_order"
        assert_contains(generated_wo, "检查 Kiro 反代当前状态")
        assert_contains(generated_wo, "回传证据")

        reviewed, route = bridge.prepare_reply(
            "审查这份 Codex 返回报告：修改文件：bridge.py；执行命令：python smoke_workflow.py；测试结果：通过",
            ctx,
        )
        assert route == "review"
        for needle in ("已验证", "未验证", "风险", "下一步决策"):
            assert_contains(reviewed, needle)

        evidence, route = bridge.prepare_reply("根据以下证据判断是否完成：只有口头说明，没有命令输出", ctx)
        assert route == "review"
        assert_contains(evidence, "未验证")
        assert_contains(evidence, "不能判定完成")

        command_order, route = bridge.prepare_reply("帮我执行 dir E:\\ai", ctx)
        assert route == "work_order"
        assert_contains(command_order, "工作单")
        assert_contains(command_order, "不运行命令")
        assert_contains(command_order, "不修改文件")
        assert_not_contains(command_order, "已执行")
        assert_not_contains(command_order, "我是 Codex")
        assert_not_contains(command_order, "我是 OpenClaw")
    finally:
        bridge.call_hermes = original_call_hermes

    print("smoke_workflow: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
