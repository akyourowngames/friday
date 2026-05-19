from .embedder import embed

import numpy as np


class Verifier:
    SIMILARITY_THRESHOLD = 0.25
    ACTION_DESCRIPTION_THRESHOLD = 0.30

    def __init__(self):
        self._tool_emb_cache = {}

    def _get_tool_emb(self, schema):
        key = schema["function"]["name"]
        if key not in self._tool_emb_cache:
            text = schema["function"]["description"]
            self._tool_emb_cache[key] = embed(text)
        return self._tool_emb_cache[key]

    def _check_against_tools(self, response, available_schemas):
        if not available_schemas:
            return "PASS"
        if len(response.strip()) < 30:
            return "PASS"
        resp_emb = embed(response)
        for schema in available_schemas:
            tool_emb = self._get_tool_emb(schema)
            sim = float(np.dot(resp_emb, tool_emb))
            if sim >= self.ACTION_DESCRIPTION_THRESHOLD:
                return "FAIL"
        return "PASS"

    def _check_against_results(self, response, tool_results):
        if not tool_results:
            return "PASS"
        if len(response.strip()) < 30:
            return "PASS"
        response_emb = embed(response)
        for result in tool_results:
            if not result or result in ("Done", "Error"):
                continue
            result_emb = embed(result)
            sim = float(np.dot(response_emb, result_emb))
            if sim >= self.SIMILARITY_THRESHOLD:
                return "PASS"
        return "FAIL"

    def verify(self, response, called, tool_results, available_schemas=None):
        if not response:
            return "PASS"
        if not called:
            return self._check_against_tools(response, available_schemas)
        if tool_results:
            return self._check_against_results(response, tool_results)
        return "PASS"
