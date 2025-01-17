import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import orjson
from hamilton import base
from hamilton.async_driver import AsyncDriver
from hamilton.function_modifiers import extract_fields
from haystack import Document, component
from haystack.components.writers import DocumentWriter
from haystack.document_stores.types import DocumentStore, DuplicatePolicy
from langfuse.decorators import observe
from tqdm import tqdm

from src.core.pipeline import BasicPipeline
from src.core.provider import DocumentStoreProvider, EmbedderProvider
from src.utils import async_timer, timer

logger = logging.getLogger("wren-ai-service")

DATASET_NAME = os.getenv("DATASET_NAME")


@component
class DocumentCleaner:
    """
    This component is used to clear all the documents in the specified document store(s).

    """

    def __init__(self, stores: List[DocumentStore]) -> None:
        self._stores = stores

    @component.output_types(mdl=str)
    async def run(self, mdl: str, id: Optional[str] = None) -> str:
        async def _clear_documents(
            store: DocumentStore, id: Optional[str] = None
        ) -> None:
            filters = (
                {
                    "operator": "AND",
                    "conditions": [
                        {"field": "project_id", "operator": "==", "value": id},
                    ],
                }
                if id
                else None
            )
            await store.delete_documents(filters)

        logger.info("Ask Indexing pipeline is clearing old documents...")
        await asyncio.gather(*[_clear_documents(store, id) for store in self._stores])
        return {"mdl": mdl}


@component
class MDLValidator:
    """
    Validate the MDL to check if it is a valid JSON and contains the required keys.
    """

    @component.output_types(mdl=Dict[str, Any])
    def run(self, mdl: str) -> str:
        try:
            mdl_json = orjson.loads(mdl)
            logger.debug(f"MDL JSON: {mdl_json}")
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}")
        if "models" not in mdl_json:
            mdl_json["models"] = []
        if "views" not in mdl_json:
            mdl_json["views"] = []
        if "relationships" not in mdl_json:
            mdl_json["relationships"] = []
        if "metrics" not in mdl_json:
            mdl_json["metrics"] = []

        return {"mdl": mdl_json}


@component
class ViewChunker:
    """
    Convert the view MDL to the following format:
    {
      "question":"user original query",
      "summary":"the description generated by LLM",
      "statement":"the SQL statement generated by LLM",
      "viewId": "the view Id"
    }
    and store it in the view store.
    """

    @component.output_types(documents=List[Document])
    def run(self, mdl: Dict[str, Any], id: Optional[str] = None) -> None:
        def _get_content(view: Dict[str, Any]) -> str:
            properties = view.get("properties", {})
            historical_queries = properties.get("historical_queries", [])
            question = properties.get("question", "")

            return " ".join(historical_queries + [question])

        def _get_meta(view: Dict[str, Any]) -> Dict[str, Any]:
            properties = view.get("properties", {})
            return {
                "summary": properties.get("summary", ""),
                "statement": view.get("statement", ""),
                "viewId": properties.get("viewId", ""),
            }

        converted_views = [
            {"content": _get_content(view), "meta": _get_meta(view)}
            for view in mdl["views"]
        ]

        return {
            "documents": [
                Document(
                    id=str(uuid.uuid4()),
                    meta={"project_id": id, **converted_view["meta"]}
                    if id
                    else {**converted_view["meta"]},
                    content=converted_view["content"],
                )
                for converted_view in tqdm(
                    converted_views,
                    desc="indexing view into the historical view question store",
                )
            ]
        }


