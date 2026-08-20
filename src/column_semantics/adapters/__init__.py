"""바깥 세계와 닿는 계층: CSV 파일, LLM 엔드포인트, 프롬프트 폴더, 환경변수.

pipeline은 여기의 구체 클래스가 아니라 프로토콜(`LLMClient`, `PromptLibrary`)에
의존한다 - 테스트에서 LLM을 가짜로 갈아끼울 수 있는 이유가 그것이다.
"""

from column_semantics.adapters.csv_source import CsvRepairError, read_csv_safely
from column_semantics.adapters.env import load_dotenv
from column_semantics.adapters.llm import (
    LLMClient,
    OpenAICompatibleLLM,
    make_llm_from_env,
    models_from_env,
)
from column_semantics.adapters.prompts import FileSystemPrompts, PromptLibrary
from column_semantics.adapters.ratelimit import RateLimiter

__all__ = [
    "CsvRepairError",
    "FileSystemPrompts",
    "LLMClient",
    "OpenAICompatibleLLM",
    "PromptLibrary",
    "RateLimiter",
    "load_dotenv",
    "make_llm_from_env",
    "models_from_env",
    "read_csv_safely",
]
