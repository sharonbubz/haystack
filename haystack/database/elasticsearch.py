import logging

from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk, scan
from haystack.database.base import BaseDocumentStore, Document

logger = logging.getLogger(__name__)

    
class ElasticsearchDocumentStore(BaseDocumentStore):
    def __init__(
        self,
        host="localhost",
        username="",
        password="",
        api_id="",
        api_key="",
        index="document",
        search_fields="text",
        text_field="text",
        name_field="name",
        external_source_id_field="external_source_id",
        tag_fields=None,
        embedding_field=None,
        embedding_dim=None,
        custom_mapping=None,
        excluded_meta_data=None,
        scheme="https",
        ca_certs=False,
        verify_certs=False,
        create_index=True
    ):
        print(host, username, password, index)
        
        if username and password:
            self.client = Elasticsearch([host], http_auth=(username, password), scheme=scheme, \
					ca_certs=ca_certs, verify_certs=verify_certs)
            self.graph_client = self.client.graph

        if api_id and api_key:
            self.client = Elasticsearch(hosts=[{"host": host}], api_key=(api_id, api_key), scheme=scheme)

        # if no custom_mapping is supplied, use the default mapping
        if not custom_mapping:
            custom_mapping = {
                "mappings": {
                    "properties": {
                        name_field: {"type": "text"},
                        text_field: {"type": "text"},
                        external_source_id_field: {"type": "text"},
                    }
                }
            }
            if embedding_field:
                custom_mapping["mappings"]["properties"][embedding_field] = {"type": "dense_vector",
                                                                             "dims": embedding_dim}
        # create an index if not exists
        if create_index:
            self.client.indices.create(index=index, ignore=400, body=custom_mapping)
        self.index = index

        # configure mappings to ES fields that will be used for querying / displaying results
        if type(search_fields) == str:
            search_fields = [search_fields]

        #TODO we should implement a more flexible interal mapping here that simplifies the usage of additional,
        # custom fields (e.g. meta data you want to return)
        self.search_fields = search_fields
        self.text_field = text_field
        self.name_field = name_field
        self.tag_fields = tag_fields
        self.external_source_id_field = external_source_id_field
        self.embedding_field = embedding_field
        self.excluded_meta_data = excluded_meta_data

    def get_document_by_id(self, id: str) -> Document:
        query = {"query": {"ids": {"values": [id]}}}
        result = self.client.search(index=self.index, body=query)["hits"]["hits"]

        document = self._convert_es_hit_to_document(result[0]) if result else None
        return document

    def get_document_ids_by_tags(self, tags: dict) -> [str]:
        term_queries = [{"terms": {key: value}} for key, value in tags.items()]
        query = {"query": {"bool": {"must": term_queries}}}
        logger.debug(f"Tag filter query: {query}")
        result = self.client.search(index=self.index, body=query, size=10000)["hits"]["hits"]
        doc_ids = []
        for hit in result:
            doc_ids.append(hit["_id"])
        return doc_ids

    def write_documents(self, documents):
        for doc in documents:
            doc["_op_type"] = "create"
            doc["_index"] = self.index

        bulk(self.client, documents)

    def get_document_count(self):
        result = self.client.count()
        count = result["count"]
        return count

    def get_all_documents(self):
        result = scan(self.client, query={"query": {"match_all": {}}}, index=self.index)
        documents = [self._convert_es_hit_to_document(hit) for hit in result]

        return documents
    
    def construct_embedding_query(
        self, 
        query_emb, 
        top_k=10, 
        candidate_doc_ids=None, 
    ) -> str:
        if not self.embedding_field:
            raise RuntimeError("Please specify arg `embedding_field` in ElasticsearchDocumentStore()")
        else:
            # +1 in cosine similarity to avoid negative numbers
            body= {
                "size": top_k,
                "query": {
                    "script_score": {
                        "query": {"match_all": {}},
                        "script": {
                            "source": "cosineSimilarity(params.query_vector,doc['question_emb']) + 1.0",
                            "params": {
                                "query_vector": query_emb
                            }
                        }
                    }
                }
            }

            if candidate_doc_ids:
                body["query"]["script_score"]["query"] = {
                    "bool": {
                        "should": [{"match_all": {}}],
                        "filter": [{"terms": {"_id": candidate_doc_ids}}]
                }}

            if self.excluded_meta_data:
                body["_source"] = {"excludes": self.excluded_meta_data}

        return body
        
    def construct_text_query(
        self,
        query: str,
        top_k: int = 10,
        candidate_doc_ids: [str] = None,
        direct_filters: dict = None,
        custom_query: str = None,
    ) -> str:
        
        # TODO:
        # for now: we keep the current structure of candidate_doc_ids for compatibility with SQL documentstores
        # midterm: get rid of it and do filtering with tags directly in this query

        # if a custom search query is provided then use it
        if custom_query:
            if "size" not in custom_query and top_k >= 0:
                custom_query.update({"size": top_k})
            body = custom_query
            print("Custom query", custom_query)
        # else use standard search query for provided search fields
        else:
            body = {
                "size": top_k,
                "query": {
                    "bool": {
                        "should": [{"multi_match": {"query": query, "type": "most_fields", "fields": self.search_fields}}]
                    }
                },
            }

        # use other filters directly with query, if provided
        if direct_filters:
            # filter types are must, should, etc.
            for filter_type, filter_dict in direct_filters.items():
                body["query"]["bool"][filter_type] = filter_dict

        if candidate_doc_ids:
            body["query"]["bool"]["filter"] = [{"terms": {"_id": candidate_doc_ids}}]

        if self.excluded_meta_data:
            body["_source"] = {"excludes": self.excluded_meta_data}
            
        return body
        
    def multiquery(
        self,
        query: [str],
    ) -> [Document]:
        body = query
        result = self.client.msearch(index=self.index, body=body)["hits"]["hits"]
        
        documents = [self._convert_es_hit_to_document(hit) for hit in result]
        return documents
    
    def query(
        self,
        query: str,
        top_k: int = 10,
        candidate_doc_ids: [str] = None,
        direct_filters: dict = None,
        custom_query: str = None,
    ) -> [Document]:
        logger.info(f"Constructing text query: {query}")
        body = self.construct_text_query(query, top_k, candidate_doc_ids, direct_filters, custom_query)
        
        logger.info(f"Retriever query: {body}")
        result = self.client.search(index=self.index, body=body)
        
        #["hits"]["hits"]
        documents = [self._convert_es_hit_to_document(hit) for hit in result]
        return documents
    
    def query_by_completion(
        self,
        query: str,
        top_k: int = 10,
        candidate_doc_ids: [str] = None,
        direct_filters: dict = None,
        custom_query: str = None,
        is_suggest: bool = True,
    ):
        logger.info(f"Constructing text query: {query}")
        body = self.construct_text_query(query, top_k, candidate_doc_ids, direct_filters, custom_query)
        
        logger.info(f"Retriever query: {body}")
        result = self.client.search(index=self.index, body=body)
        
        #["hits"]["hits"]
        if is_suggest:
            result = result.get('aggregations', {}).items()
            suggestions = [(key, buckets.get('buckets')) for key, buckets in result]
        else:
            result = result.get('hits', {}).get('hits', {})
            documents = [self._convert_es_hit_to_document(hit) for hit in result]
        return suggestions
    
    def query_by_graph(
        self,
        query: str,
        top_k: int = 10,
        candidate_doc_ids: [str] = None,
        direct_filters: dict = None,
        custom_query: str = None,
        is_suggest: bool = True,
    ):
        print('Query')
        logger.info(f"Constructing text query: {query}")
        body = self.construct_text_query(query, top_k, candidate_doc_ids, direct_filters, custom_query)
        
        logger.info(f"Retriever query: {body}")
        results = self.graph_client.explore(index=self.index, body=body)
        if is_suggest:
            suggestions = [self._convert_graph_hits_to_suggestions() ]
        else:
            documents = [self._convert_es_hit_to_document(hit) for hit in result]
        return suggestions

    def query_by_aggregate(
        self,
        query: str,
        top_k: int = 10,
        candidate_doc_ids: [str] = None,
        direct_filters: dict = None,
        custom_query: str = None,
        is_suggest: bool = True,
    ):
        
        print('Query')
        logger.info(f"Constructing text query: {query}")
        body = self.construct_text_query(query, top_k, candidate_doc_ids, direct_filters, custom_query)
        
        logger.info(f"Retriever query: {body}")
        result = self.client.search(index=self.index, body=body)['hits']['aggregations']

        logger.info(f"Process aggregated results: {body}")
        
        if is_suggest:
            suggestions = [self._convert_agg_hits_to_suggestion(key, bucket) for key, bucket in results.items()]
        else:
            documents = [self._convert_es_hit_to_document(hit) for hit in result]
        
        return suggestions
    
    def query_by_embedding(
            self, 
            query_emb, 
            top_k=10, 
            candidate_doc_ids=None, 
        ) -> [Document]:
        logger.debug(f"Constructing embedding query: {query_emb}")
        body = self.construct_embedding_query(query_emb, top_k, candidate_doc_ids)
        
        logger.debug(f"Retriever query: {body}")
        result = self.client.search(index=self.index, body=body)["hits"]["hits"]

        documents = [self._convert_es_hit_to_document(hit, score_adjustment=-1) for hit in result]
        return documents

    def _convert_es_hit_to_document(self, hit, score_adjustment=0) -> [Document]:
        # We put all additional data of the doc into meta_data and return it in the API
        meta_data = {k:v for k,v in hit["_source"].items() if k not in (self.text_field, self.external_source_id_field)}
        meta_data["name"] = meta_data.pop(self.name_field)

        document = Document(
            id=hit["_id"],
            text=hit["_source"][self.text_field],
            external_source_id=hit["_source"].get(self.external_source_id_field),
            meta=meta_data,
            query_score=hit["_score"] + score_adjustment,
        )
        return document