@component
class DDLConverter:
    @component.output_types(documents=List[Document])
    def run(
        self,
        mdl: Dict[str, Any],
        column_indexing_batch_size: int,
        id: Optional[str] = None,
    ):
        logger.info(
            "Ask Indexing pipeline is writing new documents for table schema..."
        )

        ddl_commands = self._get_ddl_commands(mdl, column_indexing_batch_size)

        return {
            "documents": [
                Document(
                    id=str(uuid.uuid4()),
                    meta=(
                        {
                            "project_id": id,
                            "type": "TABLE_SCHEMA",
                            "name": ddl_command["name"],
                        }
                        if id
                        else {
                            "type": "TABLE_SCHEMA",
                            "name": ddl_command["name"],
                        }
                    ),
                    content=ddl_command["payload"],
                )
                for ddl_command in tqdm(
                    ddl_commands,
                    desc="indexing ddl commands into the dbschema store",
                )
            ]
        }

    def _get_ddl_commands(
        self, mdl: Dict[str, Any], column_indexing_batch_size: int = 50
    ) -> List[dict]:
        semantics = {
            "models": [],
            "relationships": mdl["relationships"],
            "views": mdl["views"],
            "metrics": mdl["metrics"],
        }

        for model in mdl["models"]:
            columns = []
            for column in model.get("columns", []):
                ddl_column = {
                    "name": column.get("name", ""),
                    "type": column.get("type", ""),
                }
                if "properties" in column:
                    ddl_column["properties"] = column["properties"]
                if "relationship" in column:
                    ddl_column["relationship"] = column["relationship"]
                if "expression" in column:
                    ddl_column["expression"] = column["expression"]
                if "isCalculated" in column:
                    ddl_column["isCalculated"] = column["isCalculated"]

                columns.append(ddl_column)

            semantics["models"].append(
                {
                    "name": model.get("name", ""),
                    "properties": model.get("properties", {}),
                    "columns": columns,
                    "primaryKey": model.get("primaryKey", ""),
                }
            )

        return (
            self._convert_models_and_relationships(
                semantics["models"],
                semantics["relationships"],
                column_indexing_batch_size,
            )
            + self._convert_views(semantics["views"])
            + self._convert_metrics(semantics["metrics"])
        )

    # TODO: refactor this method
    def _convert_models_and_relationships(
        self,
        models: List[Dict[str, Any]],
        relationships: List[Dict[str, Any]],
        column_indexing_batch_size: int,
    ) -> List[str]:
        ddl_commands = []

        # A map to store model primary keys for foreign key relationships
        primary_keys_map = {model["name"]: model["primaryKey"] for model in models}

        for model in models:
            table_name = model["name"]
            columns_ddl = []
            for column in model["columns"]:
                if "relationship" not in column:
                    if "properties" in column:
                        column_properties = {
                            "alias": column["properties"].get("displayName", ""),
                            "description": column["properties"].get("description", ""),
                        }
                        nested_cols = {
                            k: v
                            for k, v in column["properties"].items()
                            if k.startswith("nested")
                        }
                        if nested_cols:
                            column_properties["nested_columns"] = nested_cols
                        comment = (
                            f"-- {orjson.dumps(column_properties).decode("utf-8")}\n  "
                        )
                    else:
                        comment = ""
                    if "isCalculated" in column and column["isCalculated"]:
                        comment = (
                            comment
                            + f"-- This column is a Calculated Field\n  -- column expression: {column["expression"]}\n  "
                        )

                    columns_ddl.append(
                        {
                            "type": "COLUMN",
                            "comment": comment,
                            "name": column["name"],
                            "data_type": column["type"],
                            "is_primary_key": column["name"] == model["primaryKey"],
                        }
                    )

            # Add foreign key constraints based on relationships
            for relationship in relationships:
                condition = relationship.get("condition", "")
                join_type = relationship.get("joinType", "")
                models = relationship.get("models", [])

                if len(models) == 2:
                    comment = (
                        f'-- {{"condition": {condition}, "joinType": {join_type}}}\n  '
                    )
                    should_add_fk = False
                    if table_name == models[0] and join_type.upper() == "MANY_TO_ONE":
                        related_table = models[1]
                        fk_column = condition.split(" = ")[0].split(".")[1]
                        fk_constraint = f"FOREIGN KEY ({fk_column}) REFERENCES {related_table}({primary_keys_map[related_table]})"
                        should_add_fk = True
                    elif table_name == models[1] and join_type.upper() == "ONE_TO_MANY":
                        related_table = models[0]
                        fk_column = condition.split(" = ")[1].split(".")[1]
                        fk_constraint = f"FOREIGN KEY ({fk_column}) REFERENCES {related_table}({primary_keys_map[related_table]})"
                        should_add_fk = True
                    elif table_name in models and join_type.upper() == "ONE_TO_ONE":
                        index = models.index(table_name)
                        related_table = [m for m in models if m != table_name][0]
                        fk_column = condition.split(" = ")[index].split(".")[1]
                        fk_constraint = f"FOREIGN KEY ({fk_column}) REFERENCES {related_table}({primary_keys_map[related_table]})"
                        should_add_fk = True

                    if should_add_fk:
                        columns_ddl.append(
                            {
                                "type": "FOREIGN_KEY",
                                "comment": comment,
                                "constraint": fk_constraint,
                                "tables": models,
                            }
                        )

            if "properties" in model:
                model_properties = {
                    "alias": model["properties"].get("displayName", ""),
                    "description": model["properties"].get("description", ""),
                }
                comment = f"\n/* {orjson.dumps(model_properties).decode("utf-8")} */\n"
            else:
                comment = ""

            ddl_commands.append(
                {
                    "name": table_name,
                    "payload": str(
                        {
                            "type": "TABLE",
                            "comment": comment,
                            "name": table_name,
                        }
                    ),
                }
            )
            column_ddl_commands = [
                {
                    "name": table_name,
                    "payload": str(
                        {
                            "type": "TABLE_COLUMNS",
                            "columns": columns_ddl[i : i + column_indexing_batch_size],
                        }
                    ),
                }
                for i in range(0, len(columns_ddl), column_indexing_batch_size)
            ]
            ddl_commands += column_ddl_commands

        return ddl_commands

    def _convert_views(self, views: List[Dict[str, Any]]) -> List[str]:
        def _format(view: Dict[str, Any]) -> dict:
            return {
                "type": "VIEW",
                "comment": f"/* {view['properties']} */\n"
                if "properties" in view
                else "",
                "name": view["name"],
                "statement": view["statement"],
            }

        return [{"name": view["name"], "payload": str(_format(view))} for view in views]

    def _convert_metrics(self, metrics: List[Dict[str, Any]]) -> List[str]:
        ddl_commands = []

        for metric in metrics:
            table_name = metric.get("name", "")
            columns_ddl = []
            for dimension in metric.get("dimension", []):
                comment = "-- This column is a dimension\n  "
                name = dimension.get("name", "")
                columns_ddl.append(
                    {
                        "type": "COLUMN",
                        "comment": comment,
                        "name": name,
                        "data_type": dimension.get("type", ""),
                    }
                )

            for measure in metric.get("measure", []):
                comment = f"-- This column is a measure\n  -- expression: {measure["expression"]}\n  "
                name = measure.get("name", "")
                columns_ddl.append(
                    {
                        "type": "COLUMN",
                        "comment": comment,
                        "name": name,
                        "data_type": measure.get("type", ""),
                    }
                )

            comment = f"\n/* This table is a metric */\n/* Metric Base Object: {metric["baseObject"]} */\n"
            ddl_commands.append(
                {
                    "name": table_name,
                    "payload": str(
                        {
                            "type": "METRIC",
                            "comment": comment,
                            "name": table_name,
                            "columns": columns_ddl,
                        }
                    ),
                }
            )

        return ddl_commands


