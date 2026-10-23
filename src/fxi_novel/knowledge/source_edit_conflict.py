"""Source edit conflicts shared by domain and HTTP adapters."""


class SourceEditConflict(ValueError):
    def __init__(self, source_id: str, expected: int, current: int) -> None:
        self.source_id = source_id
        self.expected_revision = expected
        self.current_revision = current
        super().__init__("资料已发生变化，请重新读取当前内容后合并本次修改。")


class CreativeNoteSaveConflict(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)
