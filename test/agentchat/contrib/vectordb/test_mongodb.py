import logging
import os
from time import monotonic, sleep
from typing import List

import pytest

from autogen.agentchat.contrib.vectordb.base import Document

try:
    import pymongo
    import sentence_transformers

    from autogen.agentchat.contrib.vectordb.mongodb import MongoDBAtlasVectorDB
except ImportError:
    # To display warning in pyproject.toml [tool.pytest.ini_options] set log_cli = true
    logger = logging.getLogger(__name__)
    logger.warning(f"skipping {__name__}. It requires one to pip install pymongo or the extra [retrievechat-mongodb]")
    pytest.skip(allow_module_level=True)

from pymongo.collection import Collection

logger = logging.getLogger(__name__)

MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:64684/?directConnection=true")
MONGODB_DATABASE = os.environ.get("DATABASE", "autogen_test_db")
MONGODB_COLLECTION = os.environ.get("MONGODB_COLLECTION", "autogen_test_vectorstore")
MONGODB_INDEX = os.environ.get("MONGODB_INDEX", "vector_index")

RETRIES = 10
DELAY = 2
TIMEOUT = 20.0


def _wait_for_predicate(predicate, err, timeout=TIMEOUT, interval=DELAY):
    """Generic to block until the predicate returns true

    Args:
        predicate (Callable[, bool]): A function that returns a boolean value
        err (str): Error message to raise if nothing occurs
        timeout (float, optional): Length of time to wait for predicate. Defaults to TIMEOUT.
        interval (float, optional): Interval to check predicate. Defaults to DELAY.

    Raises:
        TimeoutError: _description_
    """
    start = monotonic()
    while not predicate():
        if monotonic() - start > TIMEOUT:
            raise TimeoutError(err)
        sleep(DELAY)


def _delete_collections(database):
    """Delete all collections within the database

    Args:
        database (pymongo.Database): MongoDB Database Abstraction
    """
    for collection_name in database.list_collection_names():
        database[collection_name].drop()
    _wait_for_predicate(lambda: not database.list_collection_names(), "Not all collections deleted")


@pytest.fixture
def db():
    """VectorDB setup and teardown, including collections and search indexes"""
    vectorstore = MongoDBAtlasVectorDB(
        connection_string=MONGODB_URI,
        database_name=MONGODB_DATABASE,
        wait_until_ready=True,
        overwrite=True,
    )
    yield vectorstore
    _delete_collections(vectorstore.db)


@pytest.fixture
def example_documents() -> List[Document]:
    """Note mix of integers and strings as ids"""
    return [
        Document(id=1, content="Dogs are tough.", metadata={"a": 1}),
        Document(id=2, content="Cats have fluff.", metadata={"b": 1}),
        Document(id="1", content="What is a sandwich?", metadata={"c": 1}),
        Document(id="2", content="A sandwich makes a great lunch.", metadata={"d": 1, "e": 2}),
    ]


@pytest.fixture
def db_with_indexed_clxn():
    """VectorDB with a collection created immediately"""
    vectorstore = MongoDBAtlasVectorDB(
        connection_string=MONGODB_URI,
        database_name=MONGODB_DATABASE,
        wait_until_ready=True,
        collection_name=MONGODB_COLLECTION,
        overwrite=True,
    )
    yield vectorstore, vectorstore.db[MONGODB_COLLECTION]
    _delete_collections(vectorstore.db)


def test_create_collection(db):
    """
    def create_collection(collection_name: str,
                        overwrite: bool = False) -> Collection
    Create a collection in the vector database.
    - Case 1. if the collection does not exist, create the collection.
    - Case 2. the collection exists, if overwrite is True, it will overwrite the collection.
    - Case 3. the collection exists and overwrite is False return the existing collection.
    """
    collection_name = "test_collection"

    collection_case_1 = db.create_collection(
        collection_name=collection_name,
    )
    assert collection_case_1.name == collection_name

    collection_case_2 = db.create_collection(
        collection_name=collection_name,
        overwrite=True,
    )
    assert collection_case_2.name == collection_name

    collection_case_3 = db.create_collection(
        collection_name=collection_name,
    )
    assert collection_case_3.name == collection_name


def test_get_collection(db):
    collection_name = MONGODB_COLLECTION

    with pytest.raises(ValueError):
        db.get_collection()

    collection_created = db.create_collection(collection_name)
    assert isinstance(collection_created, Collection)
    assert collection_created.name == collection_name

    collection_got = db.get_collection(collection_name)
    assert collection_got.name == collection_created.name
    assert collection_got.name == db.active_collection.name


def test_delete_collection(db):
    assert MONGODB_COLLECTION not in db.list_collections()
    collection = db.create_collection(MONGODB_COLLECTION)
    assert MONGODB_COLLECTION in db.list_collections()
    db.delete_collection(collection.name)
    assert MONGODB_COLLECTION not in db.list_collections()