@component
class TableDescriptionConverter:
    @component.output_types(documents=List[Document])
    def run(self, mdl: Dict[str, Any], id: Optional[str] = None):
        logger.info(
            "Ask Indexing pipeline is writing new documents for table descriptions..."
        )

        table_descriptions = self._get_table_descriptions(mdl)

        return {
            "documents": [
                Document(
                    id=str(uuid.uuid4()),
                    meta=(
                        {"project_id": id, "type": "TABLE_DESCRIPTION"}
                        if id
                        else {"type": "TABLE_DESCRIPTION"}
                    ),
                    content=table_description,
                )
                for table_description in tqdm(
                    table_descriptions,
                    desc="indexing table descriptions into the table description store",
                )
            ]
        }

    def _get_table_descriptions(self, mdl: Dict[str, Any]) -> List[str]:
        table_descriptions = []
        mdl_data = [
            {
                "mdl_type": "MODEL",
                "payload": mdl["models"],
            },
            {
                "mdl_type": "METRIC",
                "payload": mdl["metrics"],
            },
            {
                "mdl_type": "VIEW",
                "payload": mdl["views"],
            },
        ]

        for data in mdl_data:
            payload = data["payload"]
            for unit in payload:
                if name := unit.get("name", ""):
                    table_description = {
                        "name": name,
                        "mdl_type": data["mdl_type"],
                        "description": unit.get("properties", {}).get(
                            "description", ""
                        ),
                    }
                    table_descriptions.append(str(table_description))

        return table_descriptions


@component
class AsyncDocumentWriter(DocumentWriter):
    @component.output_types(documents_written=int)
    async def run(
        self, documents: List[Document], policy: Optional[DuplicatePolicy] = None
    ):
        if policy is None:
            policy = self.policy

        documents_written = await self.document_store.write_documents(
            documents=documents, policy=policy
        )
        return {"documents_written": documents_written}


## Start of Pipeline
@async_timer
@observe(capture_input=False, capture_output=False)
async def clean_document_store(
    mdl_str: str, cleaner: DocumentCleaner, id: Optional[str] = None
) -> Dict[str, Any]:
    return await cleaner.run(mdl=mdl_str, id=id)


@timer
@observe(capture_input=False, capture_output=False)
@extract_fields(dict(mdl=Dict[str, Any]))
def validate_mdl(
    clean_document_store: Dict[str, Any], validator: MDLValidator
) -> Dict[str, Any]:
    mdl = clean_document_store.get("mdl")
    res = validator.run(mdl=mdl)
    return dict(mdl=res["mdl"])


@timer
@observe(capture_input=False)
def covert_to_table_descriptions(
    mdl: Dict[str, Any],
    table_description_converter: TableDescriptionConverter,
    id: Optional[str] = None,
) -> Dict[str, Any]:
    return table_description_converter.run(mdl=mdl, id=id)


