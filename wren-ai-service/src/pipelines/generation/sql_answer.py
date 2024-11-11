import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Dict

import orjson
from hamilton import base
from hamilton.experimental.h_async import AsyncDriver
from haystack import component
from haystack.components.builders.prompt_builder import PromptBuilder
from langfuse.decorators import observe
from pydantic import BaseModel

from src.core.engine import Engine
from src.core.pipeline import BasicPipeline, async_validate
from src.core.provider import LLMProvider
from src.utils import async_timer, timer

logger = logging.getLogger("wren-ai-service")

sql_to_answer_system_prompt = """
### TASK

You are a data analyst that great at answering user's questions based on the data, sql and sql summary so that even non technical users can easily understand.
Please answer the user's question in concise and clear manner in Markdown format.

### INSTRUCTIONS

1. Read the user's question and understand the user's intention.
2. Read the sql summary and understand the data.
3. Read the sql and understand the data.
4. Generate a consice and clear answer in string format and a reasoning process in string format to the user's question based on the data, sql and sql summary.
5. If answer is in list format, only list top few examples, and tell users there are more results omitted.
6. Answer must be in the same language user specified.

### OUTPUT FORMAT

Return the output in the following JSON format:

{
    "reasoning": "<STRING>",
    "answer": "<STRING_IN_MARKDOWN_FORMAT>",
}
"""

sql_to_answer_user_prompt_template = """
### Input
User's question: {{ query }}
SQL: {{ sql }}
SQL summary: {{ sql_summary }}
Data: {{ sql_data }}
Language: {{ language }}
Please think step by step and answer the user's question.
"""


@component
class SQLAnswerGenerationPostProcessor:
    @component.output_types(
        results=Dict[str, Any],
    )
    def run(
        self,
        replies: str,
    ):
        try:
            data = orjson.loads(replies[0])

            return {
                "results": {
                    "answer": data["answer"],
                    "reasoning": data["reasoning"],
                    "error": "",
                }
            }
        except Exception as e:
            logger.exception(f"Error in SQLAnswerGenerationPostProcessor: {e}")

            return {
                "results": {
                    "answer": "",
                    "reasoning": "",
                    "error": str(e),
                }
            }


## Start of Pipeline
@timer
@observe(capture_input=False)
def prompt(
    query: str,
    sql: str,
    sql_summary: str,
    sql_data: dict,
    language: str,
    prompt_builder: PromptBuilder,
) -> dict:
    logger.debug(f"query: {query}")
    logger.debug(f"sql: {sql}")
    logger.debug(f"sql_summary: {sql_summary}")
    logger.debug(f"sql data: {sql_data}")
    logger.debug(f"language: {language}")
    return prompt_builder.run(
        query=query,
        sql=sql,
        sql_summary=sql_summary,
        sql_data=sql_data["results"],
        language=language,
    )


@async_timer
@observe(as_type="generation", capture_input=False)
async def generate_answer(prompt: dict, generator: Any) -> dict:
    logger.debug(f"prompt: {orjson.dumps(prompt, option=orjson.OPT_INDENT_2).decode()}")

    return await generator.run(prompt=prompt.get("prompt"))


@timer
@observe(capture_input=False)
def post_process(
    generate_answer: dict, post_processor: SQLAnswerGenerationPostProcessor
) -> dict:
    logger.debug(
        f"generate_answer: {orjson.dumps(generate_answer, option=orjson.OPT_INDENT_2).decode()}"
    )

    return post_processor.run(generate_answer.get("replies"))


## End of Pipeline


class AnswerResults(BaseModel):
    reasoning: str
    answer: str


SQL_ANSWER_MODEL_KWARGS = {
    "response_format": {
        "type": "json_schema",
        "json_schema": {
            "name": "sql_summary",
            "schema": AnswerResults.model_json_schema(),
        },
    }
}


class SQLAnswer(BasicPipeline):
    def __init__(
        self,
        llm_provider: LLMProvider,
        engine: Engine,
        **kwargs,
    ):
        self._user_queues = {}
        self._components = {
            "prompt_builder": PromptBuilder(
                template=sql_to_answer_user_prompt_template
            ),
            "generator": llm_provider.get_generator(
                system_prompt=sql_to_answer_system_prompt,
                generation_kwargs=SQL_ANSWER_MODEL_KWARGS,
                streaming_callback=self._streaming_callback,
            ),
            "post_processor": SQLAnswerGenerationPostProcessor(),
        }

        super().__init__(
            AsyncDriver({}, sys.modules[__name__], result_builder=base.DictResult())
        )

    def _streaming_callback(self, chunk, query_id):
        if query_id not in self._user_queues:
            self._user_queues[
                query_id
            ] = asyncio.Queue()  # Create a new queue for the user if it doesn't exist
        # Put the chunk content into the user's queue
        asyncio.create_task(self._user_queues[query_id].put(chunk.content))
        if chunk.meta.get("finish_reason") == "stop":
            asyncio.create_task(self._user_queues[query_id].put("<DONE>"))

    async def get_streaming_results(self, query_id):
        if query_id not in self._user_queues:
            self._user_queues[
                query_id
            ] = asyncio.Queue()  # Ensure the user's queue exists
        while True:
            # Wait for an item from the user's queue
            self._streaming_results = await self._user_queues[query_id].get()
            if self._streaming_results == "<DONE>":  # Check for end-of-stream signal
                del self._user_queues[query_id]
                break
            if self._streaming_results:  # Check if there are results to yield
                yield self._streaming_results
                self._streaming_results = ""  # Clear after yielding

    def visualize(
        self,
        query: str,
        sql: str,
        sql_summary: str,
        sql_data: dict,
        language: str,
    ) -> None:
        destination = "outputs/pipelines/generation"
        if not Path(destination).exists():
            Path(destination).mkdir(parents=True, exist_ok=True)

        self._pipe.visualize_execution(
            ["post_process"],
            output_file_path=f"{destination}/sql_answer.dot",
            inputs={
                "query": query,
                "sql": sql,
                "sql_summary": sql_summary,
                "sql_data": sql_data,
                "language": language,
                **self._components,
            },
            show_legend=True,
            orient="LR",
        )

    @async_timer
    @observe(name="SQL Answer Generation")
    async def run(
        self,
        query: str,
        sql: str,
        sql_summary: str,
        sql_data: dict,
        language: str,
    ) -> dict:
        logger.info("Sql_Answer Generation pipeline is running...")
        return await self._pipe.execute(
            ["post_process"],
            inputs={
                "query": query,
                "sql": sql,
                "sql_summary": sql_summary,
                "sql_data": sql_data,
                "language": language,
                **self._components,
            },
        )


if __name__ == "__main__":
    from langfuse.decorators import langfuse_context

    from src.core.engine import EngineConfig
    from src.core.pipeline import async_validate
    from src.providers import init_providers
    from src.utils import init_langfuse, load_env_vars

    load_env_vars()
    init_langfuse()

    llm_provider, _, _, engine = init_providers(EngineConfig())
    pipeline = SQLAnswer(
        llm_provider=llm_provider,
        engine=engine,
    )

    pipeline.visualize(
        "query", "SELECT * FROM table_name", "sql summary", {}, "English"
    )
    async_validate(
        lambda: pipeline.run(
            "query", "SELECT * FROM table_name", "sql summary", {}, "English"
        )
    )

    langfuse_context.flush()