def test_insert_docs(db, example_documents):
    # Test that there's an active collection
    with pytest.raises(ValueError) as exc:
        db.insert_docs(example_documents)
    assert "No collection is specified" in str(exc.value)

    # Test upsert
    db.insert_docs(example_documents, MONGODB_COLLECTION, upsert=True)

    # Create a collection
    db.delete_collection(MONGODB_COLLECTION)
    collection = db.create_collection(MONGODB_COLLECTION)

    # Insert example documents
    db.insert_docs(example_documents, collection_name=MONGODB_COLLECTION)
    found = list(collection.find({}))
    assert len(found) == len(example_documents)
    # Check that documents have correct fields, including "_id" and "embedding" but not "id"
    assert all([set(doc.keys()) == {"_id", "content", "metadata", "embedding"} for doc in found])
    # Check ids
    assert {doc["_id"] for doc in found} == {1, "1", 2, "2"}
    # Check embedding lengths
    assert len(found[0]["embedding"]) == 384


def test_update_docs(db_with_indexed_clxn, example_documents):
    db, collection = db_with_indexed_clxn
    # Use update_docs to insert new documents
    db.update_docs(example_documents, MONGODB_COLLECTION, upsert=True)
    # Test that no changes were made to example_documents
    assert set(example_documents[0].keys()) == {"id", "content", "metadata"}
    assert collection.count_documents({}) == len(example_documents)
    found = list(collection.find({}))
    # Check that documents have correct fields, including "_id" and "embedding" but not "id"
    assert all([set(doc.keys()) == {"_id", "content", "metadata", "embedding"} for doc in found])
    # Check ids
    assert {doc["_id"] for doc in found} == {1, "1", 2, "2"}

    # Update an *existing* Document
    updated_doc = Document(id=1, content="Cats are tough.", metadata={"a": 10})
    db.update_docs([updated_doc], MONGODB_COLLECTION)
    assert collection.find_one({"_id": 1})["content"] == "Cats are tough."

    # Upsert a *new* Document
    new_id = 3
    new_doc = Document(id=new_id, content="Cats are tough.")
    db.update_docs([new_doc], MONGODB_COLLECTION, upsert=True)
    assert collection.find_one({"_id": new_id})["content"] == "Cats are tough."

    # Attempting to use update to insert a new doc
    # *without* setting upsert set to True
    # is a no-op in MongoDB. # TODO Confirm behaviour and autogen's preference.
    new_id = 4
    new_doc = Document(id=new_id, content="That is NOT a sandwich?")
    db.update_docs([new_doc], MONGODB_COLLECTION)
    assert collection.find_one({"_id": new_id}) is None


def test_delete_docs(db_with_indexed_clxn, example_documents):
    db, clxn = db_with_indexed_clxn
    # Insert example documents
    db.insert_docs(example_documents, collection_name=MONGODB_COLLECTION)
    # Delete the 1s
    db.delete_docs(ids=[1, "1"], collection_name=MONGODB_COLLECTION)
    # Confirm just the 2s remain
    assert {2, "2"} == {doc["_id"] for doc in clxn.find({})}


def test_get_docs_by_ids(db_with_indexed_clxn, example_documents):
    db, _ = db_with_indexed_clxn
    # Insert example documents
    db.insert_docs(example_documents, collection_name=MONGODB_COLLECTION)

    # Test without setting "include" kwarg
    docs = db.get_docs_by_ids(ids=[2, "2"], collection_name=MONGODB_COLLECTION)
    assert len(docs) == 2
    assert all([doc["id"] in [2, "2"] for doc in docs])
    assert set(docs[0].keys()) == {"id", "content", "metadata"}

    # Test with include
    docs = db.get_docs_by_ids(ids=[2], include=["content"], collection_name=MONGODB_COLLECTION)
    assert len(docs) == 1
    assert set(docs[0].keys()) == {"id", "content"}

    # Test with empty ids list
    docs = db.get_docs_by_ids(ids=[], include=["content"], collection_name=MONGODB_COLLECTION)
    assert len(docs) == 0


def test_retrieve_docs(db_with_indexed_clxn, example_documents):
    db, _ = db_with_indexed_clxn
    # Sanity test. Retrieving docs before documents have been added
    assert db.retrieve_docs(queries=["Cats"], collection_name=MONGODB_COLLECTION, n_results=2) == []
    # Insert example documents
    db.insert_docs(example_documents, collection_name=MONGODB_COLLECTION)

    # Begin testing Atlas Vector Search
    # NOTE: Indexing may take some time, so we must be patient on the first query.
    # We have the wait_until_ready flag to ensure index is created and ready
    # Immediately adding documents and then querying is only standard for testing

    n_results = 2  # Number of closest docs to return

    success = False
    start = monotonic()
    while monotonic() - start < TIMEOUT:
        results = db.retrieve_docs(queries=["Cats"], collection_name=MONGODB_COLLECTION, n_results=n_results)
        if len(results[0]) == n_results:
            success = True
            break
        else:
            sleep(DELAY)
    assert success, f"Failed to retrieve docs after waiting {TIMEOUT} seconds after each."

    assert {doc[0]["id"] for doc in results[0]} == {1, 2}

    # Empty list of queries returns empty list of results
    results = db.retrieve_docs(queries=[], collection_name=MONGODB_COLLECTION, n_results=n_results)
    assert results == []

    # Empty list of queries returns empty list of results
    queries = ["Some good pets", "What kind of Sandwich?"]
    results = db.retrieve_docs(queries=queries, collection_name=MONGODB_COLLECTION, n_results=2)
    assert len(results) == len(queries)
    assert all([len(res) == n_results for res in results])
    assert {doc[0]["id"] for doc in results[0]} == {1, 2}
    assert {doc[0]["id"] for doc in results[1]} == {"1", "2"}