@async_timer
@observe(capture_input=False, capture_output=False)
async def embed_table_descriptions(
    covert_to_table_descriptions: Dict[str, Any],
    document_embedder: Any,
) -> Dict[str, Any]:
    return await document_embedder.run(covert_to_table_descriptions["documents"])


@async_timer
@observe(capture_input=False)
async def write_table_description(
    embed_table_descriptions: Dict[str, Any], table_description_writer: DocumentWriter
) -> None:
    return await table_description_writer.run(
        documents=embed_table_descriptions["documents"]
    )


@timer
@observe(capture_input=False)
def convert_to_ddl(
    mdl: Dict[str, Any],
    ddl_converter: DDLConverter,
    column_indexing_batch_size: int,
    id: Optional[str] = None,
) -> Dict[str, Any]:
    return ddl_converter.run(
        mdl=mdl,
        column_indexing_batch_size=column_indexing_batch_size,
        id=id,
    )


@async_timer
@observe(capture_input=False, capture_output=False)
async def embed_dbschema(
    convert_to_ddl: Dict[str, Any],
    document_embedder: Any,
) -> Dict[str, Any]:
    return await document_embedder.run(documents=convert_to_ddl["documents"])


@async_timer
@observe(capture_input=False)
async def write_dbschema(
    embed_dbschema: Dict[str, Any], dbschema_writer: DocumentWriter
) -> None:
    return await dbschema_writer.run(documents=embed_dbschema["documents"])


@timer
@observe(capture_input=False)
def view_chunk(
    mdl: Dict[str, Any], view_chunker: ViewChunker, id: Optional[str] = None
) -> Dict[str, Any]:
    return view_chunker.run(mdl=mdl, id=id)


@async_timer
@observe(capture_input=False, capture_output=False)
async def embed_view(
    view_chunk: Dict[str, Any], document_embedder: Any
) -> Dict[str, Any]:
    return await document_embedder.run(documents=view_chunk["documents"])


@async_timer
@observe(capture_input=False)
async def write_view(embed_view: Dict[str, Any], view_writer: DocumentWriter) -> None:
    return await view_writer.run(documents=embed_view["documents"])


## End of Pipeline


class Indexing(BasicPipeline):
    def __init__(
        self,
        embedder_provider: EmbedderProvider,
        document_store_provider: DocumentStoreProvider,
        column_indexing_batch_size: Optional[int] = 50,
        **kwargs,
    ) -> None:
        dbschema_store = document_store_provider.get_store()
        view_store = document_store_provider.get_store(dataset_name="view_questions")
        table_description_store = document_store_provider.get_store(
            dataset_name="table_descriptions"
        )

        self._components = {
            "cleaner": DocumentCleaner(
                [dbschema_store, view_store, table_description_store]
            ),
            "validator": MDLValidator(),
            "document_embedder": embedder_provider.get_document_embedder(),
            "ddl_converter": DDLConverter(),
            "table_description_converter": TableDescriptionConverter(),
            "dbschema_writer": AsyncDocumentWriter(
                document_store=dbschema_store,
                policy=DuplicatePolicy.OVERWRITE,
            ),
            "view_chunker": ViewChunker(),
            "view_writer": AsyncDocumentWriter(
                document_store=view_store,
                policy=DuplicatePolicy.OVERWRITE,
            ),
            "table_description_writer": AsyncDocumentWriter(
                document_store=table_description_store,
                policy=DuplicatePolicy.OVERWRITE,
            ),
        }

        self._configs = {
            "column_indexing_batch_size": column_indexing_batch_size,
        }

        super().__init__(
            AsyncDriver({}, sys.modules[__name__], result_builder=base.DictResult())
        )

    def visualize(self, mdl_str: str, id: Optional[str] = None) -> None:
        destination = "outputs/pipelines/indexing"
        if not Path(destination).exists():
            Path(destination).mkdir(parents=True, exist_ok=True)

        self._pipe.visualize_execution(
            ["write_dbschema", "write_view", "write_table_description"],
            output_file_path=f"{destination}/indexing.dot",
            inputs={
                "mdl_str": mdl_str,
                "id": id,
                **self._components,
                **self._configs,
            },
            show_legend=True,
            orient="LR",
        )

    @async_timer
    @observe(name="Document Indexing")
    async def run(self, mdl_str: str, id: Optional[str] = None) -> Dict[str, Any]:
        logger.info("Document Indexing pipeline is running...")
        return await self._pipe.execute(
            ["write_dbschema", "write_view", "write_table_description"],
            inputs={
                "mdl_str": mdl_str,
                "id": id,
                **self._components,
                **self._configs,
            },
        )


if __name__ == "__main__":
    from src.pipelines.common import dry_run_pipeline

    dry_run_pipeline(
        Indexing,
        "indexing",
        mdl_str='{"models": [], "views": [], "relationships": [], "metrics": []}',
    )
