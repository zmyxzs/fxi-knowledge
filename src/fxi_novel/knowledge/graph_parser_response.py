"""Interpret provider termination without guessing missing response contents."""

from fxi_novel.models.gateway import ChatResult


def parser_response_failure_kind(text: str | None, finish_reason: str | None) -> str | None:
    if finish_reason in {"length", "max_tokens", "max_output_tokens"}:
        return "response_truncated"
    if finish_reason == "content_filter":
        return "response_filtered"
    if text is not None and not text.strip():
        return "empty_response"
    return None


def require_complete_parser_response(result: ChatResult) -> None:
    failure = parser_response_failure_kind(result.text, result.finish_reason)
    if failure is not None:
        raise ValueError(
            f"{failure}: 解析响应未正常完成（finish_reason={result.finish_reason!r}）；"
            "原始响应已保留，本次不能声明完整抽取或审校通过。"
        )
